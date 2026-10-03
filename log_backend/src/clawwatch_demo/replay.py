"""Durable, fixed-rate replay scheduling and lifecycle controls."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from clawwatch_demo.models import ReplayRun, ReplayState
from clawwatch_demo.storage import connect, immediate_transaction, migrate

ACTIVE_STATES = ("ready", "running", "paused", "interrupted")
TERMINAL_STATES = ("completed", "stopped", "failed")
VALID_PRESETS = ("all", "authentication")
VALID_ORDERINGS = ("source", "original_time")


class ReplayError(RuntimeError):
    """Base error for replay operations."""


class ReplayConflict(ReplayError):
    """Raised when an operation conflicts with another active replay."""


class ReplayStateError(ReplayError):
    """Raised when a control is invalid for the persisted run state."""


class Clock(Protocol):
    """Small scheduling surface that can be replaced in deterministic tests."""

    def monotonic(self) -> float: ...

    def utcnow(self) -> datetime: ...

    def wait(self, event: threading.Event, timeout: float) -> bool: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)


def _utc_text(clock: Clock) -> str:
    return clock.utcnow().isoformat()


def _selection_json(
    preset: str,
    event_types: tuple[str, ...],
    severities: tuple[str, ...],
) -> str:
    return json.dumps(
        {
            "preset": preset,
            "event_types": list(event_types),
            "severities": list(severities),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_filters(values: Iterable[str] | None, name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
    if any(not value for value in normalized):
        raise ReplayError(f"{name} cannot contain blank values")
    return normalized


def _decode_selection(raw: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    try:
        value = json.loads(raw)
        preset = str(value["preset"])
        event_types = tuple(str(item) for item in value.get("event_types", ()))
        severities = tuple(str(item) for item in value.get("severities", ()))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReplayError("Persisted replay selection is invalid") from exc
    return preset, event_types, severities


def _row_to_run(row: sqlite3.Row) -> ReplayRun:
    preset, event_types, severities = _decode_selection(str(row["selection_json"]))
    return ReplayRun(
        id=int(row["id"]),
        dataset_id=int(row["dataset_id"]),
        preset=preset,
        event_types=event_types,
        severities=severities,
        ordering=str(row["ordering"]),
        record_limit=None if row["record_limit"] is None else int(row["record_limit"]),
        rate=float(row["rate"]),
        state=cast(ReplayState, str(row["state"])),
        committed_selection_cursor=int(row["committed_selection_cursor"]),
        emitted_count=int(row["emitted_count"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        updated_at=str(row["updated_at"]),
        ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
    )


def _get_run(connection: sqlite3.Connection, run_id: int) -> ReplayRun:
    row = connection.execute("SELECT * FROM replay_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ReplayError(f"Replay run does not exist: {run_id}")
    return _row_to_run(row)


def get_run(database: Path, run_id: int) -> ReplayRun:
    """Read one replay run using a short-lived connection."""
    connection = connect(database, read_only=True)
    try:
        return _get_run(connection, run_id)
    finally:
        connection.close()


def _record_activity(
    connection: sqlite3.Connection,
    run_id: int,
    action: str,
    prior_state: str | None,
    new_state: str | None,
    occurred_at: str,
    detail: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO activity_log(
            run_id, action, prior_state, new_state, occurred_at, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            action,
            prior_state,
            new_state,
            occurred_at,
            json.dumps(detail or {}, sort_keys=True, separators=(",", ":")),
        ),
    )


def recover_interrupted_runs(database: Path, *, clock: Clock | None = None) -> tuple[int, ...]:
    """Mark previously running or paused producers as interrupted on startup."""
    active_clock = clock or SystemClock()
    migrate(database)
    connection = connect(database)
    recovered: list[int] = []
    try:
        with immediate_transaction(connection):
            rows = connection.execute(
                "SELECT id, state FROM replay_runs WHERE state IN ('running', 'paused')"
            ).fetchall()
            for row in rows:
                run_id = int(row["id"])
                prior_state = str(row["state"])
                now = _utc_text(active_clock)
                connection.execute(
                    """
                    UPDATE replay_runs
                    SET state = 'interrupted', updated_at = ?, last_error = ?
                    WHERE id = ? AND state = ?
                    """,
                    (now, "Application stopped before replay finalized", run_id, prior_state),
                )
                _record_activity(
                    connection,
                    run_id,
                    "interrupted",
                    prior_state,
                    "interrupted",
                    now,
                )
                recovered.append(run_id)
    finally:
        connection.close()
    return tuple(recovered)


def _selection_predicate(run: ReplayRun) -> tuple[str, list[object]]:
    clauses = ["dataset_id = ?"]
    parameters: list[object] = [run.dataset_id]
    event_types = run.event_types
    if run.preset == "authentication":
        event_types = tuple(sorted({*event_types, "auth"}))
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        clauses.append(f"event_type IN ({placeholders})")
        parameters.extend(event_types)
    if run.severities:
        placeholders = ",".join("?" for _ in run.severities)
        clauses.append(f"severity IN ({placeholders})")
        parameters.extend(run.severities)
    return " AND ".join(clauses), parameters


def _complete_run(connection: sqlite3.Connection, run: ReplayRun, now: str) -> None:
    connection.execute(
        """
        UPDATE replay_runs
        SET state = 'completed', updated_at = ?, ended_at = ?, last_error = NULL
        WHERE id = ? AND state = 'running'
        """,
        (now, now, run.id),
    )
    _record_activity(connection, run.id, "completed", "running", "completed", now)


def _emit_next(
    connection: sqlite3.Connection, run_id: int, clock: Clock, *, auto_send_critical: bool = False
) -> bool:
    """Commit the next selected event and replay cursor together."""
    with immediate_transaction(connection):
        run = _get_run(connection, run_id)
        if run.state != "running":
            raise ReplayStateError(f"Run {run_id} is {run.state}, expected running")
        if run.record_limit is not None and run.committed_selection_cursor >= run.record_limit:
            _complete_run(connection, run, _utc_text(clock))
            return False

        predicate, parameters = _selection_predicate(run)
        ordering = (
            "parsed_timestamp, line_number" if run.ordering == "original_time" else "line_number"
        )
        if run.committed_selection_cursor:
            previous = connection.execute(
                """
                SELECT se.line_number, se.parsed_timestamp
                FROM replay_events AS re
                JOIN source_events AS se ON se.id = re.source_event_id
                WHERE re.run_id = ? AND re.sequence = ?
                """,
                (run.id, run.committed_selection_cursor),
            ).fetchone()
            if previous is None:
                raise ReplayError(
                    f"Run {run.id} cursor {run.committed_selection_cursor} has no committed event"
                )
            if run.ordering == "original_time":
                predicate += (
                    " AND (parsed_timestamp > ? OR (parsed_timestamp = ? AND line_number > ?))"
                )
                parameters.extend(
                    [
                        str(previous["parsed_timestamp"]),
                        str(previous["parsed_timestamp"]),
                        int(previous["line_number"]),
                    ]
                )
            else:
                predicate += " AND line_number > ?"
                parameters.append(int(previous["line_number"]))
        source = connection.execute(
            f"""
            SELECT id, severity
            FROM source_events
            WHERE {predicate}
            ORDER BY {ordering}
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if source is None:
            _complete_run(connection, run, _utc_text(clock))
            return False

        sequence = run.committed_selection_cursor + 1
        simulated_at = _utc_text(clock)
        emitted_at = _utc_text(clock)
        emitted = connection.execute(
            """
            INSERT INTO replay_events(
                run_id, sequence, source_event_id, simulated_at, emitted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run.id, sequence, int(source["id"]), simulated_at, emitted_at),
        )
        if auto_send_critical and str(source["severity"]).strip().lower() == "critical":
            connection.execute(
                "INSERT INTO critical_log_outbox(replay_event_id) VALUES (?)",
                (emitted.lastrowid,),
            )
        connection.execute(
            """
            UPDATE replay_runs
            SET committed_selection_cursor = ?, emitted_count = emitted_count + 1,
                updated_at = ?
            WHERE id = ? AND state = 'running'
            """,
            (sequence, emitted_at, run.id),
        )
    return True


class ReplayController:
    """Own the single replay worker and expose acknowledged lifecycle controls."""

    def __init__(
        self, database: Path, *, clock: Clock | None = None, auto_send_critical: bool = False
    ) -> None:
        self.database = database.resolve()
        self.auto_send_critical = auto_send_critical
        self.clock = clock or SystemClock()
        migrate(self.database)
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_run_id: int | None = None
        self._command = "stopped"
        self._rate = 1.0
        self._schedule_revision = 0

    def create_run(
        self,
        *,
        dataset_id: int | None = None,
        preset: str = "all",
        event_types: Iterable[str] | None = None,
        severities: Iterable[str] | None = None,
        ordering: str = "source",
        record_limit: int | None = 1_000,
        rate: float = 10.0,
    ) -> ReplayRun:
        if preset not in VALID_PRESETS:
            raise ReplayError(f"Unknown replay preset: {preset}")
        if ordering not in VALID_ORDERINGS:
            raise ReplayError(f"Unknown replay ordering: {ordering}")
        if record_limit is not None and record_limit <= 0:
            raise ReplayError("record_limit must be positive or None")
        if rate <= 0:
            raise ReplayError("rate must be greater than zero")
        normalized_types = _normalize_filters(event_types, "event_types")
        normalized_severities = _normalize_filters(severities, "severities")
        if preset == "authentication" and normalized_types not in {(), ("auth",)}:
            raise ReplayError("The authentication preset only supports the auth event type")
        selection = _selection_json(preset, normalized_types, normalized_severities)
        now = _utc_text(self.clock)

        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                if dataset_id is None:
                    row = connection.execute(
                        "SELECT id FROM datasets WHERE status = 'complete' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if row is None:
                        raise ReplayError("No completed dataset is available for replay")
                    dataset_id = int(row["id"])
                else:
                    row = connection.execute(
                        "SELECT status FROM datasets WHERE id = ?", (dataset_id,)
                    ).fetchone()
                    if row is None:
                        raise ReplayError(f"Dataset does not exist: {dataset_id}")
                    if row["status"] != "complete":
                        raise ReplayStateError(f"Dataset {dataset_id} is not complete")

                active = connection.execute(
                    "SELECT id, state FROM replay_runs WHERE state IN (?, ?, ?, ?)",
                    ACTIVE_STATES,
                ).fetchone()
                if active is not None:
                    raise ReplayConflict(f"Replay run {active['id']} is still {active['state']}")
                try:
                    cursor = connection.execute(
                        """
                        INSERT INTO replay_runs(
                            dataset_id, selection_json, ordering, record_limit, rate,
                            state, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'ready', ?)
                        """,
                        (dataset_id, selection, ordering, record_limit, float(rate), now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ReplayConflict("Another replay run became active") from exc
                run_id = int(cursor.lastrowid)
                _record_activity(
                    connection,
                    run_id,
                    "created",
                    None,
                    "ready",
                    now,
                    {
                        "preset": preset,
                        "event_types": list(normalized_types),
                        "severities": list(normalized_severities),
                        "ordering": ordering,
                        "record_limit": record_limit,
                        "rate": float(rate),
                    },
                )
                run = _get_run(connection, run_id)
        finally:
            connection.close()
        return run

    def get_run(self, run_id: int) -> ReplayRun:
        return get_run(self.database, run_id)

    def _transition_to_running(self, run_id: int, allowed_state: str, action: str) -> ReplayRun:
        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                run = _get_run(connection, run_id)
                if run.state != allowed_state:
                    raise ReplayStateError(f"Cannot {action} run {run_id} while it is {run.state}")
                now = _utc_text(self.clock)
                started_at = run.started_at or now
                connection.execute(
                    """
                    UPDATE replay_runs
                    SET state = 'running', started_at = ?, updated_at = ?,
                        ended_at = NULL, last_error = NULL
                    WHERE id = ? AND state = ?
                    """,
                    (started_at, now, run_id, allowed_state),
                )
                _record_activity(connection, run_id, action, allowed_state, "running", now)
                return _get_run(connection, run_id)
        finally:
            connection.close()

    def _spawn_worker(self, run: ReplayRun) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                raise ReplayConflict(f"Replay worker already owns run {self._active_run_id}")
            self._active_run_id = run.id
            self._command = "running"
            self._rate = run.rate
            self._schedule_revision += 1
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._worker_main,
                args=(run.id,),
                name=f"clawwatch-replay-{run.id}",
                daemon=True,
            )
            self._thread.start()

    def start(self, run_id: int) -> ReplayRun:
        run = self._transition_to_running(run_id, "ready", "started")
        try:
            self._spawn_worker(run)
        except BaseException as exc:
            self._mark_failed(run_id, exc)
            raise
        return self.get_run(run_id)

    def pause(self, run_id: int, *, timeout: float = 5.0) -> ReplayRun:
        with self._condition:
            if self._active_run_id != run_id or self._thread is None:
                raise ReplayStateError(f"Run {run_id} has no active local worker")
            run = self.get_run(run_id)
            if run.state != "running":
                raise ReplayStateError(f"Cannot pause run {run_id} while it is {run.state}")
            self._command = "paused"
            self._wake.set()
        return self.wait_for_state(run_id, {"paused"}, timeout=timeout)

    def resume(self, run_id: int) -> ReplayRun:
        run = self.get_run(run_id)
        if run.state == "interrupted":
            resumed = self._transition_to_running(run_id, "interrupted", "resumed")
            self._spawn_worker(resumed)
            return self.get_run(run_id)
        if run.state != "paused":
            raise ReplayStateError(f"Cannot resume run {run_id} while it is {run.state}")
        with self._condition:
            if self._active_run_id != run_id or self._thread is None:
                raise ReplayStateError(f"Paused run {run_id} has no active local worker")
            resumed = self._transition_to_running(run_id, "paused", "resumed")
            self._command = "running"
            self._schedule_revision += 1
            self._wake.set()
            self._condition.notify_all()
        return resumed

    def set_rate(self, run_id: int, rate: float) -> ReplayRun:
        if rate <= 0:
            raise ReplayError("rate must be greater than zero")
        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                run = _get_run(connection, run_id)
                if run.state not in ACTIVE_STATES:
                    raise ReplayStateError(
                        f"Cannot change rate for run {run_id} while it is {run.state}"
                    )
                now = _utc_text(self.clock)
                connection.execute(
                    "UPDATE replay_runs SET rate = ?, updated_at = ? WHERE id = ?",
                    (float(rate), now, run_id),
                )
                _record_activity(
                    connection,
                    run_id,
                    "rate_changed",
                    run.state,
                    run.state,
                    now,
                    {"prior_rate": run.rate, "new_rate": float(rate)},
                )
                updated = _get_run(connection, run_id)
        finally:
            connection.close()

        with self._condition:
            if self._active_run_id == run_id:
                self._rate = float(rate)
                self._schedule_revision += 1
                self._wake.set()
        return updated

    def _request_terminal(self, run_id: int, state: str, timeout: float) -> ReplayRun:
        run = self.get_run(run_id)
        if run.state in TERMINAL_STATES:
            return run
        with self._condition:
            has_worker = (
                self._active_run_id == run_id
                and self._thread is not None
                and self._thread.is_alive()
            )
            if has_worker:
                self._command = state
                self._wake.set()
            else:
                self._set_terminal_state(run_id, state)
        return self.wait_for_state(run_id, {state}, timeout=timeout)

    def stop(self, run_id: int, *, timeout: float = 5.0) -> ReplayRun:
        return self._request_terminal(run_id, "stopped", timeout)

    def interrupt(self, run_id: int, *, timeout: float = 5.0) -> ReplayRun:
        return self._request_terminal(run_id, "interrupted", timeout)

    def wait_for_state(
        self,
        run_id: int,
        states: set[str],
        *,
        timeout: float = 5.0,
    ) -> ReplayRun:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run.state in states:
                return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expected = ", ".join(sorted(states))
                raise TimeoutError(
                    f"Timed out waiting for run {run_id} to become {expected}; state={run.state}"
                )
            with self._condition:
                self._condition.wait(min(remaining, 0.05))

    def _set_paused(self, run_id: int) -> None:
        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                run = _get_run(connection, run_id)
                if run.state != "running":
                    return
                now = _utc_text(self.clock)
                connection.execute(
                    "UPDATE replay_runs SET state = 'paused', updated_at = ? WHERE id = ?",
                    (now, run_id),
                )
                _record_activity(connection, run_id, "paused", "running", "paused", now)
        finally:
            connection.close()

    def _set_terminal_state(self, run_id: int, state: str) -> None:
        if state not in {"stopped", "interrupted"}:
            raise ReplayError(f"Unsupported requested terminal state: {state}")
        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                run = _get_run(connection, run_id)
                if run.state in TERMINAL_STATES:
                    return
                now = _utc_text(self.clock)
                ended_at = now if state == "stopped" else None
                last_error = (
                    "Replay worker interrupted before completion"
                    if state == "interrupted"
                    else None
                )
                connection.execute(
                    """
                    UPDATE replay_runs
                    SET state = ?, updated_at = ?, ended_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (state, now, ended_at, last_error, run_id),
                )
                _record_activity(connection, run_id, state, run.state, state, now)
        finally:
            connection.close()
        with self._condition:
            self._condition.notify_all()

    def _mark_failed(self, run_id: int, error: BaseException) -> None:
        connection = connect(self.database)
        try:
            with immediate_transaction(connection):
                run = _get_run(connection, run_id)
                if run.state in TERMINAL_STATES:
                    return
                now = _utc_text(self.clock)
                message = f"{type(error).__name__}: {error}"[:1000]
                connection.execute(
                    """
                    UPDATE replay_runs
                    SET state = 'failed', updated_at = ?, ended_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (now, now, message, run_id),
                )
                _record_activity(
                    connection,
                    run_id,
                    "failed",
                    run.state,
                    "failed",
                    now,
                    {"error": message},
                )
        finally:
            connection.close()

    def _worker_main(self, run_id: int) -> None:
        connection = connect(self.database)
        next_due = self.clock.monotonic()
        observed_revision = -1
        try:
            while True:
                with self._condition:
                    command = self._command
                    rate = self._rate
                    revision = self._schedule_revision

                if command in {"stopped", "interrupted"}:
                    self._set_terminal_state(run_id, command)
                    return
                if command == "paused":
                    self._set_paused(run_id)
                    with self._condition:
                        self._condition.notify_all()
                    self._wake.wait()
                    self._wake.clear()
                    next_due = self.clock.monotonic()
                    observed_revision = -1
                    continue

                if revision != observed_revision:
                    if observed_revision >= 0:
                        next_due = self.clock.monotonic() + (1.0 / rate)
                    observed_revision = revision

                wait_seconds = max(0.0, next_due - self.clock.monotonic())
                if wait_seconds > 0 and self.clock.wait(self._wake, wait_seconds):
                    self._wake.clear()
                    continue

                emitted = _emit_next(
                    connection, run_id, self.clock, auto_send_critical=self.auto_send_critical
                )
                if not emitted:
                    with self._condition:
                        self._condition.notify_all()
                    return
                next_due = max(next_due + (1.0 / rate), self.clock.monotonic())
        except BaseException as exc:
            self._mark_failed(run_id, exc)
        finally:
            connection.close()
            with self._condition:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                    self._command = "stopped"
                self._condition.notify_all()

    def close(self, *, timeout: float = 5.0) -> None:
        with self._condition:
            run_id = self._active_run_id
            thread = self._thread
        if run_id is not None and thread is not None and thread.is_alive():
            self.interrupt(run_id, timeout=timeout)
            thread.join(timeout)

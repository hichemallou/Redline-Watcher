"""Streaming JSONL import with validation, provenance, and restart checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from clawwatch_demo.config import AppConfig
from clawwatch_demo.models import ImportProgress, ImportResult, NormalizedEvent
from clawwatch_demo.storage import connect, immediate_transaction, migrate


class ImportFailure(RuntimeError):
    """Raised when dataset import cannot safely continue."""


class EventValidationError(ValueError):
    """Raised when one JSONL record does not satisfy the import contract."""


ProgressCallback = Callable[[ImportProgress], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _exclusive_import_lock(database: Path) -> Iterator[None]:
    lock_path = Path(f"{database}.import.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ImportFailure(f"Another import is active for {database}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{key} must be a non-empty string")
    return value


def _optional_string(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    if not isinstance(value, str):
        raise EventValidationError(f"{key} must be a string or null")
    return value or None


def _normalize_record(record: dict[str, Any], raw_line: str, line_number: int) -> NormalizedEvent:
    if not isinstance(record, dict):
        raise EventValidationError("JSON value must be an object")
    if not isinstance(record.get("advanced_metadata"), dict):
        raise EventValidationError("advanced_metadata must be an object")

    original_timestamp = _required_string(record, "timestamp")
    try:
        parsed = datetime.fromisoformat(original_timestamp)
    except ValueError as exc:
        raise EventValidationError("timestamp must be valid ISO 8601") from exc

    event_type = _required_string(record, "event_type")
    action = _optional_string(record, "action")
    source_ip = _optional_string(record, "src_ip")
    if source_ip == "N/A":
        source_ip = None

    return NormalizedEvent(
        line_number=line_number,
        event_id=_required_string(record, "event_id"),
        original_timestamp=original_timestamp,
        parsed_timestamp=parsed.isoformat(),
        timestamp_has_timezone=parsed.tzinfo is not None,
        event_type=event_type,
        severity=_required_string(record, "severity"),
        actor=_optional_string(record, "user"),
        source_ip=source_ip,
        action=action,
        status=action if event_type == "auth" else None,
        source_product=_required_string(record, "source"),
        message=_required_string(record, "description"),
        raw_log=_required_string(record, "raw_log"),
        original_json=raw_line,
    )


def normalize_event(raw_line: str, line_number: int) -> NormalizedEvent:
    """Normalize one record; used as the resilient fallback for malformed batches."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise EventValidationError(f"invalid JSON: {exc.msg}") from exc
    return _normalize_record(record, raw_line, line_number)


def _normalize_batch_with_pandas(
    batch: list[tuple[int, str]],
) -> tuple[list[NormalizedEvent], list[tuple[int, str, str]]]:
    """Parse a clean JSONL batch with pandas and retain exact source lines.

    If pandas cannot parse the complete batch, fall back to independent records so a
    malformed row is reported without rejecting its valid neighbors.
    """
    payload = "\n".join(original_line for _, original_line in batch)
    try:
        frame = pd.read_json(
            io.StringIO(payload),
            lines=True,
            dtype=False,
            convert_dates=False,
        )
    except ValueError:
        frame = None

    if frame is None or len(frame.index) != len(batch):
        events: list[NormalizedEvent] = []
        errors: list[tuple[int, str, str]] = []
        for line_number, original_line in batch:
            try:
                events.append(normalize_event(original_line, line_number))
            except EventValidationError as exc:
                errors.append((line_number, original_line, str(exc)))
        return events, errors

    events = []
    errors = []
    records = frame.to_dict(orient="records")
    for (line_number, original_line), record in zip(batch, records, strict=True):
        try:
            events.append(_normalize_record(record, original_line, line_number))
        except EventValidationError as exc:
            errors.append((line_number, original_line, str(exc)))
    return events, errors


def _store_import_error(
    connection: sqlite3.Connection,
    dataset_id: int,
    line_number: int,
    category: str,
    message: str,
    original_line: str,
) -> None:
    connection.execute(
        """
        INSERT INTO import_errors(
            dataset_id, line_number, category, message, original_line, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dataset_id, line_number, category, message[:500], original_line, _utc_now()),
    )


def _event_values(dataset_id: int, event: NormalizedEvent) -> tuple[Any, ...]:
    return (
        dataset_id,
        event.line_number,
        event.event_id,
        event.original_timestamp,
        event.parsed_timestamp,
        int(event.timestamp_has_timezone),
        event.event_type,
        event.severity,
        event.actor,
        event.source_ip,
        event.action,
        event.status,
        event.source_product,
        event.message,
        event.raw_log,
        event.original_json,
    )


def _insert_events(
    connection: sqlite3.Connection,
    dataset_id: int,
    events: list[NormalizedEvent],
) -> None:
    connection.executemany(
        """
        INSERT INTO source_events(
            dataset_id, line_number, event_id, original_timestamp, parsed_timestamp,
            timestamp_has_timezone, event_type, severity, actor, source_ip, action,
            status, source_product, message, raw_log, original_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (_event_values(dataset_id, event) for event in events),
    )


def _existing_event_ids(
    connection: sqlite3.Connection,
    dataset_id: int,
    events: list[NormalizedEvent],
) -> set[str]:
    existing: set[str] = set()
    event_ids = [event.event_id for event in events]
    for start in range(0, len(event_ids), 900):
        chunk = event_ids[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT event_id FROM source_events "
            f"WHERE dataset_id = ? AND event_id IN ({placeholders})",
            (dataset_id, *chunk),
        )
        existing.update(str(row[0]) for row in rows)
    return existing


def _commit_batch(
    connection: sqlite3.Connection,
    dataset_id: int,
    batch: list[tuple[int, str]],
) -> tuple[int, int, int]:
    events, validation_errors = _normalize_batch_with_pandas(batch)
    rejected = len(validation_errors)
    with immediate_transaction(connection):
        for line_number, original_line, message in validation_errors:
            _store_import_error(
                connection,
                dataset_id,
                line_number,
                "invalid_record",
                message,
                original_line,
            )

        seen_ids = _existing_event_ids(connection, dataset_id, events)
        accepted_events: list[NormalizedEvent] = []
        duplicate_events: list[NormalizedEvent] = []
        for event in events:
            if event.event_id in seen_ids:
                duplicate_events.append(event)
                continue
            seen_ids.add(event.event_id)
            accepted_events.append(event)

        _insert_events(connection, dataset_id, accepted_events)
        for event in duplicate_events:
            _store_import_error(
                connection,
                dataset_id,
                event.line_number,
                "duplicate_event_id",
                f"duplicate event_id: {event.event_id}",
                event.original_json,
            )

        accepted = len(accepted_events)
        duplicates = len(duplicate_events)

        connection.execute(
            """
            UPDATE datasets
            SET committed_line_cursor = ?,
                accepted_count = accepted_count + ?,
                rejected_count = rejected_count + ?,
                duplicate_count = duplicate_count + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (batch[-1][0], accepted, rejected, duplicates, _utc_now(), dataset_id),
        )
    return accepted, rejected, duplicates


def _mark_failed(connection: sqlite3.Connection, dataset_id: int, error: BaseException) -> None:
    try:
        with immediate_transaction(connection):
            connection.execute(
                """
                UPDATE datasets
                SET status = 'failed', updated_at = ?, last_error = ?
                WHERE id = ? AND status != 'complete'
                """,
                (_utc_now(), f"{type(error).__name__}: {error}"[:1000], dataset_id),
            )
    except sqlite3.Error:
        pass


def _result_from_row(
    row: sqlite3.Row,
    *,
    resumed_from_line: int,
    already_complete: bool,
    elapsed_seconds: float,
) -> ImportResult:
    return ImportResult(
        dataset_id=int(row["id"]),
        file_sha256=str(row["file_sha256"]),
        file_bytes=int(row["file_bytes"]),
        total_lines=int(row["total_lines"] or row["committed_line_cursor"]),
        accepted_count=int(row["accepted_count"]),
        rejected_count=int(row["rejected_count"]),
        duplicate_count=int(row["duplicate_count"]),
        resumed_from_line=resumed_from_line,
        already_complete=already_complete,
        elapsed_seconds=elapsed_seconds,
    )


def import_dataset(
    config: AppConfig,
    *,
    on_progress: ProgressCallback | None = None,
) -> ImportResult:
    """Import the configured JSONL file, resuming from committed batches."""
    started = time.monotonic()
    source = config.data.source
    if not source.is_file():
        raise ImportFailure(f"Dataset file does not exist: {source}")

    with _exclusive_import_lock(config.storage.database):
        digest = sha256_file(source)
        if digest != config.data.sha256:
            raise ImportFailure(
                f"Dataset checksum mismatch: expected {config.data.sha256}, got {digest}"
            )

        migrate(config.storage.database)
        connection = connect(config.storage.database)
        dataset_id: int | None = None
        try:
            now = _utc_now()
            with immediate_transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM datasets WHERE file_sha256 = ?", (digest,)
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO datasets(
                            repository_id, revision, file_sha256, source_path, file_bytes,
                            status, started_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'importing', ?, ?)
                        """,
                        (
                            config.data.repository,
                            config.data.revision,
                            digest,
                            str(source),
                            source.stat().st_size,
                            now,
                            now,
                        ),
                    )
                    dataset_id = int(cursor.lastrowid)
                    existing = connection.execute(
                        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
                    ).fetchone()
                elif existing["status"] != "complete":
                    dataset_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE datasets
                        SET repository_id = ?, revision = ?, source_path = ?, file_bytes = ?,
                            status = 'importing', updated_at = ?, last_error = NULL
                        WHERE id = ?
                        """,
                        (
                            config.data.repository,
                            config.data.revision,
                            str(source),
                            source.stat().st_size,
                            now,
                            dataset_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
                    ).fetchone()

            if existing is None:
                raise ImportFailure("Failed to register dataset")
            dataset_id = int(existing["id"])
            resumed_from = int(existing["committed_line_cursor"])
            if existing["status"] == "complete":
                return _result_from_row(
                    existing,
                    resumed_from_line=resumed_from,
                    already_complete=True,
                    elapsed_seconds=time.monotonic() - started,
                )

            accepted = int(existing["accepted_count"])
            rejected = int(existing["rejected_count"])
            duplicates = int(existing["duplicate_count"])
            batch: list[tuple[int, str]] = []
            last_line = resumed_from

            with source.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line_number <= resumed_from:
                        continue
                    original_line = line.rstrip("\r\n")
                    batch.append((line_number, original_line))
                    if len(batch) < config.importer.batch_size:
                        continue
                    added, invalid, repeated = _commit_batch(connection, dataset_id, batch)
                    accepted += added
                    rejected += invalid
                    duplicates += repeated
                    last_line = batch[-1][0]
                    batch.clear()
                    if on_progress:
                        on_progress(
                            ImportProgress(dataset_id, last_line, accepted, rejected, duplicates)
                        )

            if batch:
                added, invalid, repeated = _commit_batch(connection, dataset_id, batch)
                accepted += added
                rejected += invalid
                duplicates += repeated
                last_line = batch[-1][0]
                if on_progress:
                    on_progress(
                        ImportProgress(dataset_id, last_line, accepted, rejected, duplicates)
                    )

            completed = _utc_now()
            with immediate_transaction(connection):
                connection.execute(
                    """
                    UPDATE datasets
                    SET status = 'complete', total_lines = ?, completed_at = ?,
                        updated_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (last_line, completed, completed, dataset_id),
                )
            final_row = connection.execute(
                "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
            if final_row is None:
                raise ImportFailure("Completed dataset record disappeared")
            return _result_from_row(
                final_row,
                resumed_from_line=resumed_from,
                already_complete=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except BaseException as exc:
            if dataset_id is not None:
                _mark_failed(connection, dataset_id, exc)
            raise
        finally:
            connection.close()

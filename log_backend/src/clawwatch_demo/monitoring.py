"""Bounded pandas-backed dashboard queries over committed replay data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from clawwatch_demo.storage import connect

FEED_COLUMNS = [
    "replay_event_id",
    "sequence",
    "emitted_at",
    "severity",
    "event_type",
    "actor",
    "source_ip",
    "message",
]


@dataclass(frozen=True)
class DashboardFilters:
    event_types: tuple[str, ...] = ()
    severities: tuple[str, ...] = ()
    search: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    run_id: int | None
    state: str
    rate: float
    emitted_count: int
    actual_rate: float
    high_annotation_count: int
    open_review_count: int
    timeline: pd.DataFrame
    severity: pd.DataFrame
    categories: pd.DataFrame
    feed: pd.DataFrame
    updated_at: str
    last_error: str | None


def _empty_snapshot() -> DashboardSnapshot:
    return DashboardSnapshot(
        run_id=None,
        state="no runs",
        rate=0.0,
        emitted_count=0,
        actual_rate=0.0,
        high_annotation_count=0,
        open_review_count=0,
        timeline=pd.DataFrame(columns=["time", "events"]),
        severity=pd.DataFrame(columns=["severity", "events"]),
        categories=pd.DataFrame(columns=["event_type", "events"]),
        feed=pd.DataFrame(columns=FEED_COLUMNS),
        updated_at=datetime.now(UTC).isoformat(),
        last_error=None,
    )


def list_runs(database: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    connection = connect(database, read_only=True)
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, state, rate, emitted_count, record_limit, started_at, updated_at
                FROM replay_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
    finally:
        connection.close()


def filter_options(database: Path) -> tuple[list[str], list[str]]:
    connection = connect(database, read_only=True)
    try:
        event_types = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT event_type FROM source_events ORDER BY event_type"
            )
        ]
        severities = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT severity FROM source_events ORDER BY severity"
            )
        ]
        return event_types, severities
    finally:
        connection.close()


def _where_clause(filters: DashboardFilters) -> tuple[str, list[object]]:
    clauses = ["re.run_id = ?"]
    parameters: list[object] = []
    if filters.event_types:
        placeholders = ",".join("?" for _ in filters.event_types)
        clauses.append(f"se.event_type IN ({placeholders})")
        parameters.extend(filters.event_types)
    if filters.severities:
        placeholders = ",".join("?" for _ in filters.severities)
        clauses.append(f"se.severity IN ({placeholders})")
        parameters.extend(filters.severities)
    if filters.search.strip():
        literal = (
            filters.search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{literal}%"
        clauses.append(
            "("
            + " OR ".join(
                f"COALESCE({column}, '') LIKE ? ESCAPE '\\'"
                for column in (
                    "se.event_id",
                    "se.message",
                    "se.raw_log",
                    "se.actor",
                    "se.source_ip",
                )
            )
            + ")"
        )
        parameters.extend([pattern] * 5)
    return " AND ".join(clauses), parameters


def dashboard_snapshot(
    database: Path,
    *,
    run_id: int | None = None,
    filters: DashboardFilters | None = None,
    feed_limit: int = 200,
) -> DashboardSnapshot:
    if feed_limit <= 0 or feed_limit > 1_000:
        raise ValueError("feed_limit must be between 1 and 1,000")
    active_filters = filters or DashboardFilters()
    now = datetime.now(UTC)
    connection = connect(database, read_only=True)
    try:
        connection.execute("BEGIN")
        if run_id is None:
            run_row = connection.execute(
                "SELECT * FROM replay_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            run_row = connection.execute(
                "SELECT * FROM replay_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if run_row is None:
            connection.rollback()
            return _empty_snapshot()

        selected_run_id = int(run_row["id"])
        where, filter_parameters = _where_clause(active_filters)
        base_parameters: list[object] = [selected_run_id, *filter_parameters]
        feed = pd.read_sql_query(
            f"""
            SELECT re.id AS replay_event_id, re.sequence, re.emitted_at,
                   se.severity, se.event_type, COALESCE(se.actor, '') AS actor,
                   COALESCE(se.source_ip, '') AS source_ip, se.message
            FROM replay_events AS re
            JOIN source_events AS se ON se.id = re.source_event_id
            WHERE {where}
            ORDER BY re.sequence DESC
            LIMIT ?
            """,
            connection,
            params=(*base_parameters, feed_limit),
        )
        feed = feed.reindex(columns=FEED_COLUMNS)

        severity = pd.read_sql_query(
            f"""
            SELECT se.severity, COUNT(*) AS events
            FROM replay_events AS re
            JOIN source_events AS se ON se.id = re.source_event_id
            WHERE {where}
            GROUP BY se.severity
            ORDER BY events DESC, se.severity
            """,
            connection,
            params=base_parameters,
        )
        categories = pd.read_sql_query(
            f"""
            SELECT se.event_type, COUNT(*) AS events
            FROM replay_events AS re
            JOIN source_events AS se ON se.id = re.source_event_id
            WHERE {where}
            GROUP BY se.event_type
            ORDER BY events DESC, se.event_type
            """,
            connection,
            params=base_parameters,
        )

        timeline_start = (now - timedelta(minutes=5)).isoformat()
        timeline_raw = pd.read_sql_query(
            """
            SELECT re.emitted_at
            FROM replay_events AS re
            WHERE re.run_id = ? AND re.emitted_at >= ?
            ORDER BY re.emitted_at
            """,
            connection,
            params=(selected_run_id, timeline_start),
        )
        if timeline_raw.empty:
            timeline = pd.DataFrame(columns=["time", "events"])
        else:
            timeline_raw["time"] = pd.to_datetime(
                timeline_raw["emitted_at"], utc=True, errors="coerce"
            ).dt.floor("s")
            timeline = (
                timeline_raw.dropna(subset=["time"])
                .groupby("time", as_index=False)
                .size()
                .rename(columns={"size": "events"})
            )

        high_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM replay_events AS re
                JOIN source_events AS se ON se.id = re.source_event_id
                WHERE re.run_id = ?
                  AND lower(se.severity) IN ('high', 'critical', 'emergency')
                """,
                (selected_run_id,),
            ).fetchone()[0]
        )
        open_reviews = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM review_cards AS rc
                JOIN replay_events AS re ON re.id = rc.replay_event_id
                WHERE re.run_id = ? AND rc.stage IN ('new', 'investigating')
                """,
                (selected_run_id,),
            ).fetchone()[0]
        )
        rate_start = (now - timedelta(seconds=10)).isoformat()
        recent_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM replay_events
                WHERE run_id = ? AND emitted_at >= ?
                """,
                (selected_run_id, rate_start),
            ).fetchone()[0]
        )
        connection.commit()

        started_at = run_row["started_at"]
        elapsed_window = 10.0
        if started_at:
            try:
                started = datetime.fromisoformat(str(started_at))
                elapsed_window = min(10.0, max((now - started).total_seconds(), 0.1))
            except ValueError:
                pass
        return DashboardSnapshot(
            run_id=selected_run_id,
            state=str(run_row["state"]),
            rate=float(run_row["rate"]),
            emitted_count=int(run_row["emitted_count"]),
            actual_rate=recent_count / elapsed_window,
            high_annotation_count=high_count,
            open_review_count=open_reviews,
            timeline=timeline,
            severity=severity,
            categories=categories,
            feed=feed,
            updated_at=str(run_row["updated_at"]),
            last_error=None if run_row["last_error"] is None else str(run_row["last_error"]),
        )
    finally:
        connection.close()


def event_detail(database: Path, replay_event_id: int) -> dict[str, Any]:
    connection = connect(database, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT re.id AS replay_event_id, re.run_id, re.sequence,
                   re.simulated_at, re.emitted_at, se.event_id, se.original_timestamp,
                   se.parsed_timestamp, se.timestamp_has_timezone, se.event_type,
                   se.severity, se.actor, se.source_ip, se.action, se.status,
                   se.source_product, se.message, se.raw_log, se.original_json,
                   ds.repository_id, ds.revision, ds.file_sha256
            FROM replay_events AS re
            JOIN source_events AS se ON se.id = re.source_event_id
            JOIN datasets AS ds ON ds.id = se.dataset_id
            WHERE re.id = ?
            """,
            (replay_event_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Replay event does not exist: {replay_event_id}")
        detail = dict(row)
        try:
            detail["original_payload"] = json.loads(str(detail.pop("original_json")))
        except json.JSONDecodeError:
            detail["original_payload"] = {"unparsed": detail.pop("original_json")}
        return detail
    finally:
        connection.close()

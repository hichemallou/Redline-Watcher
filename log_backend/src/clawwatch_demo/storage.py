"""SQLite connections, migrations, and bounded status queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "001_initial.sql"),
    (2, "002_single_active_replay.sql"),
)


def connect(database: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open one configured SQLite connection.

    Connections are intentionally not shared between threads. Callers own and close
    the returned connection.
    """
    database = database.resolve()
    if read_only:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run a short write transaction that reserves the writer immediately."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def migrate(database: Path) -> int:
    """Apply packaged migrations and return the resulting schema version."""
    connection = connect(database)
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version, filename in MIGRATIONS:
            if version <= current:
                continue
            sql = resources.files("clawwatch_demo.migrations").joinpath(filename).read_text()
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
            current = version
        return current
    finally:
        connection.close()


def database_summary(database: Path) -> dict[str, Any]:
    """Return compact database and import status for CLI/UI use."""
    database = database.resolve()
    if not database.is_file():
        return {
            "database": str(database),
            "exists": False,
            "schema_version": 0,
            "datasets": [],
            "source_event_count": 0,
            "import_error_count": 0,
            "replay_runs": [],
            "replay_event_count": 0,
        }

    connection = connect(database, read_only=True)
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version == 0:
            return {
                "database": str(database),
                "exists": True,
                "schema_version": 0,
                "datasets": [],
                "source_event_count": 0,
                "import_error_count": 0,
                "replay_runs": [],
                "replay_event_count": 0,
            }
        datasets = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, repository_id, revision, file_sha256, source_path, file_bytes,
                       status, committed_line_cursor, total_lines, accepted_count,
                       rejected_count, duplicate_count, started_at, updated_at,
                       completed_at, last_error
                FROM datasets
                ORDER BY id
                """
            )
        ]
        source_count = int(connection.execute("SELECT COUNT(*) FROM source_events").fetchone()[0])
        error_count = int(connection.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0])
        replay_runs = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, dataset_id, selection_json, ordering, record_limit, rate,
                       state, committed_selection_cursor, emitted_count, started_at,
                       updated_at, ended_at, last_error
                FROM replay_runs
                ORDER BY id
                """
            )
        ]
        replay_event_count = int(
            connection.execute("SELECT COUNT(*) FROM replay_events").fetchone()[0]
        )
        return {
            "database": str(database),
            "exists": True,
            "schema_version": schema_version,
            "datasets": datasets,
            "source_event_count": source_count,
            "import_error_count": error_count,
            "replay_runs": replay_runs,
            "replay_event_count": replay_event_count,
        }
    finally:
        connection.close()

import sqlite3
from pathlib import Path

from clawwatch_demo.storage import database_summary, migrate


def test_migration_creates_versioned_schema(tmp_path: Path) -> None:
    database = tmp_path / "demo.sqlite3"

    assert migrate(database) == 2
    assert migrate(database) == 2

    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert {
        "datasets",
        "source_events",
        "import_errors",
        "replay_runs",
        "replay_events",
        "review_cards",
        "activity_log",
    } <= tables
    assert journal_mode == "wal"

    summary = database_summary(database)
    assert summary["schema_version"] == 2
    assert summary["source_event_count"] == 0


def test_summary_handles_existing_uninitialized_database(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite3"
    sqlite3.connect(database).close()

    summary = database_summary(database)

    assert summary["exists"] is True
    assert summary["schema_version"] == 0
    assert summary["datasets"] == []

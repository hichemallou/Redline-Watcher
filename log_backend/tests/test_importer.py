import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from clawwatch_demo import importer
from clawwatch_demo.config import (
    AppConfig,
    DataConfig,
    ImporterConfig,
    ReplayConfig,
    ServerConfig,
    StorageConfig,
)
from clawwatch_demo.importer import ImportFailure, import_dataset, sha256_file
from clawwatch_demo.storage import database_summary

FIXTURE = Path(__file__).parent / "fixtures/events.jsonl"


def make_config(tmp_path: Path, *, batch_size: int = 2) -> AppConfig:
    source = tmp_path / "events.jsonl"
    shutil.copyfile(FIXTURE, source)
    return AppConfig(
        project_root=tmp_path,
        config_path=tmp_path / "demo.toml",
        data=DataConfig(
            source=source,
            repository="test/events",
            revision="fixture",
            sha256=sha256_file(source),
        ),
        storage=StorageConfig(database=tmp_path / "var/demo.sqlite3"),
        importer=ImporterConfig(batch_size=batch_size, progress_every=1),
        server=ServerConfig(host="127.0.0.1", port=7860, share=False),
        replay=ReplayConfig(
            default_preset="all",
            default_limit=1000,
            default_rate=10.0,
            available_rates=(10.0,),
            refresh_seconds=1.0,
            display_timezone="America/New_York",
        ),
    )


def test_import_normalizes_and_preserves_records_idempotently(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    result = import_dataset(config)
    repeated = import_dataset(config)

    assert result.total_lines == 4
    assert result.accepted_count == 2
    assert result.rejected_count == 1
    assert result.duplicate_count == 1
    assert repeated.already_complete is True
    assert repeated.accepted_count == 2

    connection = sqlite3.connect(config.storage.database)
    connection.row_factory = sqlite3.Row
    try:
        auth = connection.execute("SELECT * FROM source_events WHERE event_id = 'evt-1'").fetchone()
        errors = connection.execute(
            "SELECT category FROM import_errors ORDER BY line_number"
        ).fetchall()
    finally:
        connection.close()

    assert auth["source_ip"] is None
    assert auth["status"] == "failed"
    assert auth["timestamp_has_timezone"] == 0
    assert '"src_ip":"N/A"' in auth["original_json"]
    assert [row["category"] for row in errors] == [
        "invalid_record",
        "duplicate_event_id",
    ]
    assert database_summary(config.storage.database)["source_event_count"] == 2


def test_import_processes_batches_with_pandas(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    read_json = importer.pd.read_json
    calls = 0

    def observed_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return read_json(*args, **kwargs)

    monkeypatch.setattr(importer.pd, "read_json", observed_read_json)

    result = import_dataset(config)

    assert calls == 2
    assert result.accepted_count == 2


def test_import_resumes_after_last_committed_batch(tmp_path: Path) -> None:
    config = make_config(tmp_path, batch_size=1)

    def interrupt_after_first_batch(progress) -> None:
        if progress.committed_line_cursor == 1:
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        import_dataset(config, on_progress=interrupt_after_first_batch)

    failed = database_summary(config.storage.database)["datasets"][0]
    assert failed["status"] == "failed"
    assert failed["committed_line_cursor"] == 1

    resumed = import_dataset(config)
    assert resumed.resumed_from_line == 1
    assert resumed.total_lines == 4
    assert resumed.accepted_count == 2
    assert resumed.rejected_count == 1
    assert resumed.duplicate_count == 1


def test_checksum_mismatch_is_rejected_before_database_creation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(config, data=replace(config.data, sha256="0" * 64))

    with pytest.raises(ImportFailure, match="checksum mismatch"):
        import_dataset(config)

    assert not config.storage.database.exists()

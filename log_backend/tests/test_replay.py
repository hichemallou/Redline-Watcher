import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawwatch_demo import replay
from clawwatch_demo.config import (
    AppConfig,
    DataConfig,
    ImporterConfig,
    ReplayConfig,
    ServerConfig,
    StorageConfig,
)
from clawwatch_demo.importer import import_dataset, sha256_file
from clawwatch_demo.replay import (
    ReplayConflict,
    ReplayController,
    ReplayStateError,
    recover_interrupted_runs,
)
from clawwatch_demo.storage import connect, immediate_transaction


class AdvancingClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def utcnow(self) -> datetime:
        return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=self.current)

    def wait(self, event, timeout: float) -> bool:
        if event.is_set():
            return True
        self.waits.append(timeout)
        self.current += timeout
        return False


def make_database(tmp_path: Path, *, event_count: int = 12) -> Path:
    source = tmp_path / "events.jsonl"
    rows = []
    for index in range(event_count):
        timestamp = datetime(2025, 1, 1) + timedelta(seconds=event_count - index)
        rows.append(
            json.dumps(
                {
                    "event_id": f"evt-{index + 1}",
                    "timestamp": timestamp.isoformat(),
                    "event_type": "auth" if index % 2 == 0 else "endpoint",
                    "source": "Replay test SIEM",
                    "severity": "high" if index % 3 == 0 else "low",
                    "raw_log": f"raw event {index + 1}",
                    "advanced_metadata": {},
                    "user": f"user-{index % 4}",
                    "action": "failed" if index % 2 == 0 else "process_start",
                    "src_ip": f"192.0.2.{index + 1}",
                    "description": f"Replay event {index + 1}",
                },
                separators=(",", ":"),
            )
        )
    source.write_text("\n".join(rows) + "\n")
    database = tmp_path / "var/replay.sqlite3"
    config = AppConfig(
        project_root=tmp_path,
        config_path=tmp_path / "demo.toml",
        data=DataConfig(
            source=source,
            repository="test/replay-events",
            revision="fixture",
            sha256=sha256_file(source),
        ),
        storage=StorageConfig(database=database),
        importer=ImporterConfig(batch_size=10, progress_every=10),
        server=ServerConfig(host="127.0.0.1", port=7860, share=False),
        replay=ReplayConfig(
            default_preset="all",
            default_limit=1_000,
            default_rate=10.0,
            available_rates=(10.0,),
            refresh_seconds=1.0,
            display_timezone="America/New_York",
        ),
    )
    result = import_dataset(config)
    assert result.accepted_count == event_count
    return database


def wait_for_emissions(
    controller: ReplayController,
    run_id: int,
    minimum: int,
    *,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while controller.get_run(run_id).emitted_count < minimum:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Run {run_id} did not emit {minimum} events")
        time.sleep(0.005)


def replay_rows(database: Path, run_id: int) -> list[sqlite3.Row]:
    connection = connect(database, read_only=True)
    try:
        return connection.execute(
            """
            SELECT re.sequence, se.event_id, se.event_type
            FROM replay_events AS re
            JOIN source_events AS se ON se.id = re.source_event_id
            WHERE re.run_id = ?
            ORDER BY re.sequence
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()


def test_replay_emits_filtered_records_once_and_completes(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=8)
    controller = ReplayController(database)
    run = controller.create_run(preset="authentication", record_limit=None, rate=1_000)

    controller.start(run.id)
    completed = controller.wait_for_state(run.id, {"completed"})
    rows = replay_rows(database, run.id)

    assert completed.emitted_count == 4
    assert completed.committed_selection_cursor == 4
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert {row["event_type"] for row in rows} == {"auth"}
    assert len({row["event_id"] for row in rows}) == 4
    with pytest.raises(ReplayStateError, match="while it is completed"):
        controller.start(run.id)

    connection = connect(database, read_only=True)
    try:
        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM activity_log WHERE run_id = ? ORDER BY id", (run.id,)
            )
        ]
    finally:
        connection.close()
    assert actions == ["created", "started", "completed"]


def test_replay_uses_fixed_rate_and_original_time_order(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=4)
    clock = AdvancingClock()
    controller = ReplayController(database, clock=clock)
    run = controller.create_run(ordering="original_time", record_limit=None, rate=25)

    controller.start(run.id)
    controller.wait_for_state(run.id, {"completed"})
    rows = replay_rows(database, run.id)

    assert [row["event_id"] for row in rows] == ["evt-4", "evt-3", "evt-2", "evt-1"]
    assert clock.waits[:3] == pytest.approx([0.04, 0.04, 0.04])


def test_pause_rate_resume_and_stop_are_acknowledged(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=100)
    controller = ReplayController(database)
    run = controller.create_run(record_limit=None, rate=50)
    controller.start(run.id)
    wait_for_emissions(controller, run.id, 2)

    paused = controller.pause(run.id)
    paused_count = paused.emitted_count
    time.sleep(0.06)
    assert controller.get_run(run.id).emitted_count == paused_count

    changed = controller.set_rate(run.id, 100)
    assert changed.rate == 100
    controller.resume(run.id)
    wait_for_emissions(controller, run.id, paused_count + 3)
    stopped = controller.stop(run.id)
    stopped_count = stopped.emitted_count
    time.sleep(0.04)

    assert controller.get_run(run.id).emitted_count == stopped_count
    rows = replay_rows(database, run.id)
    assert [row["sequence"] for row in rows] == list(range(1, stopped_count + 1))


def test_recovery_resumes_from_committed_cursor_without_duplicates(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=5)
    controller = ReplayController(database)
    run = controller.create_run(record_limit=None, rate=1_000)

    connection = connect(database)
    try:
        with immediate_transaction(connection):
            source_id = int(
                connection.execute(
                    "SELECT id FROM source_events ORDER BY line_number LIMIT 1"
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE replay_runs
                SET state = 'running', started_at = '2025-01-01T00:00:00+00:00',
                    committed_selection_cursor = 1, emitted_count = 1
                WHERE id = ?
                """,
                (run.id,),
            )
            connection.execute(
                """
                INSERT INTO replay_events(
                    run_id, sequence, source_event_id, simulated_at, emitted_at
                ) VALUES (?, 1, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
                """,
                (run.id, source_id),
            )
    finally:
        connection.close()

    assert recover_interrupted_runs(database) == (run.id,)
    assert controller.get_run(run.id).state == "interrupted"
    controller.resume(run.id)
    completed = controller.wait_for_state(run.id, {"completed"})
    rows = replay_rows(database, run.id)

    assert completed.emitted_count == 5
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4, 5]
    assert len({row["event_id"] for row in rows}) == 5


def test_database_rejects_a_second_active_run(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    first = ReplayController(database)
    second = ReplayController(database)
    run = first.create_run()

    with pytest.raises(ReplayConflict, match=f"Replay run {run.id} is still ready"):
        second.create_run()

    stopped = first.stop(run.id)
    assert stopped.state == "stopped"


def test_worker_failure_does_not_advance_cursor(tmp_path: Path, monkeypatch) -> None:
    database = make_database(tmp_path)
    controller = ReplayController(database)
    run = controller.create_run(record_limit=3, rate=1_000)

    def fail_emit(*args, **kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr(replay, "_emit_next", fail_emit)
    controller.start(run.id)
    failed = controller.wait_for_state(run.id, {"failed"})

    assert failed.committed_selection_cursor == 0
    assert failed.emitted_count == 0
    assert "simulated write failure" in (failed.last_error or "")
    assert replay_rows(database, run.id) == []

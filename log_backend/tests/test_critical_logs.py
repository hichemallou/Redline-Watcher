import fcntl
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_replay import make_database

from clawwatch_demo.critical_logs import CriticalLogNotifier
from clawwatch_demo.replay import ReplayController
from clawwatch_demo.storage import connect

SENDER = Path(__file__).resolve().parents[1] / "scripts/remote/send_critical_log.py"


def prepare_replay(tmp_path, *, enabled=True):
    database = make_database(tmp_path, event_count=3)
    connection = connect(database)
    raw = '{ "severity": " CRITICAL ", "message": "完整 $(never-run)", "extra": [1, 2] }'
    with connection:
        connection.execute(
            "UPDATE source_events SET severity = ' CRITICAL ', original_json = ? "
            "WHERE line_number = 2",
            (raw,),
        )
    connection.close()
    controller = ReplayController(database, auto_send_critical=enabled)
    run = controller.create_run(record_limit=None, rate=1000)
    controller.start(run.id)
    controller.wait_for_state(run.id, {"completed"})
    controller.close()
    return database, raw


def queue_rows(database):
    connection = connect(database, read_only=True)
    try:
        return connection.execute("SELECT * FROM critical_log_outbox").fetchall()
    finally:
        connection.close()


def test_replay_auto_delivery_preserves_payload_and_survives_restart(tmp_path, monkeypatch):
    database, raw = prepare_replay(tmp_path)
    assert len(queue_rows(database)) == 1
    calls = []

    def send(command, **kwargs):
        calls.append((command, kwargs))
        # Sending must not hold a write transaction or block replay writes.
        connection = connect(database)
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        connection.close()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", send)
    assert CriticalLogNotifier(database, SENDER).check() == 1
    assert calls[0][1]["input"] == raw
    assert "shell" not in calls[0][1]
    assert calls[0][0][1:] == [str(SENDER), "--severity", "critical"]
    assert CriticalLogNotifier(database, SENDER).check() == 0
    assert len(calls) == 1
    assert queue_rows(database)[0]["sent_at"] is not None


def test_local_replay_does_not_queue_or_send(tmp_path, monkeypatch):
    database, _ = prepare_replay(tmp_path, enabled=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("local send"))
    assert queue_rows(database) == []
    assert CriticalLogNotifier(database, SENDER).check() == 0


@pytest.mark.parametrize("failure", [1, OSError("private"), subprocess.TimeoutExpired("x", 95)])
def test_failures_persist_and_retry_without_leaking_output(tmp_path, monkeypatch, failure):
    database, _ = prepare_replay(tmp_path)

    def fail(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return SimpleNamespace(returncode=failure, stderr="private", stdout="private")

    monkeypatch.setattr(subprocess, "run", fail)
    notifier = CriticalLogNotifier(database, SENDER)
    assert notifier.check() == 0
    row = queue_rows(database)[0]
    assert row["attempts"] == 1
    assert row["sent_at"] is None
    assert "private" not in row["last_error"]
    assert notifier.check() == 0
    assert queue_rows(database)[0]["attempts"] == 1
    connection = connect(database)
    with connection:
        connection.execute("UPDATE critical_log_outbox SET next_attempt_at = 0")
    connection.close()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    assert CriticalLogNotifier(database, SENDER).check() == 1
    assert queue_rows(database)[0]["attempts"] == 2


def test_concurrent_dispatcher_skips_locked_queue(tmp_path, monkeypatch):
    database, _ = prepare_replay(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("concurrent send"))
    with Path(f"{database}.critical-logs.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert CriticalLogNotifier(database, SENDER).check() == 0
    assert queue_rows(database)[0]["attempts"] == 0


def test_worker_automatically_drains_replay_queue(tmp_path, monkeypatch):
    database, _ = prepare_replay(tmp_path)
    delivered = threading.Event()

    def send(*args, **kwargs):
        delivered.set()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", send)
    notifier = CriticalLogNotifier(database, SENDER)
    notifier.start()
    try:
        assert delivered.wait(3)
    finally:
        notifier.close()
    assert queue_rows(database)[0]["sent_at"] is not None
    connection = connect(database, read_only=True)
    try:
        row = connection.execute(
            "SELECT detail_json FROM activity_log WHERE action = 'critical_log_sent'"
        ).fetchone()
        assert json.loads(row[0])["replay_event_id"] == queue_rows(database)[0]["replay_event_id"]
    finally:
        connection.close()


def test_replay_delivers_through_real_sender_to_fake_nemoclaw(tmp_path, monkeypatch):
    database, raw = prepare_replay(tmp_path)
    output = tmp_path / "captured.json"
    executable = tmp_path / "nemoclaw"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps("
        "{'args': sys.argv[1:], 'port': os.environ['NEMOCLAW_GATEWAY_PORT']}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("NEMOCLAW_BIN", str(executable))
    assert CriticalLogNotifier(database, SENDER).check() == 1
    captured = json.loads(output.read_text())
    assert captured["port"] == "8991"
    assert captured["args"] == [
        "redline-watcher-3",
        "exec",
        "--",
        "openclaw",
        "message",
        "send",
        "--channel",
        "slack",
        "--target",
        "channel:cyber-alerts",
        "--message",
        "[CRITICAL] ClawWatch log\n" + raw,
    ]

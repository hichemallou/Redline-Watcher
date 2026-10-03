import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def remote_script(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/remote/send_critical_log.py"
    spec = importlib.util.spec_from_file_location("remote_critical_log", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_critical_record_sends_complete_json_as_one_argument(remote_script, monkeypatch):
    record = {
        "severity": " CRITICAL ",
        "message": "'quoted' $(never-run) `never-run`\n完整",
        "details": {"ids": [1, 2], "present": False},
    }
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remote_script.subprocess, "run", run)
    raw_record = json.dumps(record, ensure_ascii=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw_record))
    assert remote_script.main([]) == 0

    command = captured["command"]
    assert command[:4] == ["nemoclaw", "redline-watcher-6", "exec", "--"]
    assert command[4:10] == ["openclaw", "message", "send", "--channel", "slack", "--target"]
    assert command[10] == "channel:cyber-alerts"
    message = command[12]
    assert message.split("\n", 1)[1] == raw_record
    assert captured["kwargs"]["env"]["NEMOCLAW_GATEWAY_PORT"] == "8991"
    assert "shell" not in captured["kwargs"]


def test_noncritical_records_are_skipped(remote_script, monkeypatch, capsys):
    monkeypatch.setattr(
        remote_script.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("NemoClaw should not run"),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO('{"log":{"level":"high"},"message":"x"}'))
    assert remote_script.main([]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "not-critical"


def test_override_and_jsonl_send_only_critical_records(remote_script, tmp_path, monkeypatch):
    source = tmp_path / "events.jsonl"
    source.write_text(
        '{"severity":"low","message":"one"}\n{"severity":"critical","message":"two"}\n',
        encoding="utf-8",
    )
    messages = []
    monkeypatch.setattr(
        remote_script.subprocess,
        "run",
        lambda command, **kwargs: (
            messages.append(command[-1]) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    assert remote_script.main(["--jsonl", "--file", str(source)]) == 0
    assert len(messages) == 1
    assert json.loads(messages[0].split("\n", 1)[1])["message"] == "two"

    monkeypatch.setattr("sys.stdin", io.StringIO('{"message":"forced"}'))
    assert remote_script.main(["--severity", "critical"]) == 0
    assert len(messages) == 2


def test_dry_run_does_not_call_nemoclaw(remote_script, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"severity":"critical","message":"test"}'))
    monkeypatch.setattr(
        remote_script.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("dry-run launched NemoClaw"),
    )
    assert remote_script.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["message_characters"] > 0


@pytest.mark.parametrize(
    "error",
    [
        SimpleNamespace(returncode=1, stdout="secret", stderr="full log"),
        subprocess.TimeoutExpired("nemoclaw", 90),
    ],
)
def test_failures_are_nonzero_without_leaking_output(remote_script, monkeypatch, capsys, error):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"severity":"critical","message":"secret"}'))

    def fail(*args, **kwargs):
        if isinstance(error, BaseException):
            raise error
        return error

    monkeypatch.setattr(remote_script.subprocess, "run", fail)
    assert remote_script.main([]) == 1
    output = capsys.readouterr()
    assert "secret" not in output.err
    assert output.out == ""

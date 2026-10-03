import json
from pathlib import Path
from urllib.error import URLError

import pytest

from clawwatch_demo.slack import (
    SlackError,
    SlackSettings,
    load_slack_settings,
    read_env_file,
    send_slack_message,
)
from clawwatch_demo.slack_cli import main


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_read_env_file_supports_export_and_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Slack settings\nexport SLACK_TOKEN='xoxb-test-token'\nSLACK_CHANNEL_ID=CTEST\n",
        encoding="utf-8",
    )

    assert read_env_file(env_file) == {
        "SLACK_TOKEN": "xoxb-test-token",
        "SLACK_CHANNEL_ID": "CTEST",
    }


def test_process_environment_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SLACK_TOKEN=xoxb-file-token\nSLACK_CHANNEL_ID=CFILE\n",
        encoding="utf-8",
    )

    settings = load_slack_settings(
        env_file,
        environment={"SLACK_TOKEN": "xoxb-process-token", "SLACK_CHANNEL_ID": "CPROCESS"},
    )

    assert settings == SlackSettings(token="xoxb-process-token", channel="CPROCESS")


def test_send_slack_message_posts_expected_payload() -> None:
    captured: dict[str, object] = {}

    def opener(request: object, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"ok": True, "channel": "CTEST", "ts": "123.456"})

    settings = SlackSettings(token="xoxb-test-token", channel="CTEST")
    result = send_slack_message("critical event", settings, opener=opener)

    request = captured["request"]
    assert json.loads(request.data) == {"channel": "CTEST", "text": "critical event"}
    assert request.get_header("Authorization") == "Bearer xoxb-test-token"
    assert captured["timeout"] == 15.0
    assert result.channel == "CTEST"
    assert result.timestamp == "123.456"


def test_send_slack_message_reports_api_error() -> None:
    def opener(*_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse({"ok": False, "error": "not_in_channel"})

    with pytest.raises(SlackError, match="not_in_channel"):
        send_slack_message(
            "critical event",
            SlackSettings(token="xoxb-test-token", channel="CTEST"),
            opener=opener,
        )


def test_send_slack_message_reports_network_error() -> None:
    def opener(*_args: object, **_kwargs: object) -> FakeResponse:
        raise URLError("offline")

    with pytest.raises(SlackError, match="Could not reach Slack: offline"):
        send_slack_message(
            "critical event",
            SlackSettings(token="xoxb-test-token", channel="CTEST"),
            opener=opener,
        )


def test_cli_dry_run_does_not_show_token(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SLACK_TOKEN=xoxb-super-secret-token\nSLACK_CHANNEL_ID=CTEST\n",
        encoding="utf-8",
    )

    result = main(["test message", "--env-file", str(env_file), "--dry-run"])

    output = capsys.readouterr().out
    assert result == 0
    assert "xoxb-super-secret-token" not in output
    assert json.loads(output) == {
        "ok": True,
        "dry_run": True,
        "channel": "CTEST",
        "message_characters": 12,
    }

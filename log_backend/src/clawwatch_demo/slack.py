"""Small Slack Web API client used by the local demo."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SlackError(RuntimeError):
    """Raised when Slack configuration or message delivery fails."""


@dataclass(frozen=True, slots=True)
class SlackSettings:
    token: str
    channel: str


@dataclass(frozen=True, slots=True)
class SlackMessageResult:
    channel: str
    timestamp: str


def read_env_file(path: Path) -> dict[str, str]:
    """Read the simple KEY=VALUE syntax used by the repository .env file."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise SlackError(f"Invalid .env entry at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def load_slack_settings(
    env_file: Path,
    *,
    channel_override: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> SlackSettings:
    """Resolve Slack credentials, preferring process variables over .env."""
    file_values = read_env_file(env_file)
    process_values = os.environ if environment is None else environment
    token = process_values.get("SLACK_TOKEN") or file_values.get("SLACK_TOKEN", "")
    channel = (
        channel_override
        or process_values.get("SLACK_CHANNEL_ID")
        or file_values.get("SLACK_CHANNEL_ID", "")
    )

    if not token:
        raise SlackError(f"SLACK_TOKEN is missing; set it in the environment or {env_file}")
    if not token.startswith("xoxb-"):
        raise SlackError("SLACK_TOKEN must be a Bot User OAuth token beginning with xoxb-")
    if not channel:
        raise SlackError(f"SLACK_CHANNEL_ID is missing; set it in the environment or {env_file}")
    return SlackSettings(token=token, channel=channel)


def _slack_error(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    return fallback


def send_slack_message(
    message: str,
    settings: SlackSettings,
    *,
    timeout: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> SlackMessageResult:
    """Post a plain-text message using Slack's chat.postMessage endpoint."""
    message = message.strip()
    if not message:
        raise SlackError("Message cannot be empty")

    request = Request(
        SLACK_POST_MESSAGE_URL,
        data=json.dumps({"channel": settings.channel, "text": message}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    open_request = urlopen if opener is None else opener
    try:
        with open_request(request, timeout=timeout) as response:
            raw_payload = response.read()
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        raise SlackError(_slack_error(payload, f"Slack returned HTTP {exc.code}")) from exc
    except URLError as exc:
        raise SlackError(f"Could not reach Slack: {exc.reason}") from exc

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlackError("Slack returned an invalid response") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SlackError(_slack_error(payload, "Slack rejected the message"))

    response_channel = payload.get("channel", settings.channel)
    timestamp = payload.get("ts", "")
    return SlackMessageResult(channel=str(response_channel), timestamp=str(timestamp))

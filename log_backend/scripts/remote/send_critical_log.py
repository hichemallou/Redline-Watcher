#!/usr/bin/env python3
"""Send complete critical log records through the local NemoClaw sandbox.

Install and run this script on the remote server that hosts NemoClaw. It never
connects to that server from the ClawWatch machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class SendError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _default_binary() -> str:
    if os.environ.get("NEMOCLAW_BIN"):
        return os.environ["NEMOCLAW_BIN"]
    if shutil.which("nemoclaw"):
        return "nemoclaw"
    candidate = Path.home() / ".local/bin/nemoclaw"
    return str(candidate) if os.access(candidate, os.X_OK) else "nemoclaw"


def _failure_code(output: str) -> str:
    """Classify known diagnostics without exposing log or credential text."""
    output = output.lower()
    for code, markers in (
        ("terminal_required", ("not a tty", "not a terminal", "requires a tty")),
        ("sandbox_not_found", ("sandbox not found", "unknown sandbox", "no sandbox named")),
        ("slack_auth", ("invalid_auth", "not_authed", "token_revoked")),
        ("slack_channel", ("channel_not_found", "not_in_channel")),
        ("slack_scope", ("missing_scope",)),
        ("gateway_connection", ("connection refused", "failed to connect to gateway")),
        ("dependency_missing", ("command not found", "no such file or directory")),
    ):
        if any(marker in output for marker in markers):
            return code
    return "command_failed"


def _severity(record: Mapping[str, Any]) -> str:
    """Read the common top-level or nested severity fields."""
    for key in ("severity", "level"):
        value = record.get(key)
        if isinstance(value, str):
            return value.strip().lower()
    log = record.get("log")
    if isinstance(log, Mapping):
        for key in ("severity", "level"):
            value = log.get(key)
            if isinstance(value, str):
                return value.strip().lower()
    return ""


def _message(raw_record: str) -> str:
    """Preserve the exact JSON text without truncation or summarization."""
    return "[CRITICAL] ClawWatch log\n" + raw_record


def _command(args: argparse.Namespace, message: str) -> list[str]:
    return [
        args.nemoclaw,
        args.sandbox,
        "exec",
        "--",
        "openclaw",
        "message",
        "send",
        "--channel",
        "slack",
        "--target",
        args.target,
        "--message",
        message,
    ]


def send_record(record: Any, raw_record: str, args: argparse.Namespace) -> str:
    if not isinstance(record, Mapping):
        raise ValueError("each log record must be a JSON object")
    severity = args.severity.strip().lower() if args.severity else _severity(record)
    if severity != "critical":
        return "skipped"

    message = _message(raw_record)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "message_characters": len(message),
                    "target": args.target,
                }
            )
        )
        return "dry-run"

    environment = os.environ.copy()
    environment["NEMOCLAW_GATEWAY_PORT"] = str(args.gateway_port)
    # Desktop/services can omit the user-local bin directory from PATH.
    environment["PATH"] = os.pathsep.join(
        [environment.get("PATH", os.defpath), str(Path.home() / ".local/bin")]
    )
    try:
        result = subprocess.run(
            _command(args, message),
            env=environment,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SendError(
            "timeout", "NemoClaw send timed out; Slack delivery is unconfirmed"
        ) from exc
    except FileNotFoundError as exc:
        raise SendError(
            "executable_missing", "NemoClaw executable was not found; set NEMOCLAW_BIN"
        ) from exc
    except PermissionError as exc:
        raise SendError("permission_denied", "Permission denied launching NemoClaw") from exc
    except OSError as exc:
        raise SendError("launch_failed", "Could not launch NemoClaw") from exc

    if result.returncode:
        # Do not print subprocess output because it can contain log or credential data.
        code = _failure_code(result.stderr + "\n" + result.stdout)
        raise SendError(code, f"NemoClaw send failed with exit code {result.returncode} ({code})")
    print(json.dumps({"ok": True, "sent": True, "target": args.target}))
    return "sent"


def _records(args: argparse.Namespace) -> Iterable[tuple[Any, str]]:
    text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not text.strip():
        raise ValueError("provide a JSON log on stdin or with --file")
    if args.jsonl:
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    yield json.loads(line), line
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
    else:
        try:
            yield json.loads(text), text
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send complete critical JSON logs through local NemoClaw to Slack."
    )
    parser.add_argument("--file", type=Path, help="read one JSON object (or JSONL) from a file")
    parser.add_argument("--jsonl", action="store_true", help="process one JSON object per line")
    parser.add_argument(
        "--severity",
        help="override/inject severity when the input has no severity field",
    )
    parser.add_argument("--gateway-port", type=int, default=8991)
    parser.add_argument("--sandbox", default="redline-watcher-6")
    parser.add_argument("--target", default="channel:cyber-alerts")
    parser.add_argument(
        "--nemoclaw",
        default=_default_binary(),
        help="NemoClaw executable (default: nemoclaw or NEMOCLAW_BIN)",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--result-json", action="store_true", help="emit structured failure codes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.gateway_port <= 65535:
        print("gateway port must be between 1 and 65535", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("timeout must be greater than zero", file=sys.stderr)
        return 2
    try:
        outcomes = [send_record(record, raw_record, args) for record, raw_record in _records(args)]
    except (OSError, RuntimeError, ValueError) as exc:
        if args.result_json:
            print(
                json.dumps({"ok": False, "error_code": getattr(exc, "code", "invalid_input")}),
                file=sys.stderr,
            )
        else:
            print(f"Critical log send failed: {exc}", file=sys.stderr)
        return 1
    if outcomes and all(outcome == "skipped" for outcome in outcomes):
        print(json.dumps({"ok": True, "sent": False, "reason": "not-critical"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

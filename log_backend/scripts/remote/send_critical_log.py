#!/usr/bin/env python3
"""Send complete critical log records through the local NemoClaw sandbox.

Install and run this script on the remote server that hosts NemoClaw. It never
connects to that server from the ClawWatch machine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


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
        raise RuntimeError("NemoClaw send timed out; Slack delivery is unconfirmed") from exc
    except OSError as exc:
        raise RuntimeError(f"could not launch NemoClaw: {exc}") from exc

    if result.returncode:
        # Do not print subprocess output because it can contain log or credential data.
        raise RuntimeError(f"NemoClaw send failed with exit code {result.returncode}")
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
    parser.add_argument("--sandbox", default="redline-watcher-3")
    parser.add_argument("--target", default="channel:cyber-alerts")
    parser.add_argument(
        "--nemoclaw",
        default=os.environ.get("NEMOCLAW_BIN", "nemoclaw"),
        help="NemoClaw executable (default: nemoclaw or NEMOCLAW_BIN)",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--dry-run", action="store_true")
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
        print(f"Critical log send failed: {exc}", file=sys.stderr)
        return 1
    if outcomes and all(outcome == "skipped" for outcome in outcomes):
        print(json.dumps({"ok": True, "sent": False, "reason": "not-critical"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

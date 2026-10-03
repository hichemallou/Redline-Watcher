"""Command-line entry point for sending a Slack message."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from clawwatch_demo.slack import SlackError, load_slack_settings, send_slack_message

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="send_slack.sh",
        description="Send a plain-text message to the configured Slack channel.",
    )
    parser.add_argument("message", nargs="?", help="message text; quote text containing spaces")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the message from standard input",
    )
    parser.add_argument(
        "--channel",
        help="override SLACK_CHANNEL_ID for this message",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="environment file (default: repository .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration without contacting Slack",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stdin and args.message is not None:
        parser.error("provide either a message argument or --stdin, not both")
    message = sys.stdin.read() if args.stdin else args.message
    if message is None or not message.strip():
        parser.error("provide a non-empty message argument or use --stdin")

    try:
        settings = load_slack_settings(args.env_file, channel_override=args.channel)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "channel": settings.channel,
                        "message_characters": len(message.strip()),
                    },
                    indent=2,
                )
            )
            return 0
        result = send_slack_message(message, settings)
    except (OSError, SlackError) as exc:
        print(f"Slack message failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"ok": True, "channel": result.channel, "timestamp": result.timestamp},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

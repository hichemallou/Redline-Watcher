"""Script entry point for dispatching critical review alerts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from clawwatch_demo.config import ConfigError, load_config
from clawwatch_demo.review_slack import send_critical_reviews
from clawwatch_demo.slack import SlackError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send unsent open critical review cards to Slack.")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config/demo.toml")
    parser.add_argument("--env-file", type=Path, help="default: project .env")
    parser.add_argument("--dry-run", action="store_true", help="preview card IDs without sending")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, require_dataset=False)
        result = send_critical_reviews(
            config.storage.database,
            args.env_file or config.project_root / ".env",
            dry_run=args.dry_run,
        )
    except (ConfigError, SlackError, OSError, sqlite3.Error) as exc:
        print(f"Critical Slack alerts failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

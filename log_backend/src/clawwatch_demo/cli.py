"""Command-line interface for the ClawWatch demo."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from clawwatch_demo import __version__
from clawwatch_demo.config import AppConfig, ConfigError, load_config
from clawwatch_demo.downloader import DatasetDownloadError, ensure_dataset
from clawwatch_demo.importer import ImportFailure, import_dataset
from clawwatch_demo.models import ImportProgress
from clawwatch_demo.storage import database_summary


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config/demo.toml",
        type=Path,
        help="TOML configuration path (default: config/demo.toml)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawwatch-demo",
        description="Replay and monitor synthetic SIEM logs locally.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("config-check", help="validate configuration")
    _add_config_argument(check_parser)

    import_parser = subparsers.add_parser("import-data", help="import JSONL into SQLite")
    _add_config_argument(import_parser)

    download_parser = subparsers.add_parser(
        "download-data", help="download and verify the configured Hugging Face dataset"
    )
    _add_config_argument(download_parser)

    serve_parser = subparsers.add_parser("serve", help="launch the local Gradio dashboard")
    _add_config_argument(serve_parser)
    serve_parser.add_argument(
        "--auto-send-critical",
        action="store_true",
        help="automatically send critical replay logs; run only on the NemoClaw server",
    )

    info_parser = subparsers.add_parser("db-info", help="show local database information")
    _add_config_argument(info_parser)
    return parser


def _config_summary(config: AppConfig) -> dict[str, object]:
    return {
        "config": str(config.config_path),
        "project_root": str(config.project_root),
        "dataset": str(config.data.source),
        "dataset_repository": config.data.repository,
        "dataset_revision": config.data.revision,
        "dataset_sha256": config.data.sha256,
        "database": str(config.storage.database),
        "database_inside_repository": config.storage.database.is_relative_to(config.project_root),
        "server": f"http://{config.server.host}:{config.server.port}",
        "share": config.server.share,
        "importer": {
            "batch_size": config.importer.batch_size,
            "progress_every": config.importer.progress_every,
        },
        "replay": {
            "preset": config.replay.default_preset,
            "limit": config.replay.default_limit,
            "rate": config.replay.default_rate,
            "refresh_seconds": config.replay.refresh_seconds,
            "display_timezone": config.replay.display_timezone,
        },
    }


def _run_import(config: AppConfig) -> int:
    next_report = config.importer.progress_every

    def report(progress: ImportProgress) -> None:
        nonlocal next_report
        if progress.committed_line_cursor < next_report:
            return
        print(
            "Imported through line "
            f"{progress.committed_line_cursor:,}: "
            f"{progress.accepted_count:,} accepted, "
            f"{progress.rejected_count:,} rejected, "
            f"{progress.duplicate_count:,} duplicates",
            file=sys.stderr,
        )
        while next_report <= progress.committed_line_cursor:
            next_report += config.importer.progress_every

    try:
        result = import_dataset(config, on_progress=report)
    except (ImportFailure, OSError, sqlite3.Error) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    payload = asdict(result)
    payload["elapsed_seconds"] = round(result.elapsed_seconds, 3)
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, require_dataset=args.command != "download-data")
    except ConfigError as exc:
        parser.error(str(exc))

    if args.command == "config-check":
        print(json.dumps(_config_summary(config), indent=2))
        return 0
    if args.command == "import-data":
        return _run_import(config)
    if args.command == "download-data":
        try:
            result = ensure_dataset(config)
        except (DatasetDownloadError, OSError) as exc:
            print(f"Dataset download failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(asdict(result), indent=2, default=str))
        return 0
    if args.command == "serve":
        from clawwatch_demo.ui.app import launch_dashboard

        try:
            return launch_dashboard(config, auto_send_critical=args.auto_send_critical)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            print(f"Dashboard failed: {exc}", file=sys.stderr)
            return 1
    if args.command == "db-info":
        print(json.dumps(database_summary(config.storage.database), indent=2))
        return 0
    parser.error(f"Unknown command: {args.command}")

import json
from pathlib import Path

import pytest

from clawwatch_demo.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_config_check_reports_repo_local_database(capsys) -> None:
    result = main(["config-check", "--config", str(PROJECT_ROOT / "config/demo.toml")])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["database"] == str(PROJECT_ROOT / "var/clawwatch_demo.sqlite3")
    assert output["database_inside_repository"] is True


@pytest.mark.parametrize("enabled", [False, True])
def test_serve_passes_remote_delivery_flag(monkeypatch, enabled) -> None:
    from clawwatch_demo.ui import app

    captured = []
    monkeypatch.setattr(
        app,
        "launch_dashboard",
        lambda config, *, auto_send_critical: captured.append(auto_send_critical) or 0,
    )
    args = ["serve", "--config", str(PROJECT_ROOT / "config/demo.toml")]
    if enabled:
        args.append("--auto-send-critical")
    else:
        args.append("--no-auto-send-critical")
    assert main(args) == 0
    assert captured == [enabled]


def test_serve_enables_delivery_by_default() -> None:
    from clawwatch_demo.cli import build_parser

    assert build_parser().parse_args(["serve"]).auto_send_critical is True

import json
from pathlib import Path

from clawwatch_demo.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_config_check_reports_repo_local_database(capsys) -> None:
    result = main(["config-check", "--config", str(PROJECT_ROOT / "config/demo.toml")])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["database"] == str(PROJECT_ROOT / "var/clawwatch_demo.sqlite3")
    assert output["database_inside_repository"] is True

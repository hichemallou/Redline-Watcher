from pathlib import Path

import pytest

from clawwatch_demo.config import ConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_demo_config_resolves_paths_inside_repository() -> None:
    config = load_config(PROJECT_ROOT / "config/demo.toml")

    assert config.project_root == PROJECT_ROOT
    assert config.data.source == PROJECT_ROOT / "data/advanced_siem/raw/advanced_siem_dataset.jsonl"
    assert config.storage.database == PROJECT_ROOT / "var/clawwatch_demo.sqlite3"
    assert config.storage.database.is_relative_to(PROJECT_ROOT)
    assert config.server.share is False


def test_database_outside_repository_is_rejected(tmp_path: Path) -> None:
    config_path = PROJECT_ROOT / "config/test-outside-db.toml"
    content = (PROJECT_ROOT / "config/demo.toml").read_text()
    content = content.replace(
        'database = "var/clawwatch_demo.sqlite3"', f'database = "{tmp_path / "outside.sqlite3"}"'
    )
    config_path.write_text(content)
    try:
        with pytest.raises(ConfigError, match="must be inside the repository"):
            load_config(config_path)
    finally:
        config_path.unlink()

"""Application configuration loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True)
class DataConfig:
    source: Path
    repository: str
    revision: str
    sha256: str


@dataclass(frozen=True)
class StorageConfig:
    database: Path


@dataclass(frozen=True)
class ImporterConfig:
    batch_size: int
    progress_every: int


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    share: bool


@dataclass(frozen=True)
class ReplayConfig:
    default_preset: str
    default_limit: int
    default_rate: float
    available_rates: tuple[float, ...]
    refresh_seconds: float
    display_timezone: str


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    config_path: Path
    data: DataConfig
    storage: StorageConfig
    importer: ImporterConfig
    server: ServerConfig
    replay: ReplayConfig


def discover_project_root(start: Path) -> Path:
    """Find the nearest project root containing pyproject.toml."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"Could not find pyproject.toml from {start}")


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing or invalid [{name}] section")
    return value


def _required(
    section: dict[str, Any],
    section_name: str,
    key: str,
    expected: type | tuple[type, ...],
) -> Any:
    value = section.get(key)
    if not isinstance(value, expected):
        names = (
            expected.__name__
            if isinstance(expected, type)
            else " or ".join(item.__name__ for item in expected)
        )
        raise ConfigError(f"[{section_name}].{key} must be {names}")
    return value


def _resolve_repo_path(root: Path, raw_value: str, field: str) -> Path:
    path = Path(raw_value)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(root):
        raise ConfigError(f"{field} must be inside the repository: {root}")
    return resolved


def load_config(
    config_path: str | Path = "config/demo.toml",
    *,
    project_root: Path | None = None,
    require_dataset: bool = True,
) -> AppConfig:
    """Load and validate a TOML configuration file."""
    path = Path(config_path).expanduser()
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise ConfigError(f"Configuration file does not exist: {path}")

    root = project_root.resolve() if project_root else discover_project_root(path)
    if not path.is_relative_to(root):
        raise ConfigError(f"Configuration file must be inside the repository: {root}")

    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    data_section = _section(document, "data")
    storage_section = _section(document, "storage")
    importer_section = _section(document, "importer")
    server_section = _section(document, "server")
    replay_section = _section(document, "replay")

    source = _resolve_repo_path(
        root, _required(data_section, "data", "source", str), "[data].source"
    )
    if require_dataset and not source.is_file():
        raise ConfigError(f"Dataset file does not exist: {source}")
    repository = _required(data_section, "data", "repository", str).strip()
    revision = _required(data_section, "data", "revision", str).strip()
    sha256 = _required(data_section, "data", "sha256", str).strip().lower()
    if not repository or "/" not in repository:
        raise ConfigError("[data].repository must be a namespace/repository identifier")
    if not revision:
        raise ConfigError("[data].revision cannot be empty")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ConfigError("[data].sha256 must be a 64-character hexadecimal digest")

    database = _resolve_repo_path(
        root, _required(storage_section, "storage", "database", str), "[storage].database"
    )
    if database.suffix not in {".sqlite", ".sqlite3", ".db"}:
        raise ConfigError("[storage].database must use .sqlite, .sqlite3, or .db")

    batch_size = _required(importer_section, "importer", "batch_size", int)
    progress_every = _required(importer_section, "importer", "progress_every", int)
    if batch_size <= 0:
        raise ConfigError("[importer].batch_size must be greater than zero")
    if progress_every <= 0:
        raise ConfigError("[importer].progress_every must be greater than zero")

    host = _required(server_section, "server", "host", str).strip()
    port = _required(server_section, "server", "port", int)
    share = _required(server_section, "server", "share", bool)
    if not host:
        raise ConfigError("[server].host cannot be empty")
    if not 1 <= port <= 65535:
        raise ConfigError("[server].port must be between 1 and 65535")
    if share:
        raise ConfigError("[server].share must remain false for this local demo")

    preset = _required(replay_section, "replay", "default_preset", str)
    limit = _required(replay_section, "replay", "default_limit", int)
    raw_rate = _required(replay_section, "replay", "default_rate", (int, float))
    raw_rates = _required(replay_section, "replay", "available_rates", list)
    raw_refresh = _required(replay_section, "replay", "refresh_seconds", (int, float))
    timezone = _required(replay_section, "replay", "display_timezone", str)

    if preset not in {"all", "authentication"}:
        raise ConfigError("[replay].default_preset must be 'all' or 'authentication'")
    if limit <= 0:
        raise ConfigError("[replay].default_limit must be greater than zero")
    if not raw_rates or any(not isinstance(rate, (int, float)) or rate <= 0 for rate in raw_rates):
        raise ConfigError("[replay].available_rates must contain positive numbers")
    rates = tuple(float(rate) for rate in raw_rates)
    rate = float(raw_rate)
    if rate not in rates:
        raise ConfigError("[replay].default_rate must be in available_rates")
    refresh = float(raw_refresh)
    if refresh <= 0:
        raise ConfigError("[replay].refresh_seconds must be greater than zero")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown [replay].display_timezone: {timezone}") from exc

    return AppConfig(
        project_root=root,
        config_path=path,
        data=DataConfig(
            source=source,
            repository=repository,
            revision=revision,
            sha256=sha256,
        ),
        storage=StorageConfig(database=database),
        importer=ImporterConfig(batch_size=batch_size, progress_every=progress_every),
        server=ServerConfig(host=host, port=port, share=share),
        replay=ReplayConfig(
            default_preset=preset,
            default_limit=limit,
            default_rate=rate,
            available_rates=rates,
            refresh_seconds=refresh,
            display_timezone=timezone,
        ),
    )

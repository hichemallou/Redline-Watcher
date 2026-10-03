import hashlib
from pathlib import Path

import pytest

from clawwatch_demo.config import AppConfig, DataConfig
from clawwatch_demo.downloader import DatasetDownloadError, ensure_dataset


def _config(source: Path, payload: bytes) -> AppConfig:
    return AppConfig(
        project_root=source.parents[2],
        config_path=source.parents[2] / "config.toml",
        data=DataConfig(
            source=source,
            repository="owner/dataset",
            revision="pinned-revision",
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        storage=None,  # type: ignore[arg-type]
        importer=None,  # type: ignore[arg-type]
        server=None,  # type: ignore[arg-type]
        replay=None,  # type: ignore[arg-type]
    )


def test_existing_valid_dataset_skips_download(tmp_path: Path) -> None:
    payload = b'{"event": 1}\n'
    source = tmp_path / "data" / "raw" / "dataset.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)

    def unexpected_download(**_kwargs: object) -> str:
        raise AssertionError("download should not run")

    result = ensure_dataset(_config(source, payload), downloader=unexpected_download)

    assert result.downloaded is False
    assert result.size_bytes == len(payload)


def test_missing_dataset_is_downloaded_and_verified(tmp_path: Path) -> None:
    payload = b'{"event": 1}\n'
    source = tmp_path / "data" / "raw" / "dataset.jsonl"
    captured: dict[str, object] = {}

    def download(**kwargs: object) -> str:
        captured.update(kwargs)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
        return str(source)

    result = ensure_dataset(_config(source, payload), downloader=download)

    assert result.downloaded is True
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert captured == {
        "repo_id": "owner/dataset",
        "filename": "dataset.jsonl",
        "repo_type": "dataset",
        "revision": "pinned-revision",
        "local_dir": source.parent,
    }


def test_checksum_mismatch_is_not_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "data" / "raw" / "dataset.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unexpected")

    with pytest.raises(DatasetDownloadError, match="checksum mismatch"):
        ensure_dataset(_config(source, b"expected"))

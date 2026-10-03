"""Download and verify the configured Hugging Face dataset."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from huggingface_hub import hf_hub_download

from clawwatch_demo.config import AppConfig

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


class DatasetDownloadError(RuntimeError):
    """Raised when the configured dataset cannot be downloaded or verified."""


@dataclass(frozen=True, slots=True)
class DatasetDownloadResult:
    path: Path
    sha256: str
    downloaded: bool
    size_bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_lfs_pointer(path: Path) -> bool:
    if path.stat().st_size > 1024:
        return False
    with path.open("rb") as handle:
        return handle.read(len(_LFS_POINTER_PREFIX)) == _LFS_POINTER_PREFIX


def ensure_dataset(
    config: AppConfig,
    *,
    downloader: Callable[..., Any] = hf_hub_download,
) -> DatasetDownloadResult:
    """Return a verified local dataset, downloading the pinned revision if needed."""
    source = config.data.source
    expected_digest = config.data.sha256
    if source.is_file():
        digest = sha256_file(source)
        if digest == expected_digest:
            return DatasetDownloadResult(source, digest, False, source.stat().st_size)
        if _is_lfs_pointer(source):
            source.unlink()
        else:
            raise DatasetDownloadError(
                f"Dataset checksum mismatch at {source}: expected {expected_digest}, got {digest}"
            )

    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        downloaded_path = Path(
            downloader(
                repo_id=config.data.repository,
                filename=source.name,
                repo_type="dataset",
                revision=config.data.revision,
                local_dir=source.parent,
            )
        ).resolve()
    except Exception as exc:
        raise DatasetDownloadError(f"Hugging Face download failed: {exc}") from exc

    if downloaded_path != source.resolve() and downloaded_path.is_file():
        downloaded_path.replace(source)
    if not source.is_file():
        raise DatasetDownloadError(f"Download completed without creating {source}")

    digest = sha256_file(source)
    if digest != expected_digest:
        raise DatasetDownloadError(
            f"Downloaded dataset checksum mismatch: expected {expected_digest}, got {digest}"
        )
    return DatasetDownloadResult(source, digest, True, source.stat().st_size)

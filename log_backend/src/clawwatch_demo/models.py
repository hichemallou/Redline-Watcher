"""Typed values shared by the storage and import layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReplayState = Literal[
    "ready",
    "running",
    "paused",
    "completed",
    "stopped",
    "failed",
    "interrupted",
]


@dataclass(frozen=True)
class NormalizedEvent:
    line_number: int
    event_id: str
    original_timestamp: str
    parsed_timestamp: str
    timestamp_has_timezone: bool
    event_type: str
    severity: str
    actor: str | None
    source_ip: str | None
    action: str | None
    status: str | None
    source_product: str
    message: str
    raw_log: str
    original_json: str


@dataclass(frozen=True)
class ImportProgress:
    dataset_id: int
    committed_line_cursor: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int


@dataclass(frozen=True)
class ImportResult:
    dataset_id: int
    file_sha256: str
    file_bytes: int
    total_lines: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    resumed_from_line: int
    already_complete: bool
    elapsed_seconds: float


@dataclass(frozen=True)
class ReplayRun:
    id: int
    dataset_id: int
    preset: str
    event_types: tuple[str, ...]
    severities: tuple[str, ...]
    ordering: str
    record_limit: int | None
    rate: float
    state: ReplayState
    committed_selection_cursor: int
    emitted_count: int
    started_at: str | None
    updated_at: str
    ended_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class ReviewCard:
    id: int
    replay_event_id: int
    run_id: int
    sequence: int
    event_id: str
    event_type: str
    severity: str
    actor: str | None
    source_ip: str | None
    message: str
    emitted_at: str
    stage: str
    notes: str
    revision: int
    created_at: str
    updated_at: str

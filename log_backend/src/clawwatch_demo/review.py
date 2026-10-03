"""Persistent review cards with optimistic concurrency and audit history."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from clawwatch_demo.models import ReviewCard
from clawwatch_demo.storage import connect, immediate_transaction

REVIEW_STAGES = ("new", "investigating", "resolved", "dismissed")


class ReviewError(RuntimeError):
    """Base error for review operations."""


class ReviewConflict(ReviewError):
    """Raised when a card was updated from another browser session."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


CARD_QUERY = """
    SELECT rc.id, rc.replay_event_id, re.run_id, re.sequence, se.event_id,
           se.event_type, se.severity, se.actor, se.source_ip, se.message,
           re.emitted_at, rc.stage, rc.notes, rc.revision,
           rc.created_at, rc.updated_at
    FROM review_cards AS rc
    JOIN replay_events AS re ON re.id = rc.replay_event_id
    JOIN source_events AS se ON se.id = re.source_event_id
"""


def _row_to_card(row: sqlite3.Row) -> ReviewCard:
    return ReviewCard(
        id=int(row["id"]),
        replay_event_id=int(row["replay_event_id"]),
        run_id=int(row["run_id"]),
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        severity=str(row["severity"]),
        actor=None if row["actor"] is None else str(row["actor"]),
        source_ip=None if row["source_ip"] is None else str(row["source_ip"]),
        message=str(row["message"]),
        emitted_at=str(row["emitted_at"]),
        stage=str(row["stage"]),
        notes=str(row["notes"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _get_card(connection: sqlite3.Connection, card_id: int) -> ReviewCard:
    row = connection.execute(f"{CARD_QUERY} WHERE rc.id = ?", (card_id,)).fetchone()
    if row is None:
        raise ReviewError(f"Review card does not exist: {card_id}")
    return _row_to_card(row)


def get_review_card(database: Path, card_id: int) -> ReviewCard:
    connection = connect(database, read_only=True)
    try:
        return _get_card(connection, card_id)
    finally:
        connection.close()


def add_to_review(database: Path, replay_event_id: int) -> tuple[ReviewCard, bool]:
    """Create one card per replay event, returning an existing card idempotently."""
    connection = connect(database)
    try:
        with immediate_transaction(connection):
            existing = connection.execute(
                f"{CARD_QUERY} WHERE rc.replay_event_id = ?", (replay_event_id,)
            ).fetchone()
            if existing is not None:
                return _row_to_card(existing), False

            event = connection.execute(
                "SELECT run_id FROM replay_events WHERE id = ?", (replay_event_id,)
            ).fetchone()
            if event is None:
                raise ReviewError(f"Replay event does not exist: {replay_event_id}")
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO review_cards(
                    replay_event_id, stage, notes, revision, created_at, updated_at
                ) VALUES (?, 'new', '', 1, ?, ?)
                """,
                (replay_event_id, now, now),
            )
            card_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO activity_log(
                    run_id, card_id, action, prior_state, new_state,
                    occurred_at, detail_json
                ) VALUES (?, ?, 'review_created', NULL, 'new', ?, '{}')
                """,
                (int(event["run_id"]), card_id, now),
            )
            return _get_card(connection, card_id), True
    except sqlite3.IntegrityError as exc:
        raise ReviewConflict("The event was added to review concurrently") from exc
    finally:
        connection.close()


def update_review_card(
    database: Path,
    card_id: int,
    *,
    expected_revision: int,
    stage: str,
    notes: str,
) -> ReviewCard:
    if stage not in REVIEW_STAGES:
        raise ReviewError(f"Unknown review stage: {stage}")
    if len(notes) > 10_000:
        raise ReviewError("Review notes cannot exceed 10,000 characters")

    connection = connect(database)
    try:
        with immediate_transaction(connection):
            card = _get_card(connection, card_id)
            if card.revision != expected_revision:
                raise ReviewConflict(
                    f"Card {card_id} changed from revision {expected_revision} "
                    f"to {card.revision}; reload it before saving"
                )
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE review_cards
                SET stage = ?, notes = ?, revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (stage, notes, now, card_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ReviewConflict(f"Card {card_id} changed before this update committed")
            action = "review_moved" if stage != card.stage else "review_noted"
            detail = json.dumps(
                {
                    "notes_changed": notes != card.notes,
                    "prior_revision": card.revision,
                    "new_revision": card.revision + 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO activity_log(
                    run_id, card_id, action, prior_state, new_state,
                    occurred_at, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (card.run_id, card.id, action, card.stage, stage, now, detail),
            )
            return _get_card(connection, card_id)
    finally:
        connection.close()


def list_review_cards(
    database: Path,
    *,
    run_id: int | None = None,
    per_stage_limit: int = 20,
) -> list[ReviewCard]:
    """Return a bounded, newest-first board for each workflow stage."""
    if per_stage_limit <= 0 or per_stage_limit > 100:
        raise ReviewError("per_stage_limit must be between 1 and 100")
    connection = connect(database, read_only=True)
    try:
        parameters: list[object] = []
        run_filter = ""
        if run_id is not None:
            run_filter = "WHERE re.run_id = ?"
            parameters.append(run_id)
        rows = connection.execute(
            f"""
            WITH ranked AS (
                SELECT rc.id,
                       ROW_NUMBER() OVER (
                           PARTITION BY rc.stage
                           ORDER BY rc.updated_at DESC, rc.id DESC
                       ) AS stage_rank
                FROM review_cards AS rc
                JOIN replay_events AS re ON re.id = rc.replay_event_id
                {run_filter}
            )
            {CARD_QUERY}
            JOIN ranked ON ranked.id = rc.id
            WHERE ranked.stage_rank <= ?
            ORDER BY CASE rc.stage
                         WHEN 'new' THEN 1
                         WHEN 'investigating' THEN 2
                         WHEN 'resolved' THEN 3
                         ELSE 4
                     END,
                     rc.updated_at DESC,
                     rc.id DESC
            """,
            (*parameters, per_stage_limit),
        ).fetchall()
        return [_row_to_card(row) for row in rows]
    finally:
        connection.close()


def review_counts(database: Path, *, run_id: int | None = None) -> dict[str, int]:
    connection = connect(database, read_only=True)
    try:
        parameters: tuple[object, ...] = () if run_id is None else (run_id,)
        run_join = "" if run_id is None else "JOIN replay_events re ON re.id = rc.replay_event_id"
        run_where = "" if run_id is None else "WHERE re.run_id = ?"
        rows = connection.execute(
            f"""
            SELECT rc.stage, COUNT(*) AS count
            FROM review_cards rc
            {run_join}
            {run_where}
            GROUP BY rc.stage
            """,
            parameters,
        ).fetchall()
        counts = {stage: 0 for stage in REVIEW_STAGES}
        counts.update({cast(str, row["stage"]): int(row["count"]) for row in rows})
        return counts
    finally:
        connection.close()

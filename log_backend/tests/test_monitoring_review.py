import sqlite3
from pathlib import Path

import pytest
from test_replay import make_database

from clawwatch_demo.monitoring import (
    DashboardFilters,
    dashboard_snapshot,
    event_detail,
    filter_options,
    list_runs,
)
from clawwatch_demo.replay import ReplayController
from clawwatch_demo.review import (
    ReviewConflict,
    add_to_review,
    get_review_card,
    list_review_cards,
    review_counts,
    update_review_card,
)
from clawwatch_demo.storage import connect


def completed_run(database: Path, *, count: int = 6) -> tuple[ReplayController, int]:
    controller = ReplayController(database)
    run = controller.create_run(record_limit=count, rate=1_000)
    controller.start(run.id)
    controller.wait_for_state(run.id, {"completed"})
    return controller, run.id


def first_replay_event(database: Path, run_id: int) -> int:
    connection = connect(database, read_only=True)
    try:
        return int(
            connection.execute(
                "SELECT id FROM replay_events WHERE run_id = ? ORDER BY sequence LIMIT 1",
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_monitor_snapshot_filters_literal_search_and_event_details(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=8)
    _, run_id = completed_run(database)

    event_types, severities = filter_options(database)
    snapshot = dashboard_snapshot(
        database,
        run_id=run_id,
        filters=DashboardFilters(event_types=("auth",)),
    )
    literal_wildcard = dashboard_snapshot(
        database,
        run_id=run_id,
        filters=DashboardFilters(search="%"),
    )
    detail = event_detail(database, int(snapshot.feed.iloc[0]["replay_event_id"]))

    assert event_types == ["auth", "endpoint"]
    assert severities == ["high", "low"]
    assert snapshot.emitted_count == 6
    assert set(snapshot.feed["event_type"]) == {"auth"}
    assert literal_wildcard.feed.empty
    assert detail["event_id"].startswith("evt-")
    assert detail["original_payload"]["advanced_metadata"] == {}
    assert list_runs(database)[0]["id"] == run_id


def test_review_cards_are_idempotent_audited_and_revision_checked(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=4)
    _, run_id = completed_run(database, count=4)
    replay_event_id = first_replay_event(database, run_id)

    card, created = add_to_review(database, replay_event_id)
    existing, created_again = add_to_review(database, replay_event_id)
    updated = update_review_card(
        database,
        card.id,
        expected_revision=card.revision,
        stage="investigating",
        notes="Checking the authentication context.",
    )

    assert created is True
    assert created_again is False
    assert existing.id == card.id
    assert updated.stage == "investigating"
    assert updated.revision == 2
    assert get_review_card(database, card.id) == updated
    assert [item.id for item in list_review_cards(database, run_id=run_id)] == [card.id]
    assert review_counts(database, run_id=run_id)["investigating"] == 1

    with pytest.raises(ReviewConflict, match="changed from revision 1 to 2"):
        update_review_card(
            database,
            card.id,
            expected_revision=1,
            stage="resolved",
            notes="stale update",
        )

    connection = connect(database, read_only=True)
    try:
        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM activity_log WHERE card_id = ? ORDER BY id", (card.id,)
            )
        ]
    finally:
        connection.close()
    assert actions == ["review_created", "review_moved"]


def test_review_update_rolls_back_when_activity_write_fails(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=2)
    _, run_id = completed_run(database, count=2)
    card, _ = add_to_review(database, first_replay_event(database, run_id))
    # A trigger gives the transaction a deterministic audit-write failure.
    connection = connect(database)
    try:
        connection.execute(
            """
            CREATE TRIGGER fail_review_audit
            BEFORE INSERT ON activity_log
            WHEN NEW.action = 'review_moved'
            BEGIN
                SELECT RAISE(ABORT, 'simulated audit failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="simulated audit failure"):
        update_review_card(
            database,
            card.id,
            expected_revision=1,
            stage="resolved",
            notes="must rollback",
        )
    unchanged = get_review_card(database, card.id)
    assert unchanged.stage == "new"
    assert unchanged.revision == 1

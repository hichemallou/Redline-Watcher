from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from test_replay import make_database

from clawwatch_demo.config import StorageConfig, load_config
from clawwatch_demo.replay import ReplayController
from clawwatch_demo.review import add_to_review
from clawwatch_demo.ui.app import create_app
from clawwatch_demo.ui.callbacks import DashboardCallbacks
from clawwatch_demo.ui.render import board_html

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def complete_run(database: Path) -> tuple[ReplayController, int]:
    controller = ReplayController(database)
    run = controller.create_run(record_limit=4, rate=1_000)
    controller.start(run.id)
    controller.wait_for_state(run.id, {"completed"})
    return controller, run.id


def test_dashboard_callbacks_refresh_select_and_review(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=6)
    controller, run_id = complete_run(database)
    callbacks = DashboardCallbacks(database, controller)

    refreshed = callbacks.refresh(run_id, [], [], "", None)
    feed = refreshed[5]
    assert isinstance(feed, pd.DataFrame)
    assert len(feed.index) == 4
    assert "RUNNING" not in refreshed[0]
    assert "Committed events" in refreshed[1]

    selection = callbacks.select_event(feed, SimpleNamespace(index=(0, 0)))
    replay_event_id, metadata, raw_log, original_payload, _ = selection
    assert metadata["run_id"] == run_id
    assert raw_log.startswith("raw event")
    assert original_payload["event_id"].startswith("evt-")

    list_selection = callbacks.select_event(feed, SimpleNamespace(index=[0, 0]))
    assert list_selection[0] == replay_event_id

    board, card_update, stage, notes, revision, notice = callbacks.add_selected_to_review(
        replay_event_id, run_id
    )
    card_id = card_update["value"]
    assert "Created review card" in notice
    assert f"#{card_id}" in board
    assert stage == "new"
    assert notes == ""
    assert revision == 1

    stage, notes, revision, _ = callbacks.load_review_card(card_id)
    saved = callbacks.save_review_card(
        card_id,
        "investigating",
        "Manual review in progress.",
        revision,
        run_id,
    )
    assert stage == "new"
    assert notes == ""
    assert saved[0] == "investigating"
    assert saved[2] == 2
    assert "Manual review in progress." not in saved[4]


def test_board_renderer_escapes_dataset_content(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=2)
    controller, run_id = complete_run(database)
    refreshed = DashboardCallbacks(database, controller).refresh(run_id, [], [], "", None)
    feed = refreshed[5]
    replay_event_id = int(feed.iloc[0]["replay_event_id"])
    card, _ = add_to_review(database, replay_event_id)
    unsafe = replace(card, message="<script>alert('x')</script>")

    rendered = board_html(
        [unsafe],
        {"new": 1, "investigating": 0, "resolved": 0, "dismissed": 0},
    )

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_gradio_app_builds_with_packaged_css(tmp_path: Path) -> None:
    database = make_database(tmp_path, event_count=2)
    base = load_config(PROJECT_ROOT / "config/demo.toml")
    config = replace(base, storage=StorageConfig(database=database))

    app, runtime = create_app(config, recover=False)
    try:
        serialized = app.get_config_file()
        assert serialized["title"] == "ClawWatch Log Lab"
        assert len(serialized["components"]) >= 40
        assert ".cw-kanban" in runtime.css
        assert any(
            component["props"].get("value") == "Send critical alerts to Slack"
            for component in serialized["components"]
        )
        assert any(
            dependency.get("api_name") == "send_critical_alerts"
            for dependency in serialized["dependencies"]
        )
    finally:
        runtime.controller.close()


def test_run_id_normalizes_empty_and_single_value_sequences() -> None:
    assert DashboardCallbacks._run_id([]) is None
    assert DashboardCallbacks._run_id([7]) == 7

import json
import threading
from pathlib import Path

import pytest
from test_replay import make_database

from clawwatch_demo import review_slack
from clawwatch_demo.replay import ReplayController
from clawwatch_demo.review import add_to_review, get_review_card, update_review_card
from clawwatch_demo.review_slack import ReviewSlackNotifier, send_critical_reviews
from clawwatch_demo.slack import SlackError, SlackMessageResult
from clawwatch_demo.storage import connect


@pytest.fixture
def alert_board(tmp_path: Path, monkeypatch):
    database = make_database(tmp_path, event_count=5)
    controller = ReplayController(database)
    run = controller.create_run(record_limit=5, rate=1000)
    controller.start(run.id)
    controller.wait_for_state(run.id, {"completed"})
    controller.close()
    connection = connect(database)
    try:
        connection.execute("UPDATE source_events SET severity = 'CRITICAL' WHERE id != 5")
        connection.commit()
        event_ids = [
            row[0] for row in connection.execute("SELECT id FROM replay_events ORDER BY id")
        ]
    finally:
        connection.close()
    cards = [add_to_review(database, event_id)[0] for event_id in event_ids]
    for card, stage in zip(cards[1:4], ["investigating", "resolved", "dismissed"], strict=True):
        update_review_card(database, card.id, expected_revision=1, stage=stage, notes="")
    env_file = tmp_path / ".env"
    env_file.write_text("SLACK_TOKEN=xoxb-test\nSLACK_CHANNEL_ID=CTEST\n")
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    return database, env_file, cards


def test_only_open_critical_cards_send_once_and_are_audited(alert_board, monkeypatch):
    database, env_file, cards = alert_board
    messages = []

    def send(message, settings):
        messages.append(message)
        assert settings.channel == "CTEST"
        return SlackMessageResult("CTEST", "123.45")

    monkeypatch.setattr(review_slack, "send_slack_message", send)
    first = send_critical_reviews(database, env_file)
    assert first.card_ids == (cards[0].id, cards[1].id)
    assert send_critical_reviews(database, env_file).card_ids == ()
    assert len(messages) == 2
    assert "[CRITICAL]" in messages[0]
    assert "Event: evt-1" in messages[0]
    assert "Actor: user-0" in messages[0]
    connection = connect(database, read_only=True)
    try:
        audits = connection.execute(
            "SELECT detail_json FROM activity_log WHERE action = 'review_slack_sent'"
        ).fetchall()
    finally:
        connection.close()
    assert len(audits) == 2
    assert json.loads(audits[0][0]) == {"channel": "CTEST", "timestamp": "123.45"}
    assert get_review_card(database, cards[0].id).revision == 1


def test_failed_send_retries_without_resending_prior_success(alert_board, monkeypatch):
    database, env_file, cards = alert_board
    calls = []

    def send(message, settings):
        calls.append(message)
        if len(calls) == 2:
            raise SlackError("not_in_channel")
        return SlackMessageResult("CTEST", "123.45")

    monkeypatch.setattr(review_slack, "send_slack_message", send)
    with pytest.raises(SlackError, match="not_in_channel"):
        send_critical_reviews(database, env_file)
    assert send_critical_reviews(database, env_file).card_ids == (cards[1].id,)
    assert len(calls) == 3


def test_dry_run_does_not_send_or_mark_delivered(alert_board, monkeypatch):
    database, env_file, cards = alert_board

    def unexpected(*args):
        pytest.fail("dry-run contacted Slack")

    monkeypatch.setattr(review_slack, "send_slack_message", unexpected)
    for _ in range(2):
        result = send_critical_reviews(database, env_file, dry_run=True)
        assert result.dry_run
        assert result.card_ids == (cards[0].id, cards[1].id)


def test_concurrent_dispatch_and_database_writes_are_safe(alert_board, monkeypatch):
    database, env_file, cards = alert_board

    def send(message, settings):
        assert send_critical_reviews(database, env_file).busy
        # A network call must not block normal review edits with a DB write lock.
        card = get_review_card(database, cards[0].id)
        update_review_card(
            database, card.id, expected_revision=card.revision, stage="new", notes="checking"
        )
        return SlackMessageResult("CTEST", "123.45")

    monkeypatch.setattr(review_slack, "send_slack_message", send)
    assert len(send_critical_reviews(database, env_file).card_ids) == 2


def test_notifier_reports_errors_and_runs_without_a_browser(alert_board, monkeypatch):
    database, env_file, _ = alert_board
    checked = threading.Event()

    def fail(*args):
        checked.set()
        raise SlackError("missing_scope")

    monkeypatch.setattr(review_slack, "send_slack_message", fail)
    notifier = ReviewSlackNotifier(database, env_file)
    assert "missing_scope" in notifier.check()
    checked.clear()
    notifier.start()
    try:
        assert checked.wait(2)
    finally:
        notifier.close()
    assert not notifier._thread.is_alive()


def test_alert_escapes_slack_mentions(alert_board):
    from dataclasses import replace

    _, _, cards = alert_board
    message = review_slack.format_review_alert(
        replace(cards[0], message="<!channel> <@U123> & test")
    )
    assert "<!channel>" not in message
    assert "&lt;!channel&gt; &lt;@U123&gt; &amp; test" in message


def test_cli_uses_configured_database_and_dry_run(alert_board, monkeypatch, capsys):
    from types import SimpleNamespace

    from clawwatch_demo import review_slack_cli

    database, env_file, cards = alert_board
    monkeypatch.setattr(
        review_slack_cli,
        "load_config",
        lambda *args, **kwargs: SimpleNamespace(
            storage=SimpleNamespace(database=database), project_root=env_file.parent
        ),
    )
    assert review_slack_cli.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "xoxb-" not in output
    assert json.loads(output)["card_ids"] == [cards[0].id, cards[1].id]
    env_file.write_text("")
    assert review_slack_cli.main(["--dry-run"]) == 1
    assert "SLACK_TOKEN is missing" in capsys.readouterr().err


def test_button_calls_shared_sender(alert_board, monkeypatch):
    from clawwatch_demo.ui.callbacks import DashboardCallbacks

    database, env_file, cards = alert_board
    messages = []

    def send(message, settings):
        messages.append(message)
        return SlackMessageResult("CTEST", "123.45")

    monkeypatch.setattr(review_slack, "send_slack_message", send)
    controller = ReplayController(database)
    callbacks = DashboardCallbacks(database, controller, ReviewSlackNotifier(database, env_file))
    try:
        assert f"#{cards[0].id}" in callbacks.send_critical_alerts()
        assert "no unsent" in callbacks.send_critical_alerts()
        assert len(messages) == 2
    finally:
        controller.close()

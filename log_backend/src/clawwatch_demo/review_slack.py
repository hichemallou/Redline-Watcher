"""Send pending critical review cards to Slack, shared by the UI and CLI."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clawwatch_demo.models import ReviewCard
from clawwatch_demo.review import get_review_card
from clawwatch_demo.slack import SlackError, load_slack_settings, send_slack_message
from clawwatch_demo.storage import connect, immediate_transaction


@dataclass(frozen=True)
class AlertResult:
    card_ids: tuple[int, ...] = ()
    dry_run: bool = False
    busy: bool = False


def format_review_alert(card: ReviewCard) -> str:
    # Escape Slack control syntax in untrusted event fields (including mentions).
    def safe(value: object) -> str:
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return "\n".join(
        [
            f"[CRITICAL] ClawWatch review card #{card.id}",
            f"Event: {safe(card.event_id)} | Type: {safe(card.event_type)}",
            f"Run: {card.run_id} | Sequence: {card.sequence} | Stage: {card.stage}",
            f"Actor: {safe(card.actor or 'Unknown')} | "
            f"Source IP: {safe(card.source_ip or 'Unknown')}",
            f"Emitted: {safe(card.emitted_at)}",
            f"Message: {safe(card.message[:6000])}",
        ]
    )


def send_critical_reviews(
    database: Path,
    env_file: Path,
    *,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
) -> AlertResult:
    """Scan all runs; persist successful sends and serialize UI/CLI dispatchers.

    Network calls never hold a SQLite write transaction. As with most external
    notifications, a crash after Slack accepts a message but before the audit
    commits can cause a duplicate on retry.
    """
    database = database.resolve()
    if not database.is_file():
        raise SlackError(f"Review database does not exist: {database}")
    with Path(f"{database}.slack.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return AlertResult(busy=True, dry_run=dry_run)
        connection = connect(database)
        try:
            rows = connection.execute(
                """
                SELECT rc.id FROM review_cards rc
                JOIN replay_events re ON re.id = rc.replay_event_id
                JOIN source_events se ON se.id = re.source_event_id
                WHERE lower(trim(se.severity)) = 'critical'
                  AND rc.stage IN ('new', 'investigating')
                  AND NOT EXISTS (
                      SELECT 1 FROM activity_log a
                      WHERE a.card_id = rc.id AND a.action = 'review_slack_sent'
                  )
                ORDER BY rc.id
                LIMIT 20
                """
            ).fetchall()
            # Still validate credentials in dry-run mode, even on an empty board.
            if not rows and not dry_run:
                return AlertResult()
            settings = load_slack_settings(env_file)
            sent: list[int] = []
            for row in rows:
                if stop_event is not None and stop_event.is_set():
                    break
                card = get_review_card(database, int(row["id"]))
                if card.stage not in {"new", "investigating"}:
                    continue
                if not dry_run:
                    result = send_slack_message(format_review_alert(card), settings)
                    with immediate_transaction(connection):
                        connection.execute(
                            """
                            INSERT INTO activity_log(
                                run_id, card_id, action, occurred_at, detail_json
                            ) VALUES (?, ?, 'review_slack_sent', ?, ?)
                            """,
                            (
                                card.run_id,
                                card.id,
                                datetime.now(UTC).isoformat(),
                                json.dumps(
                                    {"channel": result.channel, "timestamp": result.timestamp}
                                ),
                            ),
                        )
                sent.append(card.id)
            return AlertResult(tuple(sent), dry_run=dry_run)
        finally:
            connection.close()


class ReviewSlackNotifier:
    """Server-owned polling worker; manual buttons use the same check method."""

    def __init__(self, database: Path, env_file: Path) -> None:
        self.database = database
        self.env_file = env_file
        self.status = "Slack: waiting for the first critical review check."
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> str:
        try:
            result = send_critical_reviews(self.database, self.env_file, stop_event=self._stop)
            if result.busy:
                return "Slack: another critical alert check is in progress."
            now = datetime.now(UTC).strftime("%H:%M:%S UTC")
            if result.card_ids:
                cards = ", ".join(f"#{card_id}" for card_id in result.card_ids)
                self.status = f"Slack: sent critical review cards {cards} at {now}."
            else:
                self.status = f"Slack: no unsent open critical cards at {now}."
        except (SlackError, OSError, sqlite3.Error) as exc:
            self.status = f"Slack alert failed: {exc}. Will retry on the next check."
        return self.status

    def start(self) -> None:
        def poll() -> None:
            while not self._stop.is_set():
                self.check()
                self._stop.wait(30)

        self._thread = threading.Thread(target=poll, name="review-slack", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=16)

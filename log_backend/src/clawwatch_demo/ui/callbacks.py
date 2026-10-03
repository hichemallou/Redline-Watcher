"""Short Gradio callbacks backed by the replay, monitoring, and review services."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import gradio as gr
import pandas as pd

from clawwatch_demo.monitoring import (
    DashboardFilters,
    dashboard_snapshot,
    event_detail,
    list_runs,
)
from clawwatch_demo.replay import ReplayController, ReplayError
from clawwatch_demo.review import (
    REVIEW_STAGES,
    ReviewError,
    add_to_review,
    get_review_card,
    list_review_cards,
    review_counts,
    update_review_card,
)
from clawwatch_demo.review_slack import ReviewSlackNotifier
from clawwatch_demo.storage import connect
from clawwatch_demo.ui.render import board_html, metrics_html, notice_html, status_html


class DashboardCallbacks:
    def __init__(
        self,
        database: Path,
        controller: ReplayController,
        slack_notifier: ReviewSlackNotifier | None = None,
    ) -> None:
        self.database = database
        self.controller = controller
        self.slack_notifier = slack_notifier

    def send_critical_alerts(self) -> str:
        if self.slack_notifier is None:
            return notice_html("Slack notifier is unavailable.", kind="error")
        return notice_html(self.slack_notifier.check())

    def slack_status(self) -> str:
        return notice_html(
            self.slack_notifier.status if self.slack_notifier else "Slack notifier is unavailable."
        )

    @staticmethod
    def _run_id(value: Any) -> int | None:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value in (None, ""):
            return None
        return int(value)

    def _run_choices(
        self, selected_run_id: int | None
    ) -> tuple[list[dict[str, Any]], list[tuple[str, int]], int | None]:
        runs = list_runs(self.database)
        choices = [
            (
                f"Run #{run['id']} · {str(run['state']).upper()} · "
                f"{int(run['emitted_count']):,} events",
                int(run["id"]),
            )
            for run in runs
        ]
        ids = {value for _, value in choices}
        selected = (
            selected_run_id if selected_run_id in ids else (choices[0][1] if choices else None)
        )
        return runs, choices, selected

    def _board_values(
        self,
        run_id: int | None,
        selected_card_id: int | None,
    ) -> tuple[str, dict[str, object]]:
        cards = list_review_cards(self.database, run_id=run_id)
        counts = review_counts(self.database, run_id=run_id)
        choices = [
            (
                f"Card #{card.id} · {card.stage.upper()} · {card.event_id}",
                card.id,
            )
            for card in cards
        ]
        ids = {value for _, value in choices}
        selected = selected_card_id if selected_card_id in ids else None
        return board_html(cards, counts), gr.update(choices=choices, value=selected)

    def _activity_frame(self, run_id: int | None) -> pd.DataFrame:
        columns = ["occurred_at", "action", "prior_state", "new_state", "card_id", "detail"]
        if run_id is None:
            return pd.DataFrame(columns=columns)
        connection = connect(self.database, read_only=True)
        try:
            frame = pd.read_sql_query(
                """
                SELECT occurred_at, action, COALESCE(prior_state, '') AS prior_state,
                       COALESCE(new_state, '') AS new_state,
                       COALESCE(card_id, '') AS card_id, detail_json AS detail
                FROM activity_log
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 100
                """,
                connection,
                params=(run_id,),
            )
            return frame.reindex(columns=columns)
        finally:
            connection.close()

    def refresh(
        self,
        run_id: Any,
        event_types: list[str] | None,
        severities: list[str] | None,
        search: str | None,
        selected_card_id: Any,
    ) -> tuple[Any, ...]:
        requested_run = self._run_id(run_id)
        runs, run_choices, selected_run = self._run_choices(requested_run)
        snapshot = dashboard_snapshot(
            self.database,
            run_id=selected_run,
            filters=DashboardFilters(
                event_types=tuple(event_types or ()),
                severities=tuple(severities or ()),
                search=search or "",
            ),
        )
        board, card_update = self._board_values(selected_run, self._run_id(selected_card_id))
        history = pd.DataFrame(runs).reindex(
            columns=[
                "id",
                "state",
                "rate",
                "emitted_count",
                "record_limit",
                "started_at",
                "updated_at",
            ]
        )
        return (
            status_html(snapshot),
            metrics_html(snapshot),
            snapshot.timeline,
            snapshot.severity,
            snapshot.categories,
            snapshot.feed,
            board,
            card_update,
            gr.update(choices=run_choices, value=selected_run),
            history,
            self._activity_frame(selected_run),
        )

    def start_run(
        self,
        preset: str,
        limit: str,
        ordering: str,
        rate: float,
    ) -> tuple[dict[str, object], str]:
        try:
            record_limit = None if limit == "All matching rows" else int(limit.replace(",", ""))
            run = self.controller.create_run(
                preset=preset,
                ordering=ordering,
                record_limit=record_limit,
                rate=float(rate),
            )
            self.controller.start(run.id)
            _, choices, _ = self._run_choices(run.id)
            return (
                gr.update(choices=choices, value=run.id),
                notice_html(f"Started replay run #{run.id}.", kind="success"),
            )
        except (ReplayError, sqlite3.Error, ValueError) as exc:
            _, choices, selected = self._run_choices(None)
            return gr.update(choices=choices, value=selected), notice_html(str(exc), kind="error")

    def _control(self, run_id: Any, action: str) -> str:
        selected = self._run_id(run_id)
        if selected is None:
            return notice_html("Select a replay run first.", kind="error")
        try:
            run = getattr(self.controller, action)(selected)
            return notice_html(
                f"Run #{run.id} is now {run.state}.",
                kind="success",
            )
        except (ReplayError, sqlite3.Error, TimeoutError, ValueError) as exc:
            return notice_html(str(exc), kind="error")

    def pause_run(self, run_id: Any) -> str:
        return self._control(run_id, "pause")

    def resume_run(self, run_id: Any) -> str:
        return self._control(run_id, "resume")

    def stop_run(self, run_id: Any) -> str:
        return self._control(run_id, "stop")

    def apply_rate(self, run_id: Any, rate: float) -> str:
        selected = self._run_id(run_id)
        if selected is None:
            return notice_html("Select a replay run first.", kind="error")
        try:
            run = self.controller.set_rate(selected, float(rate))
            return notice_html(
                f"Run #{run.id} rate changed to {run.rate:g} events/s.",
                kind="success",
            )
        except (ReplayError, sqlite3.Error, ValueError) as exc:
            return notice_html(str(exc), kind="error")

    def select_event(
        self,
        frame: pd.DataFrame | list[list[Any]] | None,
        event: gr.SelectData,
    ) -> tuple[Any, dict[str, Any], str, dict[str, Any], str]:
        try:
            if frame is None:
                raise ValueError("The live feed is empty")
            data = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
            row_index = (
                int(event.index[0]) if isinstance(event.index, (list, tuple)) else int(event.index)
            )
            replay_event_id = int(data.iloc[row_index]["replay_event_id"])
            detail = event_detail(self.database, replay_event_id)
            original_payload = detail.pop("original_payload")
            raw_log = str(detail.pop("raw_log"))
            return (
                replay_event_id,
                detail,
                raw_log,
                original_payload,
                notice_html(f"Selected replay event #{replay_event_id}."),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            return None, {}, "", {}, notice_html(str(exc), kind="error")

    def add_selected_to_review(
        self,
        replay_event_id: Any,
        run_id: Any,
    ) -> tuple[str, dict[str, object], str, str, int, str]:
        try:
            if replay_event_id in (None, ""):
                raise ReviewError("Select a live-feed event first")
            card, created = add_to_review(self.database, int(replay_event_id))
            board, card_update = self._board_values(self._run_id(run_id), card.id)
            message = (
                f"Created review card #{card.id}."
                if created
                else f"Selected existing review card #{card.id}."
            )
            return (
                board,
                card_update,
                card.stage,
                card.notes,
                card.revision,
                notice_html(message, kind="success"),
            )
        except (ReviewError, sqlite3.Error, ValueError) as exc:
            board, card_update = self._board_values(self._run_id(run_id), None)
            return (
                board,
                card_update,
                REVIEW_STAGES[0],
                "",
                0,
                notice_html(str(exc), kind="error"),
            )

    def load_review_card(self, card_id: Any) -> tuple[str, str, int, Any]:
        try:
            normalized = self._run_id(card_id)
            if normalized is None:
                return REVIEW_STAGES[0], "", 0, gr.skip()
            card = get_review_card(self.database, normalized)
            return (
                card.stage,
                card.notes,
                card.revision,
                notice_html(f"Loaded card #{card.id}, revision {card.revision}."),
            )
        except (ReviewError, sqlite3.Error, ValueError) as exc:
            return REVIEW_STAGES[0], "", 0, notice_html(str(exc), kind="error")

    def save_review_card(
        self,
        card_id: Any,
        stage: str,
        notes: str,
        revision: int,
        run_id: Any,
    ) -> tuple[str, str, int, str, str, dict[str, object]]:
        try:
            if card_id in (None, ""):
                raise ReviewError("Select a review card")
            card = update_review_card(
                self.database,
                int(card_id),
                expected_revision=int(revision),
                stage=stage,
                notes=notes,
            )
            board, card_update = self._board_values(self._run_id(run_id), card.id)
            return (
                card.stage,
                card.notes,
                card.revision,
                notice_html(f"Saved card #{card.id}, revision {card.revision}.", kind="success"),
                board,
                card_update,
            )
        except (ReviewError, sqlite3.Error, ValueError) as exc:
            board, card_update = self._board_values(self._run_id(run_id), self._run_id(card_id))
            return stage, notes, revision, notice_html(str(exc), kind="error"), board, card_update

    def selected_run_summary(self, run_id: Any) -> dict[str, Any]:
        selected = self._run_id(run_id)
        if selected is None:
            return {}
        try:
            return asdict(self.controller.get_run(selected))
        except ReplayError as exc:
            return {"error": str(exc)}

"""Grafana-inspired Gradio dashboard and persistent review board."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import gradio as gr

from clawwatch_demo.config import AppConfig
from clawwatch_demo.critical_logs import CriticalLogNotifier
from clawwatch_demo.monitoring import filter_options, list_runs
from clawwatch_demo.replay import ReplayController, recover_interrupted_runs
from clawwatch_demo.review import REVIEW_STAGES
from clawwatch_demo.review_slack import ReviewSlackNotifier
from clawwatch_demo.ui.callbacks import DashboardCallbacks
from clawwatch_demo.ui.render import board_html, header_html, notice_html


@dataclass(frozen=True)
class DashboardRuntime:
    controller: ReplayController
    slack_notifier: ReviewSlackNotifier
    critical_log_notifier: CriticalLogNotifier
    recovered_run_ids: tuple[int, ...]
    theme: Any
    css: str


def set_critical_delivery(
    enabled: bool, controller: ReplayController, notifier: CriticalLogNotifier
) -> tuple[bool, str]:
    """Toggle replay delivery from Gradio without changing replay state."""
    if enabled:
        try:
            notifier.start()
        except (OSError, RuntimeError) as exc:
            controller.auto_send_critical = False
            notifier.status = f"Critical logs: could not enable automatic sending: {exc}"
            return False, notifier.status
        controller.auto_send_critical = True
    else:
        controller.auto_send_critical = False
        notifier.close()
        notifier.status = "Critical logs: automatic sending disabled; pending logs are retained."
    return enabled, notifier.status


@contextmanager
def application_lock(database: Path) -> Iterator[None]:
    lock_path = Path(f"{database}.app.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another ClawWatch application process owns {database}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_app(
    config: AppConfig,
    *,
    recover: bool = True,
    auto_send_critical: bool = True,
) -> tuple[gr.Blocks, DashboardRuntime]:
    recovered = recover_interrupted_runs(config.storage.database) if recover else ()
    controller = ReplayController(config.storage.database, auto_send_critical=auto_send_critical)
    critical_log_notifier = CriticalLogNotifier(
        config.storage.database, config.project_root / "scripts/remote/send_critical_log.py"
    )
    if not auto_send_critical:
        critical_log_notifier.status = "Critical logs: automatic sending disabled."
    slack_notifier = ReviewSlackNotifier(config.storage.database, config.project_root / ".env")
    callbacks = DashboardCallbacks(config.storage.database, controller, slack_notifier)
    event_types, severities = filter_options(config.storage.database)
    initial_runs = list_runs(config.storage.database)
    run_choices = [
        (
            f"Run #{run['id']} · {str(run['state']).upper()} · "
            f"{int(run['emitted_count']):,} events",
            int(run["id"]),
        )
        for run in initial_runs
    ]
    initial_run_id = run_choices[0][1] if run_choices else None
    css = (
        resources.files("clawwatch_demo.ui.assets")
        .joinpath("dashboard.css")
        .read_text(encoding="utf-8")
    )
    theme = gr.themes.Base(
        primary_hue="red",
        secondary_hue="slate",
        neutral_hue="slate",
        spacing_size="sm",
        radius_size="sm",
    ).set(
        body_background_fill="#090d12",
        body_background_fill_dark="#090d12",
        body_text_color="#e8edf3",
        body_text_color_dark="#e8edf3",
        body_text_color_subdued="#8d9aa8",
        body_text_color_subdued_dark="#8d9aa8",
        background_fill_primary="#090d12",
        background_fill_primary_dark="#090d12",
        background_fill_secondary="#10161e",
        background_fill_secondary_dark="#10161e",
        block_background_fill="#10161e",
        block_background_fill_dark="#10161e",
        block_border_color="#26303c",
        block_border_color_dark="#26303c",
        block_label_background_fill="#10161e",
        block_label_background_fill_dark="#10161e",
        block_label_text_color="#8d9aa8",
        block_label_text_color_dark="#8d9aa8",
        panel_background_fill="#10161e",
        panel_background_fill_dark="#10161e",
        panel_border_color="#26303c",
        panel_border_color_dark="#26303c",
        input_background_fill="#141c25",
        input_background_fill_dark="#141c25",
        input_background_fill_focus="#18222d",
        input_background_fill_focus_dark="#18222d",
        input_border_color="#26303c",
        input_border_color_dark="#26303c",
        input_border_color_focus="#ff9f43",
        input_border_color_focus_dark="#ff9f43",
        table_text_color="#e8edf3",
        table_text_color_dark="#e8edf3",
        table_border_color="#26303c",
        table_border_color_dark="#26303c",
        table_even_background_fill="#10161e",
        table_even_background_fill_dark="#10161e",
        table_odd_background_fill="#141c25",
        table_odd_background_fill_dark="#141c25",
        button_secondary_background_fill="#141c25",
        button_secondary_background_fill_dark="#141c25",
        button_secondary_border_color="#26303c",
        button_secondary_border_color_dark="#26303c",
        button_secondary_text_color="#e8edf3",
        button_secondary_text_color_dark="#e8edf3",
    )

    with gr.Blocks(
        title="ClawWatch Log Lab",
        fill_width=True,
        analytics_enabled=False,
    ) as demo:
        selected_event_id = gr.State(None)
        selected_card_revision = gr.State(0)
        refresh_timer = gr.Timer(config.replay.refresh_seconds, active=True)

        gr.HTML(header_html(str(config.storage.database)))
        with gr.Row():
            critical_auto_send = gr.Checkbox(
                label="Auto-send critical logs (NemoClaw server only)",
                value=auto_send_critical,
                info=(
                    "Sends complete logs to cyber-alerts during replay. "
                    "Pending logs retry automatically."
                ),
            )
            critical_log_status = gr.Textbox(
                label="Automatic critical-log delivery",
                value=critical_log_notifier.status,
                interactive=False,
            )
        critical_auto_send.input(
            lambda enabled: set_critical_delivery(enabled, controller, critical_log_notifier),
            inputs=critical_auto_send,
            outputs=[critical_auto_send, critical_log_status],
            concurrency_limit=1,
            concurrency_id="critical-log-delivery",
        )
        refresh_timer.tick(
            lambda: (controller.auto_send_critical, critical_log_notifier.status),
            outputs=[critical_auto_send, critical_log_status],
            concurrency_limit=1,
            concurrency_id="critical-log-delivery",
            show_progress="hidden",
        )
        status = gr.HTML()

        with gr.Group(elem_id="cw-controls"):
            with gr.Row(elem_id="cw-config-grid", equal_height=True):
                preset = gr.Dropdown(
                    choices=[("All events", "all"), ("Authentication", "authentication")],
                    value=config.replay.default_preset,
                    label="Replay preset",
                    scale=2,
                )
                limit = gr.Dropdown(
                    choices=["100", "1,000", "10,000", "All matching rows"],
                    value=f"{config.replay.default_limit:,}",
                    label="Record limit",
                    scale=1,
                )
                ordering = gr.Dropdown(
                    choices=[("Source file order", "source"), ("Original time", "original_time")],
                    value="source",
                    label="Ordering",
                    scale=2,
                )
                rate = gr.Dropdown(
                    choices=list(config.replay.available_rates),
                    value=config.replay.default_rate,
                    label="Events / second",
                    scale=1,
                )
                start_button = gr.Button("Start new run", variant="primary", scale=1)
            with gr.Row(elem_id="cw-transport-grid", equal_height=True):
                run_selector = gr.Dropdown(
                    choices=run_choices,
                    value=initial_run_id,
                    allow_custom_value=True,
                    label="Displayed / controlled run",
                    scale=4,
                )
                pause_button = gr.Button("Pause", size="sm")
                resume_button = gr.Button("Resume", size="sm")
                stop_button = gr.Button("Stop", variant="stop", size="sm")
                apply_rate_button = gr.Button("Apply rate", size="sm")

        notice = gr.HTML(
            notice_html(
                f"Recovered interrupted runs: {', '.join(map(str, recovered))}"
                if recovered
                else "Ready. Dashboard reads committed SQLite events only."
            )
        )
        metrics = gr.HTML()

        with gr.Tabs():
            with gr.Tab("Monitor"):
                with gr.Row(elem_id="cw-chart-grid", equal_height=True):
                    timeline = gr.LinePlot(
                        x="time",
                        y="events",
                        title="Emissions · last five minutes",
                        x_title="Emission time (UTC)",
                        y_title="Events / second",
                        height=280,
                        scale=2,
                        elem_classes="cw-chart",
                    )
                    severity_chart = gr.BarPlot(
                        x="severity",
                        y="events",
                        title="Severity annotations",
                        x_label_angle=-32,
                        height=280,
                        scale=1,
                        elem_classes="cw-chart",
                    )
                    category_chart = gr.BarPlot(
                        x="event_type",
                        y="events",
                        title="Event categories",
                        x_label_angle=-32,
                        height=280,
                        scale=1,
                        elem_classes="cw-chart",
                    )

                with gr.Row(elem_id="cw-filter-grid", equal_height=True):
                    event_type_filter = gr.Dropdown(
                        choices=event_types,
                        multiselect=True,
                        label="Event type",
                        scale=2,
                    )
                    severity_filter = gr.Dropdown(
                        choices=severities,
                        multiselect=True,
                        label="Severity",
                        scale=2,
                    )
                    search = gr.Textbox(
                        label="Literal event search",
                        placeholder="Event ID, actor, IP, message, or raw log",
                        scale=4,
                    )

                with gr.Row(elem_id="cw-event-workspace", equal_height=False):
                    feed = gr.Dataframe(
                        headers=[
                            "replay_event_id",
                            "sequence",
                            "emitted_at",
                            "severity",
                            "event_type",
                            "actor",
                            "source_ip",
                            "message",
                        ],
                        datatype=["number", "number", "str", "str", "str", "str", "str", "str"],
                        label="Committed live feed · select any cell for details",
                        interactive=False,
                        wrap=False,
                        max_height=560,
                        show_search="filter",
                        scale=3,
                        elem_id="cw-feed",
                    )
                    with gr.Column(scale=2):
                        event_metadata = gr.JSON(label="Selected event and replay envelope")
                        raw_log = gr.Code(
                            label="Raw log",
                            language=None,
                            lines=5,
                            interactive=False,
                        )
                        original_payload = gr.JSON(label="Original JSON payload")
                        add_review_button = gr.Button("Add selected event to review")

            with gr.Tab("Review board"):
                gr.Markdown(
                    "Open **critical** cards across all runs are sent to Slack automatically "
                    "every 30 seconds while the server runs. Successful sends are recorded "
                    "in History and skipped on future checks."
                )
                send_slack_button = gr.Button("Send critical alerts to Slack")
                slack_notice = gr.HTML(callbacks.slack_status())
                board = gr.HTML(
                    board_html([], {stage: 0 for stage in REVIEW_STAGES}),
                    elem_id="cw-board-wrap",
                )
                with gr.Row(elem_id="cw-review-editor", equal_height=True):
                    card_selector = gr.Dropdown(
                        choices=[],
                        allow_custom_value=True,
                        label="Review card",
                        scale=3,
                    )
                    card_stage = gr.Dropdown(
                        choices=[
                            ("New", "new"),
                            ("Investigating", "investigating"),
                            ("Resolved", "resolved"),
                            ("Dismissed", "dismissed"),
                        ],
                        value="new",
                        label="Move to",
                        scale=2,
                    )
                card_notes = gr.Textbox(
                    label="Review notes",
                    lines=5,
                    max_lines=10,
                    placeholder="Record the manual investigation decision.",
                )
                save_card_button = gr.Button("Save review card", variant="primary")

            with gr.Tab("History"):
                run_summary = gr.JSON(label="Selected run")
                run_history = gr.Dataframe(
                    label="Replay runs",
                    interactive=False,
                    wrap=False,
                    max_height=320,
                )
                activity = gr.Dataframe(
                    label="Latest run and review activity",
                    interactive=False,
                    wrap=False,
                    max_height=420,
                )

        refresh_inputs = [
            run_selector,
            event_type_filter,
            severity_filter,
            search,
            card_selector,
        ]
        refresh_outputs = [
            status,
            metrics,
            timeline,
            severity_chart,
            category_chart,
            feed,
            board,
            card_selector,
            run_selector,
            run_history,
            activity,
        ]

        def wire_refresh(event):
            return event.then(
                callbacks.refresh,
                inputs=refresh_inputs,
                outputs=refresh_outputs,
                show_progress="hidden",
                concurrency_limit=1,
                concurrency_id="dashboard-refresh",
            )

        start_event = start_button.click(
            callbacks.start_run,
            inputs=[preset, limit, ordering, rate],
            outputs=[run_selector, notice],
            concurrency_limit=1,
            concurrency_id="replay-controls",
        )
        wire_refresh(start_event)

        for button, callback in (
            (pause_button, callbacks.pause_run),
            (resume_button, callbacks.resume_run),
            (stop_button, callbacks.stop_run),
        ):
            control_event = button.click(
                callback,
                inputs=run_selector,
                outputs=notice,
                concurrency_limit=1,
                concurrency_id="replay-controls",
            )
            wire_refresh(control_event)

        rate_event = apply_rate_button.click(
            callbacks.apply_rate,
            inputs=[run_selector, rate],
            outputs=notice,
            concurrency_limit=1,
            concurrency_id="replay-controls",
        )
        wire_refresh(rate_event)

        for component in (run_selector, event_type_filter, severity_filter):
            component.input(
                callbacks.refresh,
                inputs=refresh_inputs,
                outputs=refresh_outputs,
                show_progress="hidden",
                concurrency_limit=1,
                concurrency_id="dashboard-refresh",
                trigger_mode="always_last",
            )
        search.submit(
            callbacks.refresh,
            inputs=refresh_inputs,
            outputs=refresh_outputs,
            show_progress="hidden",
            concurrency_limit=1,
            concurrency_id="dashboard-refresh",
        )
        refresh_timer.tick(
            callbacks.refresh,
            inputs=refresh_inputs,
            outputs=refresh_outputs,
            show_progress="hidden",
            concurrency_limit=1,
            concurrency_id="dashboard-refresh",
            trigger_mode="always_last",
        )

        feed.select(
            callbacks.select_event,
            inputs=feed,
            outputs=[selected_event_id, event_metadata, raw_log, original_payload, notice],
            show_progress="hidden",
        )
        add_event = add_review_button.click(
            callbacks.add_selected_to_review,
            inputs=[selected_event_id, run_selector],
            outputs=[
                board,
                card_selector,
                card_stage,
                card_notes,
                selected_card_revision,
                notice,
            ],
            concurrency_limit=1,
            concurrency_id="review-controls",
        )
        wire_refresh(add_event)
        add_event.then(
            callbacks.send_critical_alerts,
            outputs=slack_notice,
            concurrency_limit=1,
            concurrency_id="slack-alerts",
        )
        send_slack_button.click(
            callbacks.send_critical_alerts,
            outputs=slack_notice,
            concurrency_limit=1,
            concurrency_id="slack-alerts",
        )
        refresh_timer.tick(
            callbacks.slack_status,
            outputs=slack_notice,
            show_progress="hidden",
        )
        card_selector.input(
            callbacks.load_review_card,
            inputs=card_selector,
            outputs=[card_stage, card_notes, selected_card_revision, notice],
            show_progress="hidden",
        )
        save_event = save_card_button.click(
            callbacks.save_review_card,
            inputs=[
                card_selector,
                card_stage,
                card_notes,
                selected_card_revision,
                run_selector,
            ],
            outputs=[
                card_stage,
                card_notes,
                selected_card_revision,
                notice,
                board,
                card_selector,
            ],
            concurrency_limit=1,
            concurrency_id="review-controls",
        )
        wire_refresh(save_event)

        run_selector.input(
            callbacks.selected_run_summary,
            inputs=run_selector,
            outputs=run_summary,
            show_progress="hidden",
        )
        demo.load(
            callbacks.refresh,
            inputs=refresh_inputs,
            outputs=refresh_outputs,
            show_progress="hidden",
            concurrency_limit=1,
            concurrency_id="dashboard-refresh",
        )
        demo.load(
            callbacks.selected_run_summary,
            inputs=run_selector,
            outputs=run_summary,
            show_progress="hidden",
        )

    return demo, DashboardRuntime(
        controller=controller,
        slack_notifier=slack_notifier,
        critical_log_notifier=critical_log_notifier,
        recovered_run_ids=recovered,
        theme=theme,
        css=css,
    )


def launch_dashboard(config: AppConfig, *, auto_send_critical: bool = True) -> int:
    with application_lock(config.storage.database):
        demo, runtime = create_app(config, auto_send_critical=auto_send_critical)
        try:
            if auto_send_critical:
                runtime.critical_log_notifier.start()
            runtime.slack_notifier.start()
            demo.queue(default_concurrency_limit=4).launch(
                server_name=config.server.host,
                server_port=config.server.port,
                share=False,
                show_error=True,
                quiet=False,
                footer_links=[],
                theme=runtime.theme,
                css=runtime.css,
            )
        finally:
            runtime.controller.close()
            runtime.critical_log_notifier.close()
            runtime.slack_notifier.close()
    return 0

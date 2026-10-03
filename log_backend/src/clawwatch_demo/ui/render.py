"""Safe HTML renderers for dashboard summaries and review cards."""

from __future__ import annotations

from html import escape

from clawwatch_demo.models import ReviewCard
from clawwatch_demo.monitoring import DashboardSnapshot

STAGE_LABELS = {
    "new": "New",
    "investigating": "Investigating",
    "resolved": "Resolved",
    "dismissed": "Dismissed",
}


def header_html(database: str) -> str:
    return f"""
    <header class="cw-header">
      <div class="cw-brand">
        <div class="cw-system-id"><span>SYS / CLW-01</span><span>LOCAL TELEMETRY</span></div>
        <p class="cw-kicker">SECURITY EVENT REPLAY CONSOLE</p>
        <h1>CLAWWATCH<span>/LOG LAB</span></h1>
      </div>
      <div class="cw-db">
        <div><span class="cw-db-led"></span><b>SQLITE / ONLINE</b></div>
        <code>{escape(database)}</code>
        <small>MODE / SYNTHETIC · NETWORK / LOCALHOST</small>
      </div>
    </header>
    """


def notice_html(message: str, *, kind: str = "info") -> str:
    return f'<div class="cw-notice {escape(kind)}">{escape(message)}</div>'


def status_html(snapshot: DashboardSnapshot) -> str:
    run = "No run selected" if snapshot.run_id is None else f"Run #{snapshot.run_id}"
    state_class = escape(snapshot.state.lower().replace(" ", "-"))
    error = (
        f'<span class="cw-status-error">{escape(snapshot.last_error)}</span>'
        if snapshot.last_error
        else ""
    )
    return f"""
    <div class="cw-statusbar">
      <span class="cw-led {state_class}"></span>
      <b>{escape(run)}</b>
      <span class="cw-state">{escape(snapshot.state.upper())}</span>
      <span>{snapshot.rate:g} events/s configured</span>
      <span>Updated {escape(snapshot.updated_at)}</span>
      {error}
    </div>
    """


def metrics_html(snapshot: DashboardSnapshot) -> str:
    metrics = (
        ("Committed events", f"{snapshot.emitted_count:,}", "RUN TOTAL"),
        ("Actual throughput", f"{snapshot.actual_rate:.1f}/s", "LAST 10 SECONDS"),
        ("High+ annotations", f"{snapshot.high_annotation_count:,}", "DATASET LABELS"),
        ("Open reviews", f"{snapshot.open_review_count:,}", "NEW + INVESTIGATING"),
    )
    cards = "".join(
        f"""
        <article class="cw-metric">
          <span>{escape(label)}</span>
          <strong>{escape(value)}</strong>
          <small>{escape(caption)}</small>
        </article>
        """
        for label, value, caption in metrics
    )
    return f'<section class="cw-metrics">{cards}</section>'


def board_html(cards: list[ReviewCard], counts: dict[str, int]) -> str:
    grouped = {stage: [] for stage in STAGE_LABELS}
    for card in cards:
        grouped[card.stage].append(card)

    columns: list[str] = []
    for stage, label in STAGE_LABELS.items():
        rendered_cards = []
        for card in grouped[stage]:
            actor = card.actor or card.source_ip or "Unknown actor"
            rendered_cards.append(
                f"""
                <article class="cw-kanban-card severity-{escape(card.severity.lower())}">
                  <div class="cw-card-top">
                    <span>{escape(card.severity.upper())}</span>
                    <code>#{card.id}</code>
                  </div>
                  <h4>{escape(card.message)}</h4>
                  <p>{escape(card.event_type)} · {escape(actor)}</p>
                  <div><code>{escape(card.event_id)}</code></div>
                  <small>Run {card.run_id} / Seq {card.sequence} · rev {card.revision}</small>
                </article>
                """
            )
        empty = '<div class="cw-board-empty">No cards in this stage</div>'
        columns.append(
            f"""
            <section class="cw-kanban-column stage-{escape(stage)}">
              <header><span>{escape(label)}</span><b>{counts.get(stage, 0)}</b></header>
              <div class="cw-card-stack">{"".join(rendered_cards) or empty}</div>
            </section>
            """
        )
    return f'<div class="cw-kanban">{"".join(columns)}</div>'

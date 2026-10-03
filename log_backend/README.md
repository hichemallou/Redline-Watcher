# ClawWatch Log Lab

ClawWatch Log Lab is a local Python demo for replaying synthetic SIEM events into SQLite
and monitoring them through a Gradio dashboard. Development follows the reviewed
[phase plan](docs/gradio-live-log-demo-plan.md).

## Current status

All six planned phases are complete: Python 3.12 packaging, configuration, dependency locks,
versioned SQLite storage, the pandas-backed importer, durable replay, the Gradio monitoring
dashboard, the persistent review board, and final performance and browser validation.

## Development setup

Run the idempotent setup script from the repository root:

```bash
./scripts/setup.sh
```

It creates the Python 3.12 environment, installs the development lockfile and editable
package, protects or creates `.env`, verifies the pinned dataset and downloads it from
Hugging Face when missing, validates the configuration, and safely imports the dataset. Use
`--skip-import` to omit the SQLite import or `--runtime-only` to install the runtime lockfile.

Equivalent manual commands:

```bash
rtk proxy uv venv --python 3.12 --seed .venv
rtk proxy .venv/bin/python -m pip install -r requirements-dev.lock
rtk proxy .venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

`uv venv` is used because this machine's managed Python 3.12 installation cannot run
`ensurepip` correctly through `python3.12 -m venv`. The resulting `.venv` is a normal,
isolated CPython 3.12 virtual environment.

Validate the checked-in configuration:

```bash
rtk proxy .venv/bin/clawwatch-demo config-check --config config/demo.toml
```

The configured database path resolves to
`/Users/binzhang/vibe_coding_repo/hackathon_dell/var/clawwatch_demo.sqlite3` and is excluded
from Git.

Import the configured JSONL dataset or safely re-run an existing completed import:

```bash
rtk proxy .venv/bin/clawwatch-demo import-data --config config/demo.toml
rtk proxy .venv/bin/clawwatch-demo db-info --config config/demo.toml
```

The importer reads bounded 10,000-row batches with pandas, validates and normalizes each
record, bulk-inserts accepted events, and falls back to isolated JSON parsing when a batch
contains malformed input. A clean 100,000-row import measured 4.04 seconds (about 24,778
rows/second) on the development machine. The complete original JSON line remains stored
alongside normalized query fields.

The replay engine in `clawwatch_demo.replay` provides fixed-rate source or original-time
ordering, event-type and severity selection, record limits, and acknowledged start, pause,
resume, rate-change, stop, and interruption controls. Each emitted event and its selection
cursor commit in one transaction. Restart recovery marks an unfinished producer as
interrupted and resumes from its last committed sequence only after an explicit command.

Launch the local dashboard:

```bash
./scripts/start_gradio.sh
```

To use another configuration file, run
`./scripts/start_gradio.sh --config path/to/config.toml`. The server remains attached to
the terminal and stops cleanly with `Ctrl+C`.

Open `http://127.0.0.1:7860`. The monitor provides replay controls, live counters, pandas-
backed charts and tables, literal search, event details, original payload inspection, run
history, and a four-stage review board. Review notes, moves, revision checks, and activity
history persist in the repository SQLite database.

Send an alert to Slack using the bot token and channel configured in `.env`:

```bash
./scripts/send_slack.sh "[CRITICAL] AI agent detected suspicious privilege escalation"
```

Use `--dry-run` to validate the configuration without contacting Slack, `--channel C123...`
to override the configured channel, or pipe a generated message through standard input:

```bash
./scripts/send_slack.sh "configuration test" --dry-run
printf '%s\n' "[CRITICAL] Generated event" | ./scripts/send_slack.sh --stdin
```

The script never prints the token. Slack failures such as `invalid_auth`, `missing_scope`, or
`not_in_channel` are returned as readable errors and a nonzero exit status.

The review board automatically checks every 30 seconds while the dashboard server runs,
even with the browser closed. Cards with severity `critical` (case insensitive) in **New** or
**Investigating** are sent to the configured Slack channel across all replay runs. Adding a
card from the monitor also triggers a check. Resolved, dismissed, and noncritical cards are
skipped. The **Send critical alerts to Slack** button runs the same check immediately.

To call that same operation from another button or a terminal:

```bash
./scripts/send_critical_reviews.sh
./scripts/send_critical_reviews.sh --dry-run
./scripts/send_critical_reviews.sh --config config/demo.toml --env-file .env
```

Each check handles up to 20 pending cards, oldest first; later checks drain any backlog.
Dry runs validate Slack settings and report candidate card IDs without sending or marking
them delivered. Successful sends appear as `review_slack_sent` in History and persist across
restarts, so refreshes, repeated clicks, and concurrent script calls skip delivered cards.
Failures remain pending for the next check and appear in the board's Slack status. A crash
between Slack accepting a message and the local audit commit can still cause a duplicate.
The button and script share `clawwatch_demo.review_slack.send_critical_reviews` directly.

To send complete critical logs automatically as replay emits them, run the backend
**on the NemoClaw server** and turn on **Auto-send critical logs** at the top of the
Gradio dashboard. Delivery status appears beside the checkbox. Turning it off stops
new sends after the current send finishes and retains pending logs for later.
The checkbox starts off on a normal launch; to start with it already enabled, use:

```bash
./scripts/start_gradio.sh --auto-send-critical
```

This queues each critical replay event durably and calls the bundled NemoClaw sender
in a background worker, without requiring a review card or manual pipe. Failed sends
retry after 30 seconds; successful sends are tracked across restarts. The normal local
startup leaves this delivery path disabled. See
[`docs/remote-critical-log-script.md`](docs/remote-critical-log-script.md) for setup,
delivery semantics, and standalone use with another log generator.

The final stress checks sustained 99.9 events/second for a real-time 1,000-event run and
persisted a controllable-clock, full-corpus replay of 100,000 unique sequences and source
events. Five dashboard snapshots over that full run took 0.250–0.259 seconds each. The UI
was inspected at 1440×900 and 1280×800 without horizontal page overflow, and the browser
walkthrough covered event details, idempotent review creation, note/stage persistence, stale
revision protection, and activity history.

Run the automated checks:

```bash
rtk proxy .venv/bin/python -m pytest
rtk proxy .venv/bin/python -m ruff check .
rtk proxy .venv/bin/python -m ruff format --check .
```

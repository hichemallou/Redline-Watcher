"""Deliver replay's durable critical-log outbox on the NemoClaw host."""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from clawwatch_demo.storage import connect, immediate_transaction

logger = logging.getLogger(__name__)

SEND_ERRORS = {
    "executable_missing": "NemoClaw executable not found. Set NEMOCLAW_BIN to its absolute path.",
    "permission_denied": "Permission denied launching NemoClaw; check its executable permissions.",
    "timeout": "NemoClaw timed out; Slack delivery is unconfirmed.",
    "launch_failed": "Could not launch NemoClaw.",
    "terminal_required": "NemoClaw reported that a terminal is required.",
    "sandbox_not_found": "NemoClaw reported that the sandbox was not found.",
    "slack_auth": "Slack rejected authentication; check the sandbox Slack credentials.",
    "slack_channel": "Slack channel is missing or the bot is not a member.",
    "slack_scope": "Slack reported a missing permission scope.",
    "gateway_connection": "NemoClaw could not connect to its gateway; check port 8991.",
    "dependency_missing": "NemoClaw reported a missing executable or file; check its runtime PATH.",
    "invalid_input": "The sender could not read or parse the queued JSON log.",
}


def _sender_error(result: subprocess.CompletedProcess) -> str:
    try:
        detail = json.loads(result.stderr)
        code = detail.get("error_code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in SEND_ERRORS:
            return SEND_ERRORS[code]
    except (json.JSONDecodeError, TypeError):
        pass
    return f"Sender failed with exit code {result.returncode}; inspect NemoClaw on the server."


class CriticalLogNotifier:
    """Run the bundled sender outside replay transactions; retry pending records."""

    def __init__(self, database: Path, sender: Path) -> None:
        self.database = database.resolve()
        self.sender = sender
        self.status = "Critical logs: automatic sending enabled; waiting for replay."
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> int:
        # Serialize dispatchers without holding a SQLite writer during delivery.
        with Path(f"{self.database}.critical-logs.lock").open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            connection = connect(self.database)
            sent = 0
            try:
                rows = connection.execute(
                    """
                    SELECT q.replay_event_id, re.run_id, se.original_json
                    FROM critical_log_outbox q
                    JOIN replay_events re ON re.id = q.replay_event_id
                    JOIN source_events se ON se.id = re.source_event_id
                    WHERE q.sent_at IS NULL AND q.next_attempt_at <= ?
                    ORDER BY q.next_attempt_at, q.replay_event_id LIMIT 20
                    """,
                    (time.time(),),
                ).fetchall()
                for row in rows:
                    if self._stop.is_set():
                        break
                    error = None
                    try:
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(self.sender),
                                "--severity",
                                "critical",
                                "--result-json",
                            ],
                            input=row["original_json"],
                            capture_output=True,
                            text=True,
                            timeout=95,
                            check=False,
                        )
                        if result.returncode:
                            error = _sender_error(result)
                    except subprocess.TimeoutExpired:
                        error = "Sender timed out; delivery unconfirmed"
                    except OSError:
                        error = "Could not launch critical-log sender"
                    now = datetime.now(UTC).isoformat()
                    with immediate_transaction(connection):
                        connection.execute(
                            """
                            UPDATE critical_log_outbox
                            SET attempts = attempts + 1, next_attempt_at = ?,
                                sent_at = ?, last_error = ?
                            WHERE replay_event_id = ?
                            """,
                            (
                                time.time() + 30 if error else 0,
                                None if error else now,
                                error,
                                row["replay_event_id"],
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO activity_log(run_id, action, occurred_at, detail_json)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                row["run_id"],
                                "critical_log_failed" if error else "critical_log_sent",
                                now,
                                json.dumps(
                                    {"replay_event_id": row["replay_event_id"], "error": error}
                                ),
                            ),
                        )
                    if error:
                        logger.warning("Critical log #%s: %s", row["replay_event_id"], error)
                    else:
                        sent += 1
                pending = connection.execute(
                    "SELECT COUNT(*), COUNT(last_error) FROM critical_log_outbox "
                    "WHERE sent_at IS NULL"
                ).fetchone()
                total_sent = connection.execute(
                    "SELECT COUNT(*) FROM critical_log_outbox WHERE sent_at IS NOT NULL"
                ).fetchone()[0]
                last_failure = connection.execute(
                    "SELECT last_error FROM critical_log_outbox "
                    "WHERE sent_at IS NULL AND last_error IS NOT NULL "
                    "ORDER BY next_attempt_at DESC LIMIT 1"
                ).fetchone()
                self.status = (
                    f"Critical logs: {total_sent} delivered; {pending[0]} pending "
                    f"({pending[1]} awaiting retry)."
                )
                if last_failure:
                    self.status += f" Last error: {last_failure[0]}"
                return sent
            finally:
                connection.close()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.sender.is_file():
            raise RuntimeError(f"Critical-log sender is missing: {self.sender}")
        self._stop.clear()
        self.status = "Critical logs: automatic sending enabled; checking pending logs."

        def poll() -> None:
            while not self._stop.is_set():
                try:
                    self.check()
                except (OSError, sqlite3.Error):
                    self.status = "Critical logs: queue check failed; retrying."
                    logger.warning(self.status)
                self._stop.wait(1)

        self._thread = threading.Thread(target=poll, name="critical-logs", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # An in-flight send has a bounded 95-second timeout. It must finish
            # before the application releases its lock to a replacement process.
            self._thread.join()

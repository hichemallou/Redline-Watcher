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
                            [sys.executable, str(self.sender), "--severity", "critical"],
                            input=row["original_json"],
                            capture_output=True,
                            text=True,
                            timeout=95,
                            check=False,
                        )
                        if result.returncode:
                            error = f"Sender failed with exit code {result.returncode}"
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
                self.status = (
                    f"Critical logs: {sent} sent this check; {pending[0]} pending "
                    f"({pending[1]} awaiting retry)."
                )
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

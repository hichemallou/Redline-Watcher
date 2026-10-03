BEGIN IMMEDIATE;

CREATE TABLE critical_log_outbox (
    replay_event_id INTEGER PRIMARY KEY REFERENCES replay_events(id) ON DELETE RESTRICT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    sent_at TEXT,
    last_error TEXT
);
CREATE INDEX idx_critical_log_outbox_pending
    ON critical_log_outbox(next_attempt_at, replay_event_id) WHERE sent_at IS NULL;

COMMIT;

BEGIN IMMEDIATE;

CREATE TABLE datasets (
    id INTEGER PRIMARY KEY,
    repository_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    file_sha256 TEXT NOT NULL UNIQUE CHECK (length(file_sha256) = 64),
    source_path TEXT NOT NULL,
    file_bytes INTEGER NOT NULL CHECK (file_bytes >= 0),
    status TEXT NOT NULL CHECK (status IN ('importing', 'complete', 'failed')),
    committed_line_cursor INTEGER NOT NULL DEFAULT 0 CHECK (committed_line_cursor >= 0),
    total_lines INTEGER CHECK (total_lines IS NULL OR total_lines >= 0),
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT
);

CREATE TABLE source_events (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE RESTRICT,
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    event_id TEXT NOT NULL,
    original_timestamp TEXT NOT NULL,
    parsed_timestamp TEXT NOT NULL,
    timestamp_has_timezone INTEGER NOT NULL CHECK (timestamp_has_timezone IN (0, 1)),
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    actor TEXT,
    source_ip TEXT,
    action TEXT,
    status TEXT,
    source_product TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_log TEXT NOT NULL,
    original_json TEXT NOT NULL,
    UNIQUE (dataset_id, event_id),
    UNIQUE (dataset_id, line_number)
);

CREATE TABLE import_errors (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE RESTRICT,
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    original_line TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (dataset_id, line_number)
);

CREATE TABLE replay_runs (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE RESTRICT,
    selection_json TEXT NOT NULL,
    ordering TEXT NOT NULL CHECK (ordering IN ('source', 'original_time')),
    record_limit INTEGER CHECK (record_limit IS NULL OR record_limit > 0),
    rate REAL NOT NULL CHECK (rate > 0),
    state TEXT NOT NULL CHECK (
        state IN ('ready', 'running', 'paused', 'completed', 'stopped', 'failed', 'interrupted')
    ),
    committed_selection_cursor INTEGER NOT NULL DEFAULT 0,
    emitted_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    last_error TEXT
);

CREATE TABLE replay_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES replay_runs(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    source_event_id INTEGER NOT NULL REFERENCES source_events(id) ON DELETE RESTRICT,
    simulated_at TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE TABLE review_cards (
    id INTEGER PRIMARY KEY,
    replay_event_id INTEGER NOT NULL UNIQUE REFERENCES replay_events(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (stage IN ('new', 'investigating', 'resolved', 'dismissed')),
    notes TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES replay_runs(id) ON DELETE RESTRICT,
    card_id INTEGER REFERENCES review_cards(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    prior_state TEXT,
    new_state TEXT,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_source_events_dataset_type_line
    ON source_events(dataset_id, event_type, line_number);
CREATE INDEX idx_source_events_dataset_severity_line
    ON source_events(dataset_id, severity, line_number);
CREATE INDEX idx_source_events_dataset_timestamp_line
    ON source_events(dataset_id, parsed_timestamp, line_number);
CREATE INDEX idx_source_events_dataset_actor
    ON source_events(dataset_id, actor) WHERE actor IS NOT NULL;
CREATE INDEX idx_source_events_dataset_source_ip
    ON source_events(dataset_id, source_ip) WHERE source_ip IS NOT NULL;
CREATE INDEX idx_replay_events_run_sequence ON replay_events(run_id, sequence);
CREATE INDEX idx_replay_events_run_emitted ON replay_events(run_id, emitted_at);
CREATE INDEX idx_review_cards_stage_updated ON review_cards(stage, updated_at);
CREATE INDEX idx_activity_log_run_time ON activity_log(run_id, occurred_at);
CREATE INDEX idx_activity_log_card_time ON activity_log(card_id, occurred_at);

COMMIT;

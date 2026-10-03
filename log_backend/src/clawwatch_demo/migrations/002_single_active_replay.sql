BEGIN IMMEDIATE;

CREATE UNIQUE INDEX uq_replay_runs_single_active
    ON replay_runs((1))
    WHERE state IN ('ready', 'running', 'paused', 'interrupted');

COMMIT;

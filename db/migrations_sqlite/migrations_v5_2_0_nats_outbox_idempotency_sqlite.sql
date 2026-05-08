-- SQLite mirror for db/migrations_v5_2_0_nats_outbox_idempotency.sql.
-- TIMESTAMPTZ -> TEXT (ISO-8601 UTC); DEFAULT NOW() -> datetime('now').

CREATE TABLE IF NOT EXISTS nats_dispatch_log (
    event_id      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    dispatched_at TEXT NOT NULL DEFAULT (datetime('now')),

    PRIMARY KEY (event_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_nats_dispatch_log_dispatched_at
    ON nats_dispatch_log (dispatched_at DESC);

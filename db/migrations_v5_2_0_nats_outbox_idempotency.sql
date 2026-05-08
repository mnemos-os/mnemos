-- MNEMOS v5.2.0 NATS substrate v0.3 consumer idempotency.
--
-- NATS delivers at least once. Consumers insert (event_id, subject) before
-- applying side effects so redeliveries can be acknowledged as duplicates.

BEGIN;

CREATE TABLE IF NOT EXISTS nats_dispatch_log (
    event_id      TEXT        NOT NULL,
    subject       TEXT        NOT NULL,
    dispatched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (event_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_nats_dispatch_log_dispatched_at
    ON nats_dispatch_log (dispatched_at DESC);

COMMENT ON TABLE nats_dispatch_log IS
    'Consumer-side idempotency log for NATS at-least-once deliveries.';

COMMIT;

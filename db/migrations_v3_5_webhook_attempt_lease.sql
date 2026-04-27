-- migrations_v3_5_webhook_attempt_lease.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 3) — persisted webhook attempt leases.
--
-- Webhook sends must not hold a PostgreSQL connection while DNS validation or
-- outbound HTTP is in flight. Runtime workers now claim an attempt by writing
-- lease_token + lease_expires_at in a short transaction, release the
-- connection, and finalize only if the same unexpired token still owns the row.
-- ---------------------------------------------------------------------------

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS lease_token UUID NULL,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN webhook_deliveries.lease_token IS
    'Worker-owned delivery claim token; NULL when no live worker owns the attempt.';

COMMENT ON COLUMN webhook_deliveries.lease_expires_at IS
    'Absolute time when a live webhook delivery claim expires and may be recovered.';

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at
    ON webhook_deliveries(lease_expires_at);

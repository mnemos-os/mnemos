-- migrations_v3_5_webhook_superseded_marker.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 8) — old-worker-compatible superseded retry marker.
--
-- Superseded retry attempts keep status='abandoned' so older v3.5-dev
-- workers skip them as terminal. The separate boolean preserves audit
-- semantics:
--   superseded=TRUE  and status='abandoned' = retry chain advanced
--   superseded=FALSE and status='abandoned' = final failure or revoked
-- Existing rows default to FALSE; the startup repair sweep and the backfill
-- below mark rows that already have a newer attempt in the same retry chain.
-- ---------------------------------------------------------------------------

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN webhook_deliveries.superseded IS
    'TRUE when this abandoned webhook delivery attempt was superseded by a later retry attempt.';

COMMENT ON COLUMN webhook_deliveries.status IS
    'pending | retrying | succeeded | abandoned';

-- Convert any rows produced by the pre-round-8 branch status and mark rows
-- that are already known to have advanced to a later retry attempt.
UPDATE webhook_deliveries d
SET status = 'abandoned',
    superseded = TRUE,
    lease_token = NULL,
    lease_expires_at = NULL
WHERE (
    d.status IN ('retrying', 'retry_scheduled')
    OR (d.status = 'abandoned' AND NOT d.superseded)
  )
  AND EXISTS (
    SELECT 1
    FROM webhook_deliveries newer
    WHERE newer.subscription_id = d.subscription_id
      AND newer.event_type = d.event_type
      AND newer.payload_hash = d.payload_hash
      AND newer.attempt_num > d.attempt_num
  );

-- Remove the branch-only terminal status even if its successor was manually
-- deleted before this migration. It remains superseded for audit because that
-- status was only ever written after scheduling a successor.
UPDATE webhook_deliveries
SET status = 'abandoned',
    superseded = TRUE,
    lease_token = NULL,
    lease_expires_at = NULL
WHERE status = 'retry_scheduled';

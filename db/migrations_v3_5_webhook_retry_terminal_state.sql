-- migrations_v3_5_webhook_retry_terminal_state.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 task #20) — make superseded webhook retry attempts terminal.
--
-- The webhook_deliveries table is row-per-attempt. Retryable failures enqueue
-- a successor row with attempt_num + 1. Older builds left the failed row in
-- status='retrying', and the recovery worker selected due retrying rows, so a
-- superseded attempt could replay forever alongside its successor.
--
-- New runtime status:
--   retry_scheduled = terminal failed attempt; a later pending attempt exists.
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN webhook_deliveries.status IS
    'pending | retrying | succeeded | retry_scheduled | abandoned';

-- One-time data repair: terminalize retrying rows that already have a newer
-- attempt for the same subscription/event/payload chain.
UPDATE webhook_deliveries d
SET status = 'retry_scheduled'
WHERE d.status = 'retrying'
  AND EXISTS (
    SELECT 1
    FROM webhook_deliveries newer
    WHERE newer.subscription_id = d.subscription_id
      AND newer.event_type = d.event_type
      AND newer.payload_hash = d.payload_hash
      AND newer.attempt_num > d.attempt_num
  );

-- Keep the recovery-worker index aligned with live statuses. retry_scheduled
-- is intentionally excluded; retrying stays indexed so crash recovery can
-- reclaim in-flight rows that do not yet have a successor.
DROP INDEX IF EXISTS idx_webhook_deliveries_pending;
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending
    ON webhook_deliveries(scheduled_at)
    WHERE status IN ('pending', 'retrying');

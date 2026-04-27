-- migrations_v3_5_webhook_attempt_unique.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 8) — one live row per webhook retry-chain attempt.
--
-- webhook_deliveries has no event_id column. The retry chain key already used
-- by recovery and advisory locks is subscription_id + event_type + payload_hash;
-- attempt_num identifies each attempt within that chain.
-- ---------------------------------------------------------------------------

-- Keep the newest live duplicate and terminalize older live duplicates before
-- adding the invariant. Historical terminal rows are intentionally left alone.
WITH ranked_live_attempts AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY subscription_id, event_type, payload_hash, attempt_num
            ORDER BY created DESC, id DESC
        ) AS live_rank
    FROM webhook_deliveries
    WHERE status IN ('pending', 'retrying')
      AND NOT superseded
),
duplicate_live_attempts AS (
    SELECT id
    FROM ranked_live_attempts
    WHERE live_rank > 1
)
UPDATE webhook_deliveries d
SET status = 'abandoned',
    superseded = TRUE,
    lease_token = NULL,
    lease_expires_at = NULL,
    error = COALESCE(d.error, 'superseded duplicate live retry attempt')
FROM duplicate_live_attempts dup
WHERE d.id = dup.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt
    ON webhook_deliveries(subscription_id, event_type, payload_hash, attempt_num)
    WHERE status IN ('pending', 'retrying') AND NOT superseded;

-- migrations_v3_5_webhook_succeeded_unique.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 14) — one succeeded row per webhook retry chain.
--
-- The retry chain key is subscription_id + event_type + payload_hash. Once any
-- attempt in that chain reaches status='succeeded', every other active or
-- terminal duplicate must converge to abandoned/superseded instead of creating
-- another canonical success row.
-- ---------------------------------------------------------------------------

-- One-time data repair for deployments that already have duplicate succeeded
-- terminal rows. Keep the earliest succeeded attempt by attempt_num, and leave
-- response_status / response_body / error / delivered_at untouched on the rows
-- that are converted for audit.
WITH ranked_succeeded_attempts AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY subscription_id, event_type, payload_hash
            ORDER BY attempt_num ASC, created ASC, id ASC
        ) AS succeeded_rank
    FROM webhook_deliveries
    WHERE status = 'succeeded'
),
duplicate_succeeded_attempts AS (
    SELECT id
    FROM ranked_succeeded_attempts
    WHERE succeeded_rank > 1
)
UPDATE webhook_deliveries AS d
SET status = 'abandoned',
    superseded = TRUE,
    lease_token = NULL,
    lease_expires_at = NULL
FROM duplicate_succeeded_attempts dup
WHERE d.id = dup.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain
    ON webhook_deliveries(subscription_id, event_type, payload_hash)
    WHERE status = 'succeeded';

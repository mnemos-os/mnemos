-- migrations_v3_5_webhook_status_updated_at.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 7) — status-transition clock for webhook recovery grace.
--
-- Lease-less legacy rows use WEBHOOK_LEGACY_GRACE_SECONDS before new recovery
-- workers may claim them. For retrying rows, scheduled_at is the original due
-- time for the attempt, not the transition time into retrying. status_updated_at
-- records the last status transition so the grace window starts at the event
-- that can race with old-writer successor inserts.
-- ---------------------------------------------------------------------------

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ;

UPDATE webhook_deliveries
SET status_updated_at = COALESCE(scheduled_at, NOW())
WHERE status_updated_at IS NULL;

ALTER TABLE webhook_deliveries
    ALTER COLUMN status_updated_at SET DEFAULT clock_timestamp(),
    ALTER COLUMN status_updated_at SET NOT NULL;

COMMENT ON COLUMN webhook_deliveries.status_updated_at IS
    'Last webhook delivery status transition time; legacy recovery grace is anchored here.';

CREATE OR REPLACE FUNCTION webhook_deliveries_set_status_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        NEW.status_updated_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_webhook_deliveries_status_updated_at ON webhook_deliveries;
CREATE TRIGGER trg_webhook_deliveries_status_updated_at
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
    EXECUTE FUNCTION webhook_deliveries_set_status_updated_at();

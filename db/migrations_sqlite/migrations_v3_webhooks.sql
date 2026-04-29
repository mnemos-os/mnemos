-- SQLite mirror for v3 webhook subscriptions and deliveries.
-- LISTEN/NOTIFY is unavailable; workers poll webhook_deliveries.
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status_attempt
  ON webhook_deliveries(status, attempt_num, scheduled_at);

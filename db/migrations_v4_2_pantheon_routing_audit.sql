-- PANTHEON routing audit mirror for the v4.2 NATS substrate v0.2 slice.
-- The memories-backed pantheon_routing log remains the primary feedback
-- source; this table is a decoupled audit trail populated by the optional
-- NATS consumer.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS pantheon_routing_audit (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id     TEXT,
    tenant_user_id TEXT,
    alias_or_model TEXT,
    resolved_to    TEXT,
    outcome        TEXT,
    latency_ms     INT,
    tokens_in      INT,
    tokens_out     INT,
    cost_usd       NUMERIC(10,4),
    error_class    TEXT,
    payload        JSONB NOT NULL,
    created        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_created_desc
    ON pantheon_routing_audit (created DESC);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_tenant_created_desc
    ON pantheon_routing_audit (tenant_user_id, created DESC);

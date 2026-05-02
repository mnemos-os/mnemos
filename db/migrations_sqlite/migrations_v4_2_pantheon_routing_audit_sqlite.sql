-- SQLite mirror for db/migrations_v4_2_pantheon_routing_audit.sql.

CREATE TABLE IF NOT EXISTS pantheon_routing_audit (
    id             TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    request_id     TEXT,
    tenant_user_id TEXT,
    alias_or_model TEXT,
    resolved_to    TEXT,
    outcome        TEXT,
    latency_ms     INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       REAL,
    error_class    TEXT,
    payload        TEXT NOT NULL,
    created        TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_created_desc
    ON pantheon_routing_audit (created DESC);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_tenant_created_desc
    ON pantheon_routing_audit (tenant_user_id, created DESC);

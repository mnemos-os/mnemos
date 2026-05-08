-- MNEMOS v5.3.4 - MCP tool-call audit log (SQLite parallel).
--
-- Mirrors db/migrations_v5_3_4_mcp_audit_log.sql.
-- Redaction is done at write-time by _mcp_parameter_shape().

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id              TEXT PRIMARY KEY,
    caller_user_id  TEXT NOT NULL,
    role            TEXT NOT NULL,
    tool            TEXT NOT NULL,
    -- parameter_shape stored as JSON text; redacted at write time.
    parameter_shape TEXT NOT NULL DEFAULT '{}',
    outcome         TEXT NOT NULL CHECK (
        outcome IN ('called', 'success', 'failure', 'error', 'denied', 'root_bypass')
    ),
    error_class     TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_created_desc
    ON mcp_audit_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_caller_created_desc
    ON mcp_audit_log (caller_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_tool_created_desc
    ON mcp_audit_log (tool, created_at DESC);

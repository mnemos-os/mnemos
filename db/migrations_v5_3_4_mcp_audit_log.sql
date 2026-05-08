-- MNEMOS v5.3.4 - MCP tool-call audit log (Phase-D durable surface).
--
-- KNOWN_LIMITATIONS.md flagged the existing `mcp_tool_invocation`
-- logger output as Phase-D deferred — text-only, ephemeral, not
-- queryable across operator sessions. This migration adds the
-- durable, redaction-aware table the dispatcher writes to in
-- addition to the logger.
--
-- Redaction is already done at write-time by
-- mnemos.mcp.tools._security._mcp_parameter_shape() — the
-- `parameter_shape` JSONB carries only key names + value-type
-- shape, never raw values. So the table is safe to retain
-- indefinitely under normal data-protection rules.
--
-- Idempotent. Safe to re-run.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    caller_user_id  TEXT NOT NULL,
    role            TEXT NOT NULL,
    tool            TEXT NOT NULL,
    -- parameter_shape is the redacted shape: {key: {"type": "str"}, ...}
    -- never the raw values. See _mcp_parameter_shape().
    parameter_shape JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome         TEXT NOT NULL CHECK (
        outcome IN ('called', 'success', 'failure', 'error', 'denied', 'root_bypass')
    ),
    error_class     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Operator queries: "what has caller X done recently?", "who used
-- tool Y?", chronological scans for incident response.
CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_created_desc
    ON mcp_audit_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_caller_created_desc
    ON mcp_audit_log (caller_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_tool_created_desc
    ON mcp_audit_log (tool, created_at DESC);

-- Round-2 of #146: grant INSERT to the runtime app role.
-- The installer applies migrations as `postgres` superuser; without
-- explicit grants the table is owned by postgres and the runtime
-- pool (connecting as cfg.db_user, typically `mnemos_user`) hits
-- permission denied. The audit writer swallows the error at debug,
-- silently leaving the durable Phase-D table empty on installer-
-- managed upgrades.
--
-- Idempotent: the DO block silently no-ops when the role doesn't
-- exist yet (fresh-install order: setup_database creates the role,
-- then run_migrations applies this file).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mnemos_user') THEN
        EXECUTE 'GRANT SELECT, INSERT ON mcp_audit_log TO mnemos_user';
    END IF;
END $$;

COMMIT;

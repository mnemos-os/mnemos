-- v5.3.3: Composite index for the MPF v0.2 deletion_log export access pattern.
--
-- The deletion_log table was indexed for the existing /v1/deletion_log
-- API on (requested_at) and (memory_id), but the MPF v0.2 export path
-- filters by (owner_id, namespace) and orders by (executed_at, id) for
-- chunked replay. Without a matching composite index, the export query
-- on a tenant with a large deletion_log scans+sorts the whole audit
-- log before applying LIMIT — defeating the per-envelope cap's purpose.
--
-- This index is idempotent (CREATE INDEX IF NOT EXISTS) and partial
-- (only NOT NULL executed_at, which is the canonical case — DEFAULT
-- now()).

CREATE INDEX IF NOT EXISTS deletion_log_export_idx
    ON deletion_log (owner_id, namespace, executed_at, id)
    WHERE executed_at IS NOT NULL;

-- Root unscoped exports (when the operator runs --upgrade against a
-- non-tenant-scoped audit) hit a different path. Ensure the
-- executed_at/id leading-key access pattern is also covered.

CREATE INDEX IF NOT EXISTS deletion_log_executed_at_id_idx
    ON deletion_log (executed_at, id)
    WHERE executed_at IS NOT NULL;

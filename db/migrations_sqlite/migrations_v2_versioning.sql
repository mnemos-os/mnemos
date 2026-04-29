-- SQLite mirror for v2 memory_versions and audit tables.
-- The consolidated schema creates these tables up front to keep SQLite startup
-- idempotent on fresh single-file databases.
CREATE INDEX IF NOT EXISTS idx_mv_branch_head ON memory_versions(memory_id, branch, version_num DESC);

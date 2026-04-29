-- SQLite mirror for v1 multi-user columns, indexes, and visibility semantics.
-- RLS policies are not available in SQLite; the application visibility
-- predicate in mnemos.core.visibility is the enforcement boundary.
CREATE INDEX IF NOT EXISTS idx_memories_owner_namespace ON memories(owner_id, namespace);

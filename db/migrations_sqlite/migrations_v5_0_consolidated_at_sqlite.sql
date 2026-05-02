-- SQLite mirror for db/migrations_v5_0_consolidated_at.sql.

ALTER TABLE memories
    ADD COLUMN consolidated_at TEXT DEFAULT NULL;

UPDATE memories
SET consolidated_at = COALESCE(updated, CURRENT_TIMESTAMP)
WHERE consolidated_into IS NOT NULL
  AND consolidated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_consolidated_at
    ON memories(consolidated_at, id)
    WHERE consolidated_into IS NOT NULL
      AND consolidated_at IS NOT NULL;

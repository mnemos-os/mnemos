-- migrations_v5_0_consolidated_at.sql
--
-- Federation-aware MORPHEUS consolidation tombstones need a stable
-- timestamp for the redirect event cursor. Existing consolidated rows
-- are backfilled from updated so peers can see the most recent known
-- consolidation state after upgrade.

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ DEFAULT NULL;

UPDATE memories
SET consolidated_at = COALESCE(updated, NOW())
WHERE consolidated_into IS NOT NULL
  AND consolidated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_consolidated_at
    ON memories(consolidated_at, id)
    WHERE consolidated_into IS NOT NULL
      AND consolidated_at IS NOT NULL;

COMMENT ON COLUMN memories.consolidated_at IS
    'Timestamp when consolidated_into was set; used for federation consolidation tombstones.';

COMMIT;

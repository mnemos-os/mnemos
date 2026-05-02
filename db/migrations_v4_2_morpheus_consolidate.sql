-- migrations_v4_2_morpheus_consolidate.sql
--
-- MORPHEUS slice 3: CONSOLIDATE phase.
--
-- Adds a soft pointer from duplicate memories to their canonical
-- memory, plus counters on morpheus_runs. This never hard-deletes
-- originals; rollback restores the pointer and permission mode from
-- the metadata audit key written by the phase.
--
-- Note: memories.id is TEXT in the canonical schema, so the
-- consolidated_into self-reference is TEXT as well.

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS consolidated_into TEXT
    REFERENCES memories(id) DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_consolidated_into
    ON memories(consolidated_into)
    WHERE consolidated_into IS NOT NULL;

ALTER TABLE morpheus_runs
    ADD COLUMN IF NOT EXISTS memories_consolidated int NOT NULL DEFAULT 0;

ALTER TABLE morpheus_runs
    ADD COLUMN IF NOT EXISTS clusters_consolidated int NOT NULL DEFAULT 0;

COMMENT ON COLUMN memories.consolidated_into IS
    'Soft MORPHEUS consolidation pointer to the canonical memories.id; originals are never hard-deleted.';

COMMENT ON COLUMN morpheus_runs.memories_consolidated IS
    'Count of non-canonical memories soft-consolidated by this MORPHEUS run.';

COMMENT ON COLUMN morpheus_runs.clusters_consolidated IS
    'Count of clusters with at least one memory soft-consolidated by this MORPHEUS run.';

COMMIT;

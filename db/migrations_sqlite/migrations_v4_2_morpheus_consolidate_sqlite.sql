-- SQLite mirror for db/migrations_v4_2_morpheus_consolidate.sql.

ALTER TABLE memories
    ADD COLUMN consolidated_into TEXT DEFAULT NULL REFERENCES memories(id);

CREATE INDEX IF NOT EXISTS idx_memories_consolidated_into
    ON memories(consolidated_into)
    WHERE consolidated_into IS NOT NULL;

ALTER TABLE morpheus_runs
    ADD COLUMN memories_consolidated INTEGER NOT NULL DEFAULT 0;

ALTER TABLE morpheus_runs
    ADD COLUMN clusters_consolidated INTEGER NOT NULL DEFAULT 0;

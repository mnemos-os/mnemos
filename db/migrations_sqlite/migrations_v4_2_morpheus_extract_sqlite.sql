-- SQLite mirror for db/migrations_v4_2_morpheus_extract.sql.

ALTER TABLE memories
    ADD COLUMN triples_extracted_at TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_triples_extracted
    ON memories(triples_extracted_at)
    WHERE triples_extracted_at IS NULL;

ALTER TABLE kg_triples
    ADD COLUMN extracted_by_run_id TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_extract_run
    ON kg_triples(extracted_by_run_id)
    WHERE extracted_by_run_id IS NOT NULL;

ALTER TABLE morpheus_runs
    ADD COLUMN triples_extracted INTEGER NOT NULL DEFAULT 0;

ALTER TABLE morpheus_runs
    ADD COLUMN memories_processed_for_extraction INTEGER NOT NULL DEFAULT 0;

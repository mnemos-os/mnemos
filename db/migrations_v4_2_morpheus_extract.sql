-- migrations_v4_2_morpheus_extract.sql
--
-- MORPHEUS slice 4: EXTRACT phase.
--
-- Tracks prose memories already mined for latent KG triples and tags
-- triples created by a dream run so rollback can remove only that run's
-- extraction output.

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS triples_extracted_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_triples_extracted
    ON memories(triples_extracted_at)
    WHERE triples_extracted_at IS NULL;

ALTER TABLE kg_triples
    ADD COLUMN IF NOT EXISTS extracted_by_run_id UUID DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_extract_run
    ON kg_triples(extracted_by_run_id)
    WHERE extracted_by_run_id IS NOT NULL;

ALTER TABLE morpheus_runs
    ADD COLUMN IF NOT EXISTS triples_extracted int NOT NULL DEFAULT 0;

ALTER TABLE morpheus_runs
    ADD COLUMN IF NOT EXISTS memories_processed_for_extraction int NOT NULL DEFAULT 0;

COMMENT ON COLUMN memories.triples_extracted_at IS
    'Timestamp when MORPHEUS EXTRACT processed this memory for KG triples.';

COMMENT ON COLUMN kg_triples.extracted_by_run_id IS
    'MORPHEUS run id that created this extracted KG triple; used for scoped rollback.';

COMMENT ON COLUMN morpheus_runs.triples_extracted IS
    'Count of KG triples inserted by the MORPHEUS EXTRACT phase.';

COMMENT ON COLUMN morpheus_runs.memories_processed_for_extraction IS
    'Count of memories marked processed by the MORPHEUS EXTRACT phase.';

COMMIT;

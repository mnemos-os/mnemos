-- migrations_v5_0_morpheus_extract_run_memories.sql
--
-- Sidecar for MORPHEUS EXTRACT rollback. kg_triples only records
-- memories that produced triples; this table records every processed
-- candidate so rollback can reset triples_extracted_at even when a run
-- emitted zero triples for a memory.

BEGIN;

CREATE TABLE IF NOT EXISTS morpheus_extract_run_memories (
    run_id UUID NOT NULL REFERENCES morpheus_runs(id) ON DELETE CASCADE,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_morpheus_extract_run_memories_memory
    ON morpheus_extract_run_memories(memory_id, processed_at DESC);

COMMENT ON TABLE morpheus_extract_run_memories IS
    'MORPHEUS EXTRACT processed-memory sidecar used for rollback of zero-triple candidates.';

COMMIT;

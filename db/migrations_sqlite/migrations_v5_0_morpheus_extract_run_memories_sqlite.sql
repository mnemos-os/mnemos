-- SQLite mirror for db/migrations_v5_0_morpheus_extract_run_memories.sql.

CREATE TABLE IF NOT EXISTS morpheus_extract_run_memories (
    run_id TEXT NOT NULL REFERENCES morpheus_runs(id) ON DELETE CASCADE,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_morpheus_extract_run_memories_memory
    ON morpheus_extract_run_memories(memory_id, processed_at DESC);

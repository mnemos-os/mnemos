-- migrations_v4_2_persephone.sql
--
-- SQLite parity for PERSEPHONE read-path filtering. The archival
-- worker itself is Postgres-only, but the local backend needs the
-- marker column so shared memory SELECT lists remain backend-neutral.

CREATE TABLE IF NOT EXISTS memory_archive (
    id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE RESTRICT,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_by TEXT NOT NULL DEFAULT 'system:persephone',
    compressed_content BLOB NOT NULL,
    compression_algo TEXT NOT NULL DEFAULT 'zstd',
    original_size_bytes INTEGER NOT NULL,
    compressed_size_bytes INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memory_archive_archived_at
    ON memory_archive(archived_at DESC);

ALTER TABLE memories
    ADD COLUMN archived_at TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_archived_at
    ON memories(archived_at)
    WHERE archived_at IS NOT NULL;

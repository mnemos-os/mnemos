-- migrations_v4_2_persephone.sql
--
-- PERSEPHONE archival subsystem.
--
-- Cold memories keep their live memories row as a stub pointer while
-- the full memory payload is moved into compressed archival storage.
-- The memories.updated/content UPDATE performed by the archival runner
-- intentionally fires the existing version/federation triggers, so peers
-- see the archive marker as a normal state transition.

BEGIN;

CREATE TABLE IF NOT EXISTS memory_archive (
    id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE RESTRICT,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_by TEXT NOT NULL DEFAULT 'system:persephone',
    compressed_content BYTEA NOT NULL,
    compression_algo TEXT NOT NULL DEFAULT 'zstd',
    original_size_bytes INTEGER NOT NULL,
    compressed_size_bytes INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memory_archive_archived_at
    ON memory_archive(archived_at DESC);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_archived_at
    ON memories(archived_at)
    WHERE archived_at IS NOT NULL;

COMMENT ON TABLE memory_archive IS
    'PERSEPHONE archival storage for stubbed memories; memories rows remain as live archive pointers.';

COMMENT ON COLUMN memories.archived_at IS
    'PERSEPHONE archive marker. NULL means live; non-NULL rows have content=ARCHIVED:<id> and full payload in memory_archive.';

COMMIT;

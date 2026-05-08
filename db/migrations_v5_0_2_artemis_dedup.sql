-- migrations_v5_0_2_artemis_dedup.sql
--
-- ARTEMIS write-time duplicate detection needs a stable hash of
-- newline-normalized memory content. PostgreSQL stores it as a
-- generated column so direct SQL writers and API writers stay aligned.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS content_hash TEXT GENERATED ALWAYS AS (
        encode(
            digest(
                replace(replace(content, E'\r\n', E'\n'), E'\r', E'\n'),
                'sha256'
            ),
            'hex'
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_memories_owner_namespace_content_hash_active
    ON memories(owner_id, namespace, content_hash)
    WHERE deleted_at IS NULL
      AND archived_at IS NULL
      AND consolidated_into IS NULL
      AND content_hash IS NOT NULL;

COMMENT ON COLUMN memories.content_hash IS
    'SHA-256 of content after CRLF/CR newline normalization; used for ARTEMIS duplicate detection.';

COMMIT;

-- migrations_v3_5_session_compression_legacy_drop.sql
--
-- Remove the remaining legacy session-compression columns. Real compression
-- state lives in memory_compression_queue and memory_compressed_variants.

BEGIN;

ALTER TABLE session_messages
    DROP COLUMN IF EXISTS compressed;

ALTER TABLE session_memory_injections
    DROP COLUMN IF EXISTS compressed;

ALTER TABLE sessions
    DROP COLUMN IF EXISTS compression_tier;

COMMIT;

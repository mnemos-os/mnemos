-- migrations_v3_5_session_compression_ratio_drop.sql
--
-- Audit must-fix #7: remove session-layer compression_ratio fiction.
-- These columns were always NULL after placeholder ratios were removed;
-- real compression ratios live in the operator-batched compression tables.

BEGIN;

ALTER TABLE session_messages
    DROP COLUMN IF EXISTS compression_ratio;

ALTER TABLE session_memory_injections
    DROP COLUMN IF EXISTS compression_ratio;

COMMIT;

-- migrations_v4_2_compression_candidates_nullable_tokens.sql
--
-- Bring `memory_compression_candidates.original_tokens` into parity
-- with the SQLite shape (db/migrations_sqlite/migrations.sql:104
-- where the column is plain ``INTEGER`` — nullable). The PG schema
-- declared it ``INTEGER NOT NULL`` (db/migrations_v3_1_compression.sql:82)
-- and the persistence-parity test
-- ``test_compression_candidate_variant_and_export[postgres]`` inserts
-- without supplying it, surfacing on master CI as
-- ``NotNullViolationError: null value in column "original_tokens"``.
--
-- The compression candidate row is created early in the contest
-- pipeline; original_tokens may be unknown at insert time and filled
-- in by a later UPDATE. SQLite already permits this; PG must too for
-- parity to hold.
--
-- Idempotent: ALTER COLUMN ... DROP NOT NULL is a no-op on a column
-- that is already nullable.

BEGIN;

ALTER TABLE memory_compression_candidates
    ALTER COLUMN original_tokens DROP NOT NULL;

COMMIT;

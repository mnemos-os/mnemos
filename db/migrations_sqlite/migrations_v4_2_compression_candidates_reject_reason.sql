-- migrations_v4_2_compression_candidates_reject_reason.sql (SQLite)
--
-- v4.2.0a6 SQLite upgrade migration: add ``reject_reason`` to
-- ``memory_compression_candidates`` so the column shape matches PG
-- (mcc_loser_has_reason check constraint requires reject_reason on
-- losers). The fresh-install schema in
-- db/migrations_sqlite/migrations.sql already declares the column;
-- this migration brings existing/upgraded SQLite databases in line.
--
-- SQLite ALTER TABLE ADD COLUMN cannot be wrapped in IF NOT EXISTS,
-- and re-running it on a table that already has the column would
-- error. The application loader catches the "duplicate column"
-- error and treats it as success — see SqliteBackend.run_migrations.

ALTER TABLE memory_compression_candidates ADD COLUMN reject_reason TEXT;

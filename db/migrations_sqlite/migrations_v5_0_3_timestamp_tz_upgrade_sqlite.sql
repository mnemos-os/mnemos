-- MNEMOS v5.0.3 SQLite parity migration.
--
-- SQLite stores timestamp values as TEXT in MNEMOS. The Postgres
-- v5.0.3 migration upgrades legacy TIMESTAMP columns to TIMESTAMPTZ;
-- no physical conversion is required for SQLite, but the migration
-- chain includes this file to keep upgrade ordering in parity.
SELECT 1;

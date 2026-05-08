-- MNEMOS v5.2.2: GIN index on memories.content tsvector.
--
-- Without this index, /v1/memories/search FTS path executes a sequential
-- scan over every memory row, computing to_tsvector() per row, then a
-- top-N heap sort by ts_rank. On a 7.5K-row corpus that measured at
-- p50=1527ms / p95=1913ms in production. With the GIN index a Bitmap
-- Index Scan replaces the seq scan and the same workload drops to
-- p50≈250ms.
--
-- Idempotent. CREATE INDEX CONCURRENTLY would be the live-database
-- choice for long-running tables, but the loader runs migrations inside
-- a transaction so we use the plain form here. Operators who applied
-- this index live (PYTHIA, CERBERUS, PROTEUS on 2026-05-04) can safely
-- re-run because of `IF NOT EXISTS`.

CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING gin(to_tsvector('english', content));

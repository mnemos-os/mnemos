-- migrations_v5_3_5_model_registry_capabilities_gin.sql
--
-- Add a GIN index on `model_registry.capabilities` (TEXT[]) so the
-- `capabilities @> $3` containment queries used by the provider
-- discovery + recommend paths don't degrade to seq-scan as the
-- registry grows.
--
-- Issue surfaced by the v5.0.1 cross-code audit:
-- `mnemos/api/routes/providers.py:106` runs:
--     SELECT ... FROM model_registry
--      WHERE provider = $1 AND available = TRUE
--        AND capabilities @> $3
--      ORDER BY arena_score DESC NULLS LAST
--      LIMIT $2
-- The provider + available scalar columns are already indexed
-- (see migrations_model_registry.sql); the array containment
-- predicate is the missing leg.
--
-- GIN is the right index for `@>` over TEXT[]: btree wouldn't
-- match the operator, GIST would have higher write overhead than
-- needed for this read-heavy workload, and `array_ops` is the
-- default operator class for TEXT[] under GIN.
--
-- IF NOT EXISTS keeps this migration idempotent so a re-run on
-- an already-migrated cluster is a no-op.

CREATE INDEX IF NOT EXISTS idx_model_registry_capabilities_gin
    ON model_registry USING GIN (capabilities);

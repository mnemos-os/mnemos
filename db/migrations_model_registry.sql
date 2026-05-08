-- MNEMOS Model Registry Schema
-- Tracks all models available from each LLM provider, synced daily via provider APIs.
-- Arena.ai rankings are stored here so MNEMOS is the authoritative model registry.
--
-- Run after migrations_v2_versioning.sql

CREATE TABLE IF NOT EXISTS model_registry (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity
    provider              VARCHAR(50) NOT NULL,           -- graeae provider key: xai, openai, gemini, …
    model_id              TEXT        NOT NULL,           -- provider API model ID (exact string for API calls)
    display_name          TEXT,                           -- human-readable name / Arena model name
    family                TEXT,                           -- major version family: grok-4, gpt-5, gemini-3, …

    -- Capabilities
    context_window        INT,                            -- max input tokens
    max_output_tokens     INT,                            -- max completion tokens
    capabilities          TEXT[]      DEFAULT ARRAY[]::TEXT[],  -- ['chat', 'vision', 'code', 'reasoning', 'web_search']

    -- Pricing (USD per million tokens; 0 = unknown/free-tier)
    input_cost_per_mtok   NUMERIC(12, 6) DEFAULT 0,
    output_cost_per_mtok  NUMERIC(12, 6) DEFAULT 0,
    cache_read_per_mtok   NUMERIC(12, 6) DEFAULT 0,
    cache_write_per_mtok  NUMERIC(12, 6) DEFAULT 0,

    -- Status
    available             BOOLEAN     NOT NULL DEFAULT TRUE,
    deprecated            BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Arena.ai ranking (updated by update_model_registry.py / Elo sync)
    arena_score           NUMERIC(8, 2),                 -- raw Elo score from Arena
    arena_rank            INT,                            -- rank position in Arena leaderboard (1-based)
    graeae_weight         NUMERIC(5, 4),                 -- normalized weight used by GRAEAE engine (0.50–1.00)

    -- Lifecycle timestamps
    first_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_synced           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Full API response payload for debugging / future fields
    raw                   JSONB       DEFAULT '{}',

    CONSTRAINT uq_model_registry_provider_model UNIQUE (provider, model_id)
);

-- Indexes optimized for the expected query patterns
CREATE INDEX IF NOT EXISTS idx_model_registry_provider        ON model_registry(provider);
CREATE INDEX IF NOT EXISTS idx_model_registry_available       ON model_registry(available) WHERE available = TRUE;
CREATE INDEX IF NOT EXISTS idx_model_registry_arena_score     ON model_registry(arena_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_model_registry_graeae_weight   ON model_registry(graeae_weight DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_model_registry_family          ON model_registry(family);
CREATE INDEX IF NOT EXISTS idx_model_registry_last_synced     ON model_registry(last_synced DESC);

-- ── Sync log ─────────────────────────────────────────────────────────────────
-- Records each provider-sync run so operators can track freshness per provider.

CREATE TABLE IF NOT EXISTS model_registry_sync_log (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider     VARCHAR(50) NOT NULL,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    models_found INT         NOT NULL DEFAULT 0,
    models_added INT         NOT NULL DEFAULT 0,
    models_updated INT       NOT NULL DEFAULT 0,
    models_deprecated INT    NOT NULL DEFAULT 0,
    error        TEXT,                                   -- NULL = success
    duration_ms  INT
);

CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_provider   ON model_registry_sync_log(provider);
CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_synced_at  ON model_registry_sync_log(synced_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry TO mnemos_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_sync_log TO mnemos_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry TO mnemos;
GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_sync_log TO mnemos;

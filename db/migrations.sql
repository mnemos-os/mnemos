-- MNEMOS Database Schema with Compression & Quality Tracking
-- Phase 1: Core tables with integrated compression support

-- Enable required extensions FIRST
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- memories table: Core memory storage with compression support
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,

  -- Content (always store original)
  content TEXT NOT NULL,

  -- Metadata
  category VARCHAR(50) NOT NULL,
  task_type VARCHAR(100),
  created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated TIMESTAMPTZ DEFAULT NOW(),

  -- Compression fields
  compressed_content TEXT,
  compression_method VARCHAR(50),           -- 'lethe', 'aletheia', 'anamnesis', etc
  compression_ratio FLOAT,                   -- 0.0-1.0 (compressed/original)

  -- Quality tracking
  compression_manifest JSONB,                -- Full quality details
  quality_rating INT,                        -- 0-100% (how much preserved)
  quality_summary JSONB,                     -- What was removed/preserved
  original_reference TEXT REFERENCES memories(id),

  -- Audit
  compressed_at TIMESTAMPTZ,
  compressed_by VARCHAR(50),                 -- 'storage', 'rehydration', 'manual'
  compression_reason VARCHAR(255),           -- Why was it compressed

  -- Flags
  is_original BOOLEAN NOT NULL DEFAULT TRUE,
  is_compressed BOOLEAN GENERATED ALWAYS AS (compressed_content IS NOT NULL) STORED,

  -- Token counts for tracking
  original_token_count INT,
  compressed_token_count INT,

  -- Vector embedding
  embedding vector(768),

  -- Indexes
  CONSTRAINT valid_quality_rating CHECK (quality_rating IS NULL OR (quality_rating >= 0 AND quality_rating <= 100))
);

CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_task_type ON memories(task_type);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created DESC);
CREATE INDEX IF NOT EXISTS idx_memories_is_compressed ON memories(is_compressed);
CREATE INDEX IF NOT EXISTS idx_memories_original_reference ON memories(original_reference);
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING ivfflat(embedding vector_cosine_ops);

-- compression_quality_log: Audit trail of all compression operations
CREATE TABLE IF NOT EXISTS compression_quality_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,

  -- Compression metrics
  original_token_count INT NOT NULL,
  compressed_token_count INT NOT NULL,
  compression_ratio FLOAT NOT NULL,
  compression_method VARCHAR(50),

  -- Quality assessment
  quality_rating INT NOT NULL,
  quality_summary JSONB,
  compression_manifest JSONB,

  -- Audit
  created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed BOOLEAN DEFAULT FALSE,
  review_notes TEXT,
  reviewed_by VARCHAR(100),
  reviewed_at TIMESTAMPTZ,

  CONSTRAINT valid_compression_ratio CHECK (compression_ratio > 0 AND compression_ratio <= 1.0),
  CONSTRAINT valid_quality CHECK (quality_rating >= 0 AND quality_rating <= 100)
);

CREATE INDEX IF NOT EXISTS idx_compression_log_memory_id ON compression_quality_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_compression_log_created ON compression_quality_log(created DESC);
CREATE INDEX IF NOT EXISTS idx_compression_log_reviewed ON compression_quality_log(reviewed);
CREATE INDEX IF NOT EXISTS idx_compression_log_quality_rating ON compression_quality_log(quality_rating);

-- graeae_consultations: Store Graeae consensus results with both versions
CREATE TABLE IF NOT EXISTS graeae_consultations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Request
  prompt TEXT NOT NULL,
  task_type VARCHAR(100),

  -- Context (both versions)
  context_uncompressed TEXT,                 -- Full qualitative reference
  context_compressed TEXT,                   -- For actual consultation

  -- Quality tracking
  context_quality_rating INT,                -- 0-100%
  context_quality_summary JSONB,
  compression_manifest JSONB,

  -- Consultation memory IDs (for tracking what was used)
  context_memory_ids TEXT[],

  -- Response
  consensus_response TEXT NOT NULL,
  consensus_score FLOAT,                     -- 0.0-1.0 agreement level
  winning_muse VARCHAR(100),

  -- Metadata
  cost FLOAT,                                -- USD spent
  latency_ms INT,
  mode VARCHAR(50),                          -- 'local', 'external', 'auto'

  -- Audit
  created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  model_variants JSONB,                      -- Which model variants were used

  CONSTRAINT valid_quality CHECK (context_quality_rating IS NULL OR (context_quality_rating >= 0 AND context_quality_rating <= 100)),
  CONSTRAINT valid_consensus_score CHECK (consensus_score IS NULL OR (consensus_score >= 0 AND consensus_score <= 1.0))
);

CREATE INDEX IF NOT EXISTS idx_graeae_consult_task_type ON graeae_consultations(task_type);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_created ON graeae_consultations(created DESC);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_mode ON graeae_consultations(mode);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_winning_muse ON graeae_consultations(winning_muse);

-- state table: Store current state (identity, today, workspace)
CREATE TABLE IF NOT EXISTS state (
  key VARCHAR(100) PRIMARY KEY,
  value JSONB NOT NULL,
  updated TIMESTAMPTZ DEFAULT NOW(),

  -- Audit
  updated_by VARCHAR(100),
  version INT DEFAULT 1
);

-- journal table: JSONL-style journal entries, date-partitioned
CREATE TABLE IF NOT EXISTS journal (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_date DATE NOT NULL,
  topic VARCHAR(100),
  content TEXT,
  metadata JSONB,
  created TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT fk_journal_date CHECK (entry_date = DATE(created))
);

CREATE INDEX IF NOT EXISTS idx_journal_entry_date ON journal(entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_topic ON journal(topic);
CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created DESC);

-- entities table: Track people, projects, etc.
CREATE TABLE IF NOT EXISTS entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type VARCHAR(50) NOT NULL,         -- 'person', 'project', 'concept'
  name VARCHAR(255) NOT NULL,
  description TEXT,
  metadata JSONB,

  -- Relationships
  related_entities UUID[],

  created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated TIMESTAMPTZ DEFAULT NOW(),

  UNIQUE(entity_type, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS kg_triples (
    id           TEXT PRIMARY KEY,
    subject      TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object       TEXT NOT NULL,
    subject_type TEXT,
    object_type  TEXT,
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until  TIMESTAMPTZ,
    memory_id    TEXT,
    confidence   FLOAT NOT NULL DEFAULT 1.0,
    created      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_kg_subject    ON kg_triples(subject);
CREATE INDEX IF NOT EXISTS idx_kg_predicate  ON kg_triples(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_memory_id  ON kg_triples(memory_id);


-- Extension: Enable vector support if not already enabled
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- View: Compression statistics by task type
CREATE OR REPLACE VIEW v_compression_stats AS
SELECT
  m.task_type,
  COUNT(*) as total_compressions,
  AVG(cql.quality_rating) as avg_quality_rating,
  MIN(cql.quality_rating) as min_quality_rating,
  MAX(cql.quality_rating) as max_quality_rating,
  AVG(cql.compression_ratio) as avg_compression_ratio,
  COUNT(CASE WHEN cql.reviewed THEN 1 END) as reviewed_count,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY cql.quality_rating) as median_quality
FROM compression_quality_log cql
LEFT JOIN memories m ON cql.memory_id = m.id
GROUP BY m.task_type;

-- View: Unreviewed compressions (require attention)
CREATE OR REPLACE VIEW v_unreviewed_compressions AS
SELECT
  cql.id,
  cql.memory_id,
  m.task_type,
  cql.quality_rating,
  cql.compression_ratio,
  cql.created,
  m.content,
  cql.quality_summary
FROM compression_quality_log cql
JOIN memories m ON cql.memory_id = m.id
WHERE cql.reviewed = FALSE
  AND cql.quality_rating < 80
ORDER BY cql.quality_rating ASC, cql.created DESC;

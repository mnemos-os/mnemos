-- SQLite profile schema mirror for db/migrations.sql through v3.5.
--
-- SQLite stores UUID, JSONB, TIMESTAMPTZ, arrays, and vector columns as TEXT.
-- JSON values are plain JSON text consumed through JSON1-aware application SQL.
-- FTS uses FTS5. Vector search uses sqlite-vec when the vec0 extension is
-- available and falls back to the memory_embeddings JSON table otherwise.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  task_type TEXT,
  context TEXT,
  importance INTEGER DEFAULT 5,
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed TEXT,
  embedding TEXT,
  is_compressed INTEGER NOT NULL DEFAULT 0,
  original_reference TEXT,
  compression_ratio REAL,
  quality_rating INTEGER NOT NULL DEFAULT 0,
  subcategory TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  verbatim_content TEXT,
  owner_id TEXT NOT NULL DEFAULT 'default',
  group_id TEXT,
  namespace TEXT NOT NULL DEFAULT 'default',
  permission_mode INTEGER NOT NULL DEFAULT 600,
  source_model TEXT,
  source_provider TEXT,
  source_session TEXT,
  source_agent TEXT,
  llm_optimized INTEGER NOT NULL DEFAULT 0,
  optimized_at TEXT,
  federation_source TEXT,
  federation_synced_at TEXT,
  morpheus_run_id TEXT,
  morpheus_cluster_id TEXT,
  source_memory_ids TEXT NOT NULL DEFAULT '[]',
  provenance TEXT NOT NULL DEFAULT '{}',
  last_recalled_at TEXT,
  recall_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_task_type ON memories(task_type);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created DESC);
CREATE INDEX IF NOT EXISTS idx_memories_updated_id ON memories(updated ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_memories_is_compressed ON memories(is_compressed);
CREATE INDEX IF NOT EXISTS idx_memories_original_reference ON memories(original_reference);
CREATE INDEX IF NOT EXISTS idx_memories_owner_id ON memories(owner_id);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);
CREATE INDEX IF NOT EXISTS idx_memories_group_id ON memories(group_id) WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_owner_cat ON memories(owner_id, category);
CREATE INDEX IF NOT EXISTS idx_memories_federation ON memories(federation_source) WHERE federation_source IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_morpheus_run ON memories(morpheus_run_id);
CREATE INDEX IF NOT EXISTS idx_memories_provenance ON memories(source_provider, source_model, source_agent);
CREATE INDEX IF NOT EXISTS idx_memories_last_recalled_at ON memories(last_recalled_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_recall_count ON memories(recall_count DESC);

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  embedding TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  id UNINDEXED,
  content,
  category,
  tokenize = 'porter'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_ai
AFTER INSERT ON memories
BEGIN
  INSERT INTO memories_fts(id, content, category)
  VALUES (new.id, new.content, new.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad
AFTER DELETE ON memories
BEGIN
  DELETE FROM memories_fts WHERE id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_au
AFTER UPDATE OF content, category ON memories
BEGIN
  DELETE FROM memories_fts WHERE id = old.id;
  INSERT INTO memories_fts(id, content, category)
  VALUES (new.id, new.content, new.category);
END;

CREATE TABLE IF NOT EXISTS compression_quality_log (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  memory_id TEXT,
  original_content TEXT,
  compressed_content TEXT,
  original_tokens INTEGER,
  compressed_tokens INTEGER,
  compression_ratio REAL,
  quality_rating INTEGER,
  llm_feedback TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  owner_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_compression_log_memory_id ON compression_quality_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_compression_log_created ON compression_quality_log(created DESC);
CREATE INDEX IF NOT EXISTS idx_compression_log_reviewed ON compression_quality_log(reviewed);
CREATE INDEX IF NOT EXISTS idx_compression_log_quality_rating ON compression_quality_log(quality_rating);

CREATE TABLE IF NOT EXISTS graeae_consultations (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  prompt TEXT NOT NULL,
  task_type TEXT NOT NULL,
  consensus_response TEXT,
  consensus_score REAL,
  winning_muse TEXT,
  cost REAL DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  mode TEXT DEFAULT 'single',
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_graeae_consult_task_type ON graeae_consultations(task_type);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_created ON graeae_consultations(created DESC);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_mode ON graeae_consultations(mode);
CREATE INDEX IF NOT EXISTS idx_graeae_consult_winning_muse ON graeae_consultations(winning_muse);
CREATE INDEX IF NOT EXISTS idx_graeae_consultations_owner ON graeae_consultations(owner_id);
CREATE INDEX IF NOT EXISTS idx_graeae_consultations_owner_namespace
  ON graeae_consultations(owner_id, namespace);

CREATE TABLE IF NOT EXISTS state (
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  key TEXT NOT NULL,
  value TEXT,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (owner_id, namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_state_owner ON state(owner_id);
CREATE INDEX IF NOT EXISTS idx_state_owner_namespace ON state(owner_id, namespace);

CREATE TABLE IF NOT EXISTS journal (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  entry_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  topic TEXT,
  content TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_journal_entry_date ON journal(entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_topic ON journal(topic);
CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created DESC);
CREATE INDEX IF NOT EXISTS idx_journal_owner ON journal(owner_id);
CREATE INDEX IF NOT EXISTS idx_journal_owner_date ON journal(owner_id, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_owner_namespace ON journal(owner_id, namespace);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  entity_type TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(owner_id, namespace, entity_type, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_owner ON entities(owner_id);
CREATE INDEX IF NOT EXISTS idx_entities_namespace ON entities(namespace);
CREATE INDEX IF NOT EXISTS idx_entities_owner_namespace ON entities(owner_id, namespace);

CREATE TABLE IF NOT EXISTS kg_triples (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object TEXT NOT NULL,
  subject_type TEXT,
  object_type TEXT,
  valid_from TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  valid_until TEXT,
  memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_kg_subject ON kg_triples(subject);
CREATE INDEX IF NOT EXISTS idx_kg_predicate ON kg_triples(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_memory_id ON kg_triples(memory_id);
CREATE INDEX IF NOT EXISTS idx_kg_owner_id ON kg_triples(owner_id);
CREATE INDEX IF NOT EXISTS idx_kg_namespace ON kg_triples(namespace);
CREATE INDEX IF NOT EXISTS idx_kg_owner_subject ON kg_triples(owner_id, subject);
CREATE INDEX IF NOT EXISTS idx_kg_owner_predicate ON kg_triples(owner_id, predicate);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  namespace TEXT NOT NULL DEFAULT 'default',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_namespace ON users(namespace);

CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_groups (
  user_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  user_id TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  label TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(key_hash) WHERE revoked = 0;

CREATE TABLE IF NOT EXISTS memory_versions (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  version_num INTEGER NOT NULL,
  content TEXT NOT NULL,
  category TEXT,
  subcategory TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  verbatim_content TEXT,
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  permission_mode INTEGER NOT NULL DEFAULT 600,
  source_model TEXT,
  source_provider TEXT,
  source_session TEXT,
  source_agent TEXT,
  snapshot_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  snapshot_by TEXT,
  change_type TEXT NOT NULL DEFAULT 'create',
  commit_hash TEXT,
  parent_version_id TEXT REFERENCES memory_versions(id) ON DELETE SET NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  merge_parents TEXT NOT NULL DEFAULT '[]',
  UNIQUE(memory_id, branch, version_num)
);

CREATE INDEX IF NOT EXISTS idx_mv_memory_id ON memory_versions(memory_id);
CREATE INDEX IF NOT EXISTS idx_mv_memory_id_vnum ON memory_versions(memory_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_mv_snapshot_at ON memory_versions(snapshot_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_commit_hash ON memory_versions(commit_hash)
  WHERE commit_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_main_linear ON memory_versions(memory_id, version_num)
  WHERE branch = 'main';

CREATE TABLE IF NOT EXISTS memory_branches (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  head_version_id TEXT REFERENCES memory_versions(id) ON DELETE SET NULL,
  created_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (memory_id, name)
);

CREATE INDEX IF NOT EXISTS idx_memory_branches_memory ON memory_branches(memory_id);

CREATE TABLE IF NOT EXISTS graeae_audit_log (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  sequence_num INTEGER,
  consultation_id TEXT,
  prompt TEXT,
  prompt_hash TEXT,
  provider TEXT,
  model TEXT,
  response_text TEXT,
  response_hash TEXT,
  chain_hash TEXT,
  prev_id TEXT,
  prev_chain_hash TEXT,
  task_type TEXT,
  quality_score REAL,
  latency_ms INTEGER,
  cost_usd REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_sequence ON graeae_audit_log(sequence_num);
CREATE INDEX IF NOT EXISTS idx_audit_created ON graeae_audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_graeae_audit_log_consultation ON graeae_audit_log(consultation_id);
CREATE INDEX IF NOT EXISTS idx_graeae_audit_log_created_at ON graeae_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_graeae_audit_log_chain_hash ON graeae_audit_log(chain_hash);

CREATE TABLE IF NOT EXISTS consultation_memory_refs (
  consultation_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  relevance_score REAL,
  injected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (consultation_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_consultation_memory_refs_consultation
  ON consultation_memory_refs(consultation_id);
CREATE INDEX IF NOT EXISTS idx_consultation_memory_refs_memory
  ON consultation_memory_refs(memory_id);
CREATE INDEX IF NOT EXISTS idx_consultation_memory_refs_injected_at
  ON consultation_memory_refs(injected_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  namespace TEXT NOT NULL DEFAULT 'default',
  title TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_owner_namespace ON sessions(user_id, namespace);

CREATE TABLE IF NOT EXISTS session_messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session_ts
  ON session_messages(session_id, timestamp ASC);
CREATE INDEX IF NOT EXISTS idx_session_messages_role ON session_messages(session_id, role);

CREATE TABLE IF NOT EXISTS session_memory_injections (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relevance_score REAL,
  injected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_memory_injections_session
  ON session_memory_injections(session_id);

CREATE TABLE IF NOT EXISTS model_registry (
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT,
  family TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  input_cost_per_mtok REAL NOT NULL DEFAULT 0,
  output_cost_per_mtok REAL NOT NULL DEFAULT 0,
  context_window INTEGER,
  arena_score REAL,
  graeae_weight REAL NOT NULL DEFAULT 0,
  available INTEGER NOT NULL DEFAULT 1,
  deprecated INTEGER NOT NULL DEFAULT 0,
  last_synced TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (provider, model_id)
);

CREATE INDEX IF NOT EXISTS idx_model_registry_provider ON model_registry(provider);
CREATE INDEX IF NOT EXISTS idx_model_registry_available ON model_registry(available) WHERE available = 1;
CREATE INDEX IF NOT EXISTS idx_model_registry_arena_score ON model_registry(arena_score DESC);
CREATE INDEX IF NOT EXISTS idx_model_registry_graeae_weight ON model_registry(graeae_weight DESC);
CREATE INDEX IF NOT EXISTS idx_model_registry_family ON model_registry(family);
CREATE INDEX IF NOT EXISTS idx_model_registry_last_synced ON model_registry(last_synced DESC);

CREATE TABLE IF NOT EXISTS model_registry_sync_log (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  provider TEXT NOT NULL,
  models_seen INTEGER NOT NULL DEFAULT 0,
  models_upserted INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_provider
  ON model_registry_sync_log(provider);
CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_synced_at
  ON model_registry_sync_log(synced_at DESC);

CREATE TABLE IF NOT EXISTS memory_compression_queue (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  scheduled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcq_ready ON memory_compression_queue(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_mcq_memory ON memory_compression_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcq_owner ON memory_compression_queue(owner_id);

CREATE TABLE IF NOT EXISTS memory_compression_candidates (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL DEFAULT 'default',
  contest_id TEXT,
  engine_id TEXT NOT NULL,
  engine_version TEXT,
  candidate_content TEXT,
  candidate_tokens INTEGER,
  compression_ratio REAL,
  quality_score REAL,
  composite_score REAL,
  is_winner INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcc_memory ON memory_compression_candidates(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcc_contest ON memory_compression_candidates(contest_id);
CREATE INDEX IF NOT EXISTS idx_mcc_memory_winner ON memory_compression_candidates(memory_id, is_winner);
CREATE INDEX IF NOT EXISTS idx_mcc_owner ON memory_compression_candidates(owner_id);
CREATE INDEX IF NOT EXISTS idx_mcc_engine ON memory_compression_candidates(engine_id);

CREATE TABLE IF NOT EXISTS memory_compressed_variants (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL DEFAULT 'default',
  winner_candidate_id TEXT REFERENCES memory_compression_candidates(id) ON DELETE SET NULL,
  engine_id TEXT NOT NULL,
  engine_version TEXT,
  compressed_content TEXT,
  compressed_tokens INTEGER,
  compression_ratio REAL,
  quality_score REAL,
  composite_score REAL,
  scoring_profile TEXT NOT NULL DEFAULT 'balanced',
  judge_model TEXT,
  selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcv_owner ON memory_compressed_variants(owner_id);
CREATE INDEX IF NOT EXISTS idx_mcv_engine ON memory_compressed_variants(engine_id);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  url TEXT NOT NULL,
  events TEXT NOT NULL DEFAULT '[]',
  secret TEXT,
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  revoked INTEGER NOT NULL DEFAULT 0,
  revoked_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_owner
  ON webhook_subscriptions(owner_id, namespace);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  subscription_id TEXT NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  attempt_num INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  response_status INTEGER,
  response_body TEXT,
  error TEXT,
  scheduled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_token TEXT,
  lease_expires_at TEXT,
  writer_revision INTEGER NOT NULL DEFAULT 1,
  status_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  superseded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription
  ON webhook_deliveries(subscription_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending
  ON webhook_deliveries(scheduled_at)
  WHERE status IN ('pending', 'retrying') AND superseded = 0;
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at
  ON webhook_deliveries(lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt
  ON webhook_deliveries(subscription_id, event_type, payload_hash, attempt_num)
  WHERE status IN ('pending', 'retrying') AND superseded = 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain
  ON webhook_deliveries(subscription_id, event_type, payload_hash)
  WHERE status = 'succeeded';

CREATE TABLE IF NOT EXISTS oauth_providers (
  id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  client_id TEXT NOT NULL,
  client_secret TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_identities (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  provider_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  email TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oauth_identities_user ON oauth_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_identities_email ON oauth_identities(email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS oauth_sessions (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  user_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  access_token TEXT,
  refresh_token TEXT,
  expires_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oauth_sessions_user ON oauth_sessions(user_id) WHERE revoked = 0;
CREATE INDEX IF NOT EXISTS idx_oauth_sessions_expires ON oauth_sessions(expires_at) WHERE revoked = 0;

CREATE TABLE IF NOT EXISTS federation_peers (
  id TEXT PRIMARY KEY,
  name TEXT,
  base_url TEXT NOT NULL UNIQUE,
  api_key TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_sync_at TEXT,
  cursor_updated TEXT,
  cursor_id TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_federation_peers_enabled
  ON federation_peers(enabled) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS federation_sync_log (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  peer_id TEXT NOT NULL REFERENCES federation_peers(id) ON DELETE CASCADE,
  direction TEXT NOT NULL,
  status TEXT NOT NULL,
  records_seen INTEGER NOT NULL DEFAULT 0,
  records_written INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_federation_sync_log_peer
  ON federation_sync_log(peer_id, synced_at DESC);

CREATE TABLE IF NOT EXISTS morpheus_runs (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  owner_id TEXT NOT NULL DEFAULT 'default',
  namespace TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'pending',
  config TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_morpheus_runs_status ON morpheus_runs(status);
CREATE INDEX IF NOT EXISTS idx_morpheus_runs_started ON morpheus_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_morpheus_runs_namespace ON morpheus_runs(namespace);

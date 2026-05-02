-- migrations_v4_2_deletion_requests_soft_delete_columns.sql
--
-- Phase B of the GDPR right-to-be-forgotten path. Adds
-- soft-delete columns to every table the confirmed deletion-request
-- worker touches, plus partial indexes for the live-row read path
-- (``deleted_at IS NULL``).
--
-- No BEGIN/COMMIT
--
-- This file deliberately does NOT wrap its statements in an
-- explicit transaction block. The migration runner applies files
-- through ``psql -f``; without an explicit BEGIN, psql runs each
-- statement in autocommit mode. That shape is required for
-- ``CREATE INDEX CONCURRENTLY``, which Postgres refuses inside a
-- transaction block. The column additions are metadata-only nullable
-- columns; the indexes are partial live-row indexes built online.
--
-- Idempotent. Safe to re-run.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE memory_versions
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE memory_branches
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE kg_triples
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE session_messages
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE session_memory_injections
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE journal
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE state
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE graeae_consultations
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE graeae_audit_log
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_live_owner_namespace
    ON memories (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_versions_live_owner_namespace
    ON memory_versions (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_branches_live_memory
    ON memory_branches (memory_id)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kg_triples_live_owner_namespace
    ON kg_triples (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sessions_live_user_namespace
    ON sessions (user_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_session_messages_live_session
    ON session_messages (session_id)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_session_memory_injections_live_session
    ON session_memory_injections (session_id)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_journal_live_owner_namespace
    ON journal (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_entities_live_owner_namespace
    ON entities (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_state_live_owner_namespace
    ON state (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_graeae_consultations_live_owner_namespace
    ON graeae_consultations (owner_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_graeae_audit_log_live_consultation
    ON graeae_audit_log (consultation_id)
    WHERE deleted_at IS NULL;

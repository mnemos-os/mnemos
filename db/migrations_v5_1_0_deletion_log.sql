-- MNEMOS v5.1.0 - GDPR deletion-log audit trail.
--
-- Records the destructive edge of wipe/purge flows without retaining
-- deleted content. Each row keeps a memory id, sha256(content) hex
-- digest, tenant scope, caller attribution, timing, request kind, and
-- optional source breadcrumbs.
--
-- Idempotent. Safe to re-run.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS deletion_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    owner_id     TEXT,
    namespace    TEXT,
    requested_by TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    executed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_kind TEXT NOT NULL CHECK (
        request_kind IN ('gdpr_wipe', 'admin_purge', 'tombstone_collected')
    ),
    reason       TEXT,
    source       TEXT[]
);

CREATE INDEX IF NOT EXISTS deletion_log_requested_at_idx
    ON deletion_log (requested_at DESC);

CREATE INDEX IF NOT EXISTS deletion_log_memory_id_idx
    ON deletion_log (memory_id);

CREATE INDEX IF NOT EXISTS deletion_log_owner_namespace_idx
    ON deletion_log (owner_id, namespace);

COMMIT;

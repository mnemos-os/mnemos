-- SQLite mirror for db/migrations_v5_1_0_deletion_log.sql.
-- Differences from Postgres:
--   * gen_random_uuid()  -> id is TEXT NOT NULL with API-side UUID;
--                           IF NULL SQLite trigger fills lower(hex(randomblob(16)))
--   * TIMESTAMPTZ        -> TEXT (ISO-8601 UTC)
--   * TEXT[]             -> TEXT (JSON-encoded array; API serializes)
--   * CHECK enum kept verbatim (sqlite supports CHECK)
--   * DEFAULT now()      -> DEFAULT (datetime('now'))

CREATE TABLE IF NOT EXISTS deletion_log (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    owner_id     TEXT,
    namespace    TEXT,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    executed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    request_kind TEXT NOT NULL CHECK (
        request_kind IN ('gdpr_wipe', 'admin_purge', 'tombstone_collected')
    ),
    reason       TEXT,
    source       TEXT
);

CREATE INDEX IF NOT EXISTS deletion_log_requested_at_idx
    ON deletion_log (requested_at DESC);

CREATE INDEX IF NOT EXISTS deletion_log_memory_id_idx
    ON deletion_log (memory_id);

CREATE INDEX IF NOT EXISTS deletion_log_owner_namespace_idx
    ON deletion_log (owner_id, namespace);

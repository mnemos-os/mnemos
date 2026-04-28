-- MNEMOS v3.2 — per-entity namespace column
--
-- Entities gained `owner_id` in migrations_v3_ownership.sql but not
-- `namespace`. Now that per-user namespaces are live
-- (migrations_v3_2_user_namespace.sql), entities need the same
-- two-dimensional tenancy gate as memories/kg/dag/webhooks.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + DEFAULT populates existing
-- rows with 'default' in place — no separate backfill step needed
-- because entities have no linked memory to inherit from.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

-- Indexes matching the owner_id patterns above it.
CREATE INDEX IF NOT EXISTS idx_entities_namespace
    ON entities(namespace);
CREATE INDEX IF NOT EXISTS idx_entities_owner_namespace
    ON entities(owner_id, namespace);

-- Note: this migration originally deferred widening the
-- (owner_id, entity_type, name) UNIQUE constraint. That deferral is closed in
-- migrations_v3_5_entities_namespace_unique.sql, which replaces it with
-- (owner_id, namespace, entity_type, name) after an exact-duplicate guard.

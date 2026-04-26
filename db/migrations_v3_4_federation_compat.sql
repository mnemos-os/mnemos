-- migrations_v3_4_federation_compat.sql
--
-- v3.4 federation schema-compatibility check. After standing up
-- cross-version federation (PROTEUS at v3.4 pulling from PYTHIA at
-- v3.3 succeeded by accident on 2026-04-26), we want explicit
-- safety: federation should fail-loud when peers have
-- significantly-different schemas, not silently drop or mangle
-- data on either side.
--
-- Adds three columns to federation_peers:
--
--   compat_mode TEXT — 'strict' (default) | 'permissive'
--     - strict: refuse to sync if peer's mnemos_version major.minor
--       differs from local. Operator must explicitly opt into
--       cross-version sync.
--     - permissive: log mismatch + continue. For staging-from-prod
--       and other deliberate cross-version flows.
--
--   peer_mnemos_version TEXT — peer's reported version at last sync
--   last_schema_check_at TIMESTAMPTZ — when we last asked the peer
--
-- Future v3.5/v4.0 work: replace with a "core + extensions" schema
-- contract per docs/V3_5_CHARTER.md. For now this is the minimum
-- safe operational surface.
--
-- Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS guards.

BEGIN;

ALTER TABLE federation_peers
    ADD COLUMN IF NOT EXISTS compat_mode TEXT NOT NULL DEFAULT 'strict';

ALTER TABLE federation_peers
    ADD COLUMN IF NOT EXISTS peer_mnemos_version TEXT;

ALTER TABLE federation_peers
    ADD COLUMN IF NOT EXISTS last_schema_check_at TIMESTAMPTZ;

-- Constrain compat_mode to the allowed values. DROP first so
-- re-running the migration doesn't accumulate constraints.
ALTER TABLE federation_peers
    DROP CONSTRAINT IF EXISTS federation_peers_compat_mode_check;
ALTER TABLE federation_peers
    ADD CONSTRAINT federation_peers_compat_mode_check
    CHECK (compat_mode IN ('strict', 'permissive'));

COMMIT;

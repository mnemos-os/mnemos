-- migrations_v4_2_deletion_requests.sql
--
-- GDPR right-to-be-forgotten infrastructure: an audit-bearing
-- request table that records who asked to wipe what, when the
-- request was confirmed, and when the soft / hard delete
-- actually executed.
--
-- This migration ships the SCHEMA only — the admin endpoint
-- (POST /v1/admin/deletion-requests) is gated by round-77 of
-- v4.2.0a14; the actual wipe worker that consumes pending
-- requests is gated by round-78+. The 30-day grace-period
-- semantics (soft → hard delete) are operator-facing and
-- documented in DEPLOYMENT.md.
--
-- ── Why a request table (not just a wipe endpoint) ─────────
--
-- GDPR right-to-be-forgotten under most jurisdictions requires
-- proof that the deletion happened. The request table is the
-- audit-bearing breadcrumb: the row survives the wipe (rows
-- are not self-deleted) so an operator can show "this
-- deletion request was honored on this date" without needing
-- to retain the deleted personal data itself.
--
-- The table also drives the soft → hard delete lifecycle:
--
--   * status='requested': admin endpoint just wrote the row.
--   * status='confirmed': second-step confirmation accepted.
--   * status='soft_deleted': worker has soft-deleted target's
--     rows across memories, kg_triples, sessions, journal,
--     entities, state, graeae_consultations, etc. ``restore_by``
--     timestamp is set to now()+30 days at this transition.
--   * status='restored': operator-triggered restore before
--     ``restore_by``; soft-deletes are reverted.
--   * status='hard_deleted': worker has applied permanent
--     deletion after the ``restore_by`` window passed.
--
-- ``target_user_id`` is required; ``target_namespace`` is
-- optional — when NULL the wipe covers ALL of the user's
-- namespaces. Operators typically wipe at the user level
-- (one request per data subject); per-namespace wipes are a
-- narrower operational tool (e.g., "remove this user's data
-- from this specific tenant but leave their other namespaces
-- intact").
--
-- Idempotent. Safe to re-run.

BEGIN;

-- gen_random_uuid() requires pgcrypto. Existing migrations
-- already enable it (verified via migrations.sql); this
-- statement is defensive in case operators ran the schema
-- partially.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS deletion_requests (
    id                UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    target_user_id    TEXT NOT NULL,
    target_namespace  TEXT,           -- NULL = wipe all namespaces for the user
    requested_by      TEXT NOT NULL,  -- root user who initiated
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at      TIMESTAMPTZ,
    soft_deleted_at   TIMESTAMPTZ,
    restore_by        TIMESTAMPTZ,    -- soft_deleted_at + grace; hard-delete after this
    restored_at       TIMESTAMPTZ,
    hard_deleted_at   TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'requested',
    notes             TEXT,
    CONSTRAINT deletion_requests_status_valid
        CHECK (status IN (
            'requested', 'confirmed', 'soft_deleted',
            'restored', 'hard_deleted', 'cancelled'
        )),
    -- A given target_user_id + target_namespace can only have
    -- ONE non-terminal request at a time. Terminal states
    -- (restored, hard_deleted, cancelled) are excluded from
    -- the partial unique index so historical requests don't
    -- block new ones.
    CONSTRAINT deletion_requests_status_lifecycle CHECK (
        (status = 'requested' AND soft_deleted_at IS NULL AND hard_deleted_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND soft_deleted_at IS NULL)
        OR (status = 'soft_deleted' AND soft_deleted_at IS NOT NULL AND restore_by IS NOT NULL AND hard_deleted_at IS NULL)
        OR (status = 'restored' AND restored_at IS NOT NULL)
        OR (status = 'hard_deleted' AND hard_deleted_at IS NOT NULL)
        OR (status = 'cancelled')
    )
);

-- Lookup index: admin "list pending requests" view scans by
-- status; the wipe worker scans by status='confirmed' or
-- status='soft_deleted' (with restore_by passed).
CREATE INDEX IF NOT EXISTS deletion_requests_status_idx
    ON deletion_requests (status);

-- Lookup index: "show me all requests for this user" — used
-- by the audit / compliance reporting path.
CREATE INDEX IF NOT EXISTS deletion_requests_target_user_idx
    ON deletion_requests (target_user_id);

-- Partial unique index: only one non-terminal request per
-- (user, namespace) pair at a time. NULL namespace is treated
-- as the all-namespaces scope and uniqueness applies. Postgres'
-- default NULLS DISTINCT semantics in UNIQUE indexes allow
-- multiple NULL-namespace rows when the user differs, but
-- two same-user NULL-namespace requests would also be allowed
-- — explicitly use COALESCE to a sentinel so namespace-scope
-- and user-scope share the same uniqueness gate.
CREATE UNIQUE INDEX IF NOT EXISTS deletion_requests_active_unique_idx
    ON deletion_requests (
        target_user_id,
        COALESCE(target_namespace, '*')
    )
    WHERE status IN ('requested', 'confirmed', 'soft_deleted');

COMMIT;

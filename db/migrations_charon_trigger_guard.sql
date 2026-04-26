-- migrations_charon_trigger_guard.sql
--
-- CHARON — targeted suppression for the version-snapshot trigger.
--
-- Background: when an MPF envelope's memory_versions sidecar arrives,
-- the import is restoring authoritative version history. If the
-- mnemos_version_snapshot trigger fires on the memory INSERT, it
-- synthesizes a fresh v1 row whose (memory_id, version_num) pair
-- collides with the envelope's authoritative v1 on the partial unique
-- index `idx_mv_main_linear (memory_id, version_num) WHERE
-- branch='main'`. The portability handler's ON CONFLICT (id) cannot
-- catch that collision because the conflict target is the natural key,
-- not the surrogate id.
--
-- Initial v3.3 fix used `SET LOCAL session_replication_role = replica`
-- to suppress the trigger for the import transaction. That works in
-- the dev container (where the app role is superuser), but it is
-- (a) unavailable to a non-superuser app role, which is the
-- production posture, and (b) overbroad — it also suppresses other
-- user-defined triggers and FK checks for the duration of the
-- transaction.
--
-- This migration replaces that mechanism with a targeted WHEN clause
-- on the three version-snapshot triggers. The portability handler now
-- sets `SET LOCAL mnemos.suppress_version_snapshot = '1'` for the
-- import transaction; only those three triggers consult that GUC,
-- and FK checks plus every other user trigger continue to fire.
--
-- Custom GUCs that contain a dot in their name (e.g. `mnemos.foo`)
-- can be SET LOCAL by any role in any transaction without superuser
-- privilege — Postgres treats them as user-defined session variables.
--
-- Idempotent: DROP TRIGGER IF EXISTS guards each recreate.

BEGIN;

DROP TRIGGER IF EXISTS trg_memory_version_insert ON memories;
CREATE TRIGGER trg_memory_version_insert
    AFTER INSERT ON memories
    FOR EACH ROW
    WHEN (current_setting('mnemos.suppress_version_snapshot', TRUE) IS DISTINCT FROM '1')
    EXECUTE FUNCTION mnemos_version_snapshot();

DROP TRIGGER IF EXISTS trg_memory_version_update ON memories;
CREATE TRIGGER trg_memory_version_update
    AFTER UPDATE ON memories
    FOR EACH ROW
    WHEN (current_setting('mnemos.suppress_version_snapshot', TRUE) IS DISTINCT FROM '1')
    EXECUTE FUNCTION mnemos_version_snapshot();

DROP TRIGGER IF EXISTS trg_memory_version_delete ON memories;
CREATE TRIGGER trg_memory_version_delete
    AFTER DELETE ON memories
    FOR EACH ROW
    WHEN (current_setting('mnemos.suppress_version_snapshot', TRUE) IS DISTINCT FROM '1')
    EXECUTE FUNCTION mnemos_version_snapshot();

COMMIT;

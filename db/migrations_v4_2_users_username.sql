-- migrations_v4_2_users_username.sql
--
-- Bring the Postgres `users` table into parity with the SQLite shape
-- (db/migrations_sqlite/migrations.sql:215-221) by adding `username`.
-- The persistence-parity tests that exercise both backends issue
-- INSERT INTO users (id, username, role, namespace) VALUES (...) and
-- expected the column on both sides since the SQLite migration that
-- introduced it (>= v3.x). PG was never migrated; v4.2.0a3 master CI
-- surfaced the drift on five test_persistence_parity tests as
-- ``UndefinedColumnError: column "username" of relation "users"``.
--
-- Idempotent. Safe to apply over a populated `users` table:
--   1. ADD COLUMN nullable so existing rows do not violate NOT NULL.
--   2. Backfill from id (id is the user_id, suitable as a default
--      handle when no human-readable username was set during a
--      pre-migration insert).
--   3. UNIQUE INDEX over the column so future inserts get the same
--      uniqueness guarantee SQLite enforces inline.
--
-- Why not NOT NULL: existing rows MAY have NULL username after
-- migrations from older deployments. Backfilling from id keeps every
-- row set, but a hard NOT NULL would block this migration on a stale
-- snapshot. The application-side INSERT path supplies a username
-- value, so new rows are non-null by construction.

BEGIN;

-- 1. Add the column nullable so the migration can succeed against
--    populated tables; backfill from id; then enforce NOT NULL.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS username TEXT;

UPDATE users
SET username = id
WHERE username IS NULL;

-- 2. Default-trigger so future INSERTs that omit username (e.g. the
--    OAuth user-creation path that doesn't yet supply one) still
--    populate the column. Without this, the SQLite invariant
--    ``username UNIQUE NOT NULL`` would still be weaker on PG.
CREATE OR REPLACE FUNCTION mnemos_users_default_username() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.username IS NULL OR NEW.username = '' THEN
        NEW.username := NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_users_default_username ON users;
CREATE TRIGGER trg_users_default_username
    BEFORE INSERT OR UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION mnemos_users_default_username();

-- 3. Now that backfill + trigger guarantee non-null, enforce it
--    at the column level. Idempotent: SET NOT NULL is a no-op if
--    already set.
ALTER TABLE users
    ALTER COLUMN username SET NOT NULL;

-- 4. Plain UNIQUE INDEX (not partial) so the invariant matches
--    SQLite's ``UNIQUE NOT NULL``. Drop the partial index from any
--    previously-applied earlier-shape of this migration before
--    creating the canonical one.
DROP INDEX IF EXISTS users_username_unique_idx;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_key
    ON users (username);

COMMIT;

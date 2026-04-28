-- MNEMOS v3.5 — namespace state and journal
--
-- v3 ownership made state and journal owner-scoped. This migration adds the
-- second tenancy dimension used by the rest of the memory core.
--
-- Idempotent. Exact state key duplicates under the new key abort the migration;
-- there is no safe automatic value/version winner.

ALTER TABLE state
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';
ALTER TABLE journal
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

UPDATE state SET namespace = 'default' WHERE namespace IS NULL;
UPDATE journal SET namespace = 'default' WHERE namespace IS NULL;

ALTER TABLE state ALTER COLUMN namespace SET DEFAULT 'default';
ALTER TABLE state ALTER COLUMN namespace SET NOT NULL;
ALTER TABLE journal ALTER COLUMN namespace SET DEFAULT 'default';
ALTER TABLE journal ALTER COLUMN namespace SET NOT NULL;

DO $$
DECLARE
    _owner_attnum     SMALLINT;
    _namespace_attnum SMALLINT;
    _key_attnum       SMALLINT;
    _conname          TEXT;
    _state_pk         TEXT;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT owner_id, namespace, key, COUNT(*) AS n
            FROM state
            GROUP BY owner_id, namespace, key
            HAVING COUNT(*) > 1
        ) AS dup
    ) THEN
        RAISE EXCEPTION
            'Cannot widen state uniqueness: duplicate (owner_id, namespace, key) rows exist';
    END IF;

    SELECT attnum INTO _owner_attnum
      FROM pg_attribute
     WHERE attrelid = 'state'::regclass AND attname = 'owner_id';
    SELECT attnum INTO _namespace_attnum
      FROM pg_attribute
     WHERE attrelid = 'state'::regclass AND attname = 'namespace';
    SELECT attnum INTO _key_attnum
      FROM pg_attribute
     WHERE attrelid = 'state'::regclass AND attname = 'key';

    SELECT conname INTO _state_pk
      FROM pg_constraint
     WHERE conrelid = 'state'::regclass
       AND contype = 'p';

    IF _state_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'state'::regclass
           AND contype = 'p'
           AND conkey = ARRAY[
                _owner_attnum,
                _namespace_attnum,
                _key_attnum
           ]::SMALLINT[]
    ) THEN
        EXECUTE format('ALTER TABLE state DROP CONSTRAINT %I', _state_pk);
    END IF;

    -- Drop any old narrower unique constraint by column set.
    FOR _conname IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'state'::regclass
           AND contype = 'u'
           AND conkey IN (
                ARRAY[
                    _owner_attnum,
                    _key_attnum
                ]::SMALLINT[],
                ARRAY[
                    _key_attnum
                ]::SMALLINT[]
           )
    LOOP
        EXECUTE format('ALTER TABLE state DROP CONSTRAINT %I', _conname);
    END LOOP;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'state'::regclass
           AND contype IN ('p', 'u')
           AND conkey = ARRAY[
                _owner_attnum,
                _namespace_attnum,
                _key_attnum
           ]::SMALLINT[]
    ) THEN
        ALTER TABLE state ADD PRIMARY KEY (owner_id, namespace, key);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_state_owner_namespace
    ON state(owner_id, namespace);
CREATE INDEX IF NOT EXISTS idx_journal_owner_namespace
    ON journal(owner_id, namespace);

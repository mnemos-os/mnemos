-- MNEMOS v3.5 — widen entity uniqueness to include namespace
--
-- Entities gained namespace in v3.2, but the UNIQUE constraint stayed at
-- (owner_id, entity_type, name). That prevented the same owner from having the
-- same entity name/type in separate namespaces and left handler ON CONFLICT
-- clauses with a one-dimensional tenancy key.
--
-- Idempotent. If exact duplicates already exist under the new key, abort; there
-- is no safe automatic winner for entity identity.

DO $$
DECLARE
    _owner_attnum       SMALLINT;
    _namespace_attnum   SMALLINT;
    _entity_type_attnum SMALLINT;
    _name_attnum        SMALLINT;
    _old_conname        TEXT;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT owner_id, namespace, entity_type, name, COUNT(*) AS n
            FROM entities
            GROUP BY owner_id, namespace, entity_type, name
            HAVING COUNT(*) > 1
        ) AS dup
    ) THEN
        RAISE EXCEPTION
            'Cannot widen entities uniqueness: duplicate (owner_id, namespace, entity_type, name) rows exist';
    END IF;

    SELECT attnum INTO _owner_attnum
      FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'owner_id';
    SELECT attnum INTO _namespace_attnum
      FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'namespace';
    SELECT attnum INTO _entity_type_attnum
      FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'entity_type';
    SELECT attnum INTO _name_attnum
      FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'name';

    -- Drop narrower unique keys by column set, not name. The canonical prior
    -- key is (owner_id, entity_type, name); some long-lived installs may still
    -- carry the original (entity_type, name) key as well.
    FOR _old_conname IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'entities'::regclass
           AND contype = 'u'
           AND conkey IN (
                ARRAY[
                    _owner_attnum,
                    _entity_type_attnum,
                    _name_attnum
                ]::SMALLINT[],
                ARRAY[
                    _entity_type_attnum,
                    _name_attnum
                ]::SMALLINT[]
           )
    LOOP
        EXECUTE format('ALTER TABLE entities DROP CONSTRAINT %I', _old_conname);
    END LOOP;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'entities'::regclass
           AND contype = 'u'
           AND conkey = ARRAY[
                _owner_attnum,
                _namespace_attnum,
                _entity_type_attnum,
                _name_attnum
           ]::SMALLINT[]
    ) THEN
        ALTER TABLE entities
            ADD CONSTRAINT entities_owner_namespace_type_name_key
            UNIQUE (owner_id, namespace, entity_type, name);
    END IF;
END$$;

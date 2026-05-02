-- migrations_v4_2_deletion_requests_blank_namespace_cleanup.sql
--
-- Round-78 normalized API inputs so blank ``target_namespace``
-- ("" or whitespace) is rejected at the route boundary. Round
-- -79/80/81 close the legacy-data window: any deployment that
-- ran the round-77 alpha could have persisted active rows with
-- ``target_namespace=''`` or whitespace-only values
-- (including Unicode whitespace, since Python's ``.strip()``
-- normalizes those at the API but the DB persisted whatever
-- the operator submitted).
--
-- Codex review iterations 2..4 surfaced a series of related
-- correctness gaps:
--
--   * review-2 (round-78): legacy ``target_namespace=''`` rows
--     bypassed the new overlap guard.
--   * review-3 (round-79): naive UPDATE could ``unique_violation``
--     on Class A (NULL+blank for same user) OR leave Class B
--     overlap (blank+specific for same user). ``BTRIM`` only
--     trims spaces, missing tabs/newlines.
--   * review-4 (round-80): the round-80 DO blocks dropped a
--     ``;`` after ``END`` (PL/pgSQL parse failure — migration
--     would never run); Class B preflight missed the
--     NULL-vs-specific overlap (round-77's partial unique
--     index allowed it); POSIX ``[[:space:]]`` doesn't match
--     Python's ``str.strip()`` Unicode-whitespace semantics
--     (NBSP, em-space, etc. survived).
--
-- Round-81 fixes all three review-4 findings:
--
--   1. SQL parse: every DO block ends with ``END;`` inside the
--      ``$do$`` quotes (PL/pgSQL block terminator).
--   2. Class B preflight widened: matches active rows where
--      ``target_namespace IS NULL OR is_blank(...)`` against
--      any active specific row for the same user. Round-77's
--      NULL-vs-specific overlap is now caught.
--   3. Unicode whitespace: defines a helper SQL function
--      ``mnemos_is_blank_namespace(text)`` that uses an
--      explicit ``regexp_replace`` over a character class
--      enumerating ALL Python ``str.isspace()`` characters
--      via ``\uXXXX`` escapes (so the SQL is editor-safe
--      and reviewers can read the codepoints directly):
--      ASCII whitespace + Unicode characters with
--      White_Space=Yes per the Unicode standard (NEL, NBSP,
--      OGHAM SPACE MARK, EN/EM SPACES, LINE/PARAGRAPH
--      SEPARATORS, NARROW/MEDIUM/IDEOGRAPHIC SPACES). The
--      function is used in the UPDATE WHERE, CHECK
--      constraint, AND runtime overlap SELECT in
--      ``mnemos/api/routes/admin.py`` so the API and DB
--      agree on what "blank" means.
--
-- Idempotent. Re-runs are safe — the preflight passes on a
-- post-cleanup database.

BEGIN;

-- Helper function: returns TRUE if the input is NULL OR
-- consists entirely of Python ``str.strip()`` whitespace.
-- IMMUTABLE so it can be used in a CHECK constraint.
--
-- The regex char class enumerates the full Python
-- ``str.isspace()`` Unicode set via ``\uXXXX`` escapes
-- (https://www.postgresql.org/docs/current/sql-syntax-
-- lexical.html#SQL-SYNTAX-STRINGS-ESCAPE). Postgres replaces
-- each escape with the literal codepoint BEFORE the regex
-- engine parses the pattern, so the engine sees actual
-- whitespace characters in the class. Comments use ASCII-
-- only ``U+XXXX`` notation so the file parses cleanly under
-- ``psql -f`` (codex review-5 of round-81 caught that
-- embedded literal control characters in earlier comment
-- tables silently broke the migration text).
--
-- Codepoints in the char class:
--   U+0009..U+000D : ASCII whitespace range (HT, LF, VT, FF, CR)
--   U+001C..U+001F : INFORMATION SEPARATORS FOUR..ONE (FS, GS, RS, US)
--                    Python str.strip() includes these.
--   U+0020         : SPACE
--   U+0085         : NEXT LINE (NEL)
--   U+00A0         : NO-BREAK SPACE (NBSP)
--   U+1680         : OGHAM SPACE MARK
--   U+2000..U+200A : EN QUAD through HAIR SPACE
--   U+2028         : LINE SEPARATOR
--   U+2029         : PARAGRAPH SEPARATOR
--   U+202F         : NARROW NO-BREAK SPACE
--   U+205F         : MEDIUM MATHEMATICAL SPACE
--   U+3000         : IDEOGRAPHIC SPACE
--
-- These match the Python C-source ``Py_UNICODE_ISSPACE``
-- predicate which ``str.strip()`` and ``str.isspace()``
-- consult.
CREATE OR REPLACE FUNCTION mnemos_is_blank_namespace(value TEXT)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT value IS NULL
        OR length(regexp_replace(
                value,
                E'[\u0009-\u000D\u001C-\u001F\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]',
                '',
                'g'
           )) = 0;
$$;

-- Take an EXCLUSIVE lock on deletion_requests so a concurrent
-- INSERT can't race the preflight: in particular, a concurrent
-- ``create_deletion_request`` could persist a blank-namespace
-- row AFTER the preflight passes but BEFORE the UPDATE +
-- CHECK land, leaving the migration in a "verified clean"
-- state with new dirty data. EXCLUSIVE blocks all writes for
-- the duration of the migration.
LOCK TABLE deletion_requests IN EXCLUSIVE MODE;

DO $do$
DECLARE
    class_a_collisions TEXT;
    class_b_collisions TEXT;
BEGIN
    -- Class A: multiple active rows for same user where each
    -- is NULL or Unicode-blank. After normalization all become
    -- NULL, violating the partial unique index.
    SELECT string_agg(DISTINCT target_user_id, ', ' ORDER BY target_user_id)
      INTO class_a_collisions
      FROM (
        SELECT target_user_id
          FROM deletion_requests
         WHERE status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted')
           AND mnemos_is_blank_namespace(target_namespace)
         GROUP BY target_user_id
        HAVING COUNT(*) > 1
      ) sub;

    IF class_a_collisions IS NOT NULL THEN
        RAISE EXCEPTION
          'deletion_requests cleanup aborted - Class A '
          'collision: target_user_id(s) % have multiple '
          'active rows that all-normalize to NULL '
          'target_namespace. Operators must manually '
          'cancel/progress the duplicates before re-running '
          'this migration.', class_a_collisions
          USING HINT =
            'Run: SELECT id, target_user_id, target_namespace, '
            'status FROM deletion_requests WHERE '
            'target_user_id IN (...) AND status IN '
            '(''requested'', ''confirmed'', ''sweep_verifying'', ''soft_deleted'') '
            'AND mnemos_is_blank_namespace(target_namespace) '
            'ORDER BY target_user_id, requested_at;';
    END IF;

    -- Class B (widened in round-81): an active all-namespace-
    -- equivalent row (target_namespace IS NULL OR Unicode-
    -- blank) coexists with an active specific-namespace row
    -- for the same user. Round-77's partial unique index
    -- allowed (alice, NULL) + (alice, 'tenant-a') simultaneously
    -- because NULL-coalesced-to-'*' and 'tenant-a' are
    -- distinct keys; codex review-4 of round-80 caught that
    -- round-80's Class B preflight only matched
    -- (blank, specific) and missed (NULL, specific). The
    -- predicate now matches both via mnemos_is_blank_namespace.
    SELECT string_agg(DISTINCT target_user_id, ', ' ORDER BY target_user_id)
      INTO class_b_collisions
      FROM (
        SELECT all_ns_row.target_user_id
          FROM deletion_requests all_ns_row
          JOIN deletion_requests specific_row
            USING (target_user_id)
         WHERE all_ns_row.status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted')
           AND specific_row.status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted')
           AND mnemos_is_blank_namespace(all_ns_row.target_namespace)
           AND NOT mnemos_is_blank_namespace(specific_row.target_namespace)
           AND all_ns_row.id <> specific_row.id
      ) sub;

    IF class_b_collisions IS NOT NULL THEN
        RAISE EXCEPTION
          'deletion_requests cleanup aborted - Class B '
          'collision: target_user_id(s) % have an active '
          'all-namespace-equivalent row (NULL or '
          'whitespace-only) AND an active specific-namespace '
          'row simultaneously. The all-namespaces scope '
          'contains the specific-namespace one, which is '
          'forbidden by round-78 containment rules. Operators '
          'must manually cancel one before re-running this '
          'migration.', class_b_collisions
          USING HINT =
            'Run: SELECT id, target_user_id, target_namespace, '
            'status FROM deletion_requests WHERE '
            'target_user_id IN (...) AND status IN '
            '(''requested'', ''confirmed'', ''sweep_verifying'', ''soft_deleted'') '
            'ORDER BY target_user_id, requested_at;';
    END IF;
END;
$do$;

-- Step 2: normalize blank/whitespace-only target_namespace to
-- NULL across all lifecycle states. Uses mnemos_is_blank
-- _namespace so ASCII whitespace, Unicode whitespace
-- (NBSP, em-space, narrow no-break, ideographic, etc.), and
-- empty strings all collapse to NULL.
UPDATE deletion_requests
   SET target_namespace = NULL
 WHERE target_namespace IS NOT NULL
   AND mnemos_is_blank_namespace(target_namespace);

-- Step 3: forbid future blank/whitespace-only target_namespace
-- inserts at the DB level. Defense-in-depth — the route
-- validator (_normalize_deletion_target) is the primary gate.
DO $do$
BEGIN
    ALTER TABLE deletion_requests
        ADD CONSTRAINT deletion_requests_namespace_not_blank
            CHECK (
                target_namespace IS NULL
                OR NOT mnemos_is_blank_namespace(target_namespace)
            );
EXCEPTION
    WHEN duplicate_object THEN
        -- Constraint already exists (idempotent re-run); ignore.
        NULL;
END;
$do$;

COMMIT;

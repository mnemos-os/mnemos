-- migrations_v3_5_rls_group_select_unix_bits.sql
--
-- v3.5 (slice 2.5 task #25) — replace the group-read RLS policy so
-- it checks the Unix group read bit directly, matching the application
-- layer's read_visibility_predicate group branch.

BEGIN;

DROP POLICY mnemos_group_select ON memories;

CREATE POLICY mnemos_group_select ON memories
    FOR SELECT TO mnemos_user
    USING (
        ((permission_mode / 10) % 10) >= 4
        AND group_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM user_groups
            WHERE user_id::text = current_setting('mnemos.current_user_id', TRUE)
              AND group_id = memories.group_id
        )
    );

COMMIT;

-- migrations_v4_2_compression_dag.sql
--
-- Wire compression derivations into the memory_versions DAG.
--
-- Successful compression contests now write child versions on derived
-- branches:
--   * distilled: raw/dense compression artifact
--   * narrated:  prose-shaped narration/extractive variant
--
-- Branch names remain convention-only. The schema enforces only the
-- change_type expansion needed for these derivation commits.

BEGIN;

ALTER TABLE memory_versions
    DROP CONSTRAINT IF EXISTS memory_versions_change_type_check;

ALTER TABLE memory_versions
    ADD CONSTRAINT memory_versions_change_type_check
    CHECK (change_type IN ('create', 'update', 'delete', 'compress'));

COMMENT ON COLUMN memory_versions.change_type IS
    'Version operation: create, update, delete, or compress for derived compression artifacts.';

COMMENT ON COLUMN memory_versions.branch IS
    'Version branch. main is the live memory branch; distilled and narrated are compression derivation conventions.';

CREATE INDEX IF NOT EXISTS idx_mv_memory_branch
    ON memory_versions(memory_id, branch);

GRANT SELECT, INSERT ON memory_versions TO mnemos_user;
GRANT SELECT, INSERT, UPDATE ON memory_branches TO mnemos_user;

COMMIT;

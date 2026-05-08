-- MNEMOS v5.0.3 — upgrade legacy timestamp columns to TIMESTAMPTZ.
--
-- v3.x-era Postgres installs created several lifecycle columns as
-- TIMESTAMP WITHOUT TIME ZONE while newer subsystem tables use
-- TIMESTAMPTZ. asyncpg rejects mixed aware/naive comparisons and
-- encodes, so interpret existing legacy values as UTC and make the
-- stored type explicit.

DROP VIEW IF EXISTS v_unreviewed_compressions;
DROP VIEW IF EXISTS v_compression_stats;

DO $$
DECLARE
    column_target record;
    table_reg regclass;
    current_type text;
BEGIN
    FOR column_target IN
        SELECT *
        FROM (VALUES
            ('memories', 'created'),
            ('memories', 'updated'),
            ('memories', 'compressed_at'),
            ('compression_quality_log', 'created'),
            ('compression_quality_log', 'reviewed_at'),
            ('graeae_consultations', 'created'),
            ('state', 'updated'),
            ('journal', 'created'),
            ('entities', 'created'),
            ('entities', 'updated'),
            ('model_registry', 'first_seen'),
            ('model_registry', 'last_seen'),
            ('model_registry', 'last_synced'),
            ('model_registry_sync_log', 'synced_at')
        ) AS v(table_name, column_name)
    LOOP
        table_reg := to_regclass(column_target.table_name);
        IF table_reg IS NULL THEN
            CONTINUE;
        END IF;

        SELECT format_type(a.atttypid, a.atttypmod)
          INTO current_type
          FROM pg_attribute a
         WHERE a.attrelid = table_reg
           AND a.attname = column_target.column_name
           AND NOT a.attisdropped;

        IF current_type = 'timestamp without time zone' THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I TYPE TIMESTAMPTZ USING %I AT TIME ZONE %L',
                table_reg,
                column_target.column_name,
                column_target.column_name,
                'UTC'
            );
        END IF;
    END LOOP;
END $$;

CREATE OR REPLACE VIEW v_compression_stats AS
SELECT
    COUNT(*) AS total_compressions,
    COUNT(*) FILTER (WHERE reviewed) AS reviewed,
    COUNT(*) FILTER (WHERE NOT reviewed) AS unreviewed,
    AVG(CAST(quality_rating AS FLOAT)) AS avg_quality,
    AVG(compression_ratio) AS avg_ratio
FROM compression_quality_log;

CREATE OR REPLACE VIEW v_unreviewed_compressions AS
SELECT
    cql.id,
    cql.memory_id,
    cql.original_token_count AS original_size,
    cql.compressed_token_count AS compressed_size,
    cql.compression_ratio,
    cql.created AS compressed_at,
    m.category,
    m.content
FROM compression_quality_log cql
LEFT JOIN memories m ON m.id = cql.memory_id
WHERE NOT cql.reviewed
ORDER BY cql.created DESC;

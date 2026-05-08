"""Regression coverage for v5.0.3 timezone-aware Postgres timestamps."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio


PG_URL = os.getenv("MNEMOS_TEST_DB") or os.getenv("MNEMOS_TEST_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="set MNEMOS_TEST_DB or MNEMOS_TEST_PG_URL to run real-Postgres timezone tests",
)


@pytest_asyncio.fixture
async def legacy_timestamp_pool():
    import asyncpg

    schema = f"tz_fix_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    await admin.close()

    pool = await asyncpg.create_pool(
        PG_URL,
        min_size=1,
        max_size=2,
        server_settings={"search_path": f"{schema},public"},
    )
    try:
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(PG_URL)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_morpheus_cluster_phase_handles_legacy_timestamp_columns_after_tz_migration(
    legacy_timestamp_pool,
):
    from mnemos.domain.morpheus.runner import phase_cluster

    run_id = uuid.uuid4()
    legacy_created = datetime(2026, 4, 3, 6, 49, 29)
    window_start = legacy_created.replace(tzinfo=timezone.utc) - timedelta(minutes=5)
    window_end = legacy_created.replace(tzinfo=timezone.utc) + timedelta(minutes=5)
    migration_sql = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations_v5_0_3_timestamp_tz_upgrade.sql"
    ).read_text()

    async with legacy_timestamp_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT,
                metadata JSONB,
                quality_rating INT,
                verbatim_content TEXT,
                owner_id TEXT,
                group_id TEXT,
                namespace TEXT,
                permission_mode INT,
                source_model TEXT,
                source_provider TEXT,
                source_session TEXT,
                source_agent TEXT,
                created TIMESTAMP NOT NULL DEFAULT NOW(),
                updated TIMESTAMP DEFAULT NOW(),
                compressed_at TIMESTAMP,
                embedding TEXT,
                provenance TEXT,
                morpheus_run_id UUID,
                deleted_at TIMESTAMPTZ,
                archived_at TIMESTAMPTZ,
                consolidated_into TEXT
            );

            CREATE TABLE compression_quality_log (
                id UUID PRIMARY KEY,
                memory_id TEXT,
                original_token_count INT NOT NULL DEFAULT 1,
                compressed_token_count INT NOT NULL DEFAULT 1,
                compression_ratio FLOAT NOT NULL DEFAULT 1.0,
                quality_rating INT NOT NULL DEFAULT 100,
                reviewed BOOLEAN NOT NULL DEFAULT FALSE,
                created TIMESTAMP NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMP
            );

            CREATE TABLE morpheus_runs (
                id UUID PRIMARY KEY,
                cluster_min_size INT NOT NULL,
                window_started_at TIMESTAMPTZ,
                window_ended_at TIMESTAMPTZ,
                namespace TEXT,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                clusters_found INT NOT NULL DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO morpheus_runs (
                id, cluster_min_size, window_started_at, window_ended_at,
                namespace
            )
            VALUES ($1, 1, $2, $3, NULL)
            """,
            run_id,
            window_start,
            window_end,
        )
        await conn.execute(
            """
            INSERT INTO memories (
                id, content, category, created, updated, embedding, namespace,
                permission_mode
            )
            VALUES ($1, $2, 'facts', $3, $3, $4, 'default', 644)
            """,
            "mem_tz_fix",
            "Real-corpus MORPHEUS timestamp regression fixture.",
            legacy_created,
            "[1.0, 0.0, 0.0]",
        )
        await conn.execute(migration_sql)
        created_type = await conn.fetchval(
            """
            SELECT data_type
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'memories'
               AND column_name = 'created'
            """
        )

    assert created_type == "timestamp with time zone"
    assert await phase_cluster(legacy_timestamp_pool, str(run_id)) == 1

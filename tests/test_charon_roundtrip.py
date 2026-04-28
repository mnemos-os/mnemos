"""CHARON round-trip integration test against real Postgres.

Verifies that an envelope produced by `GET /v1/export?include_sidecars=true`
and consumed by `POST /v1/import?preserve_owner=true` is lossless on
the four CHARON surfaces (memories + kg_triples + memory_versions +
memory_compressed_variants) when run against a real database.

The mocked unit tests in `test_portability.py` cover wire-shape
correctness and access-control rules. This test catches the integration
seams the unit tests can't see — `ON CONFLICT` keys, UUID coercion,
JSONB serialization, type-cast edge cases.

To run locally against a fresh Postgres (with pgvector + pgcrypto):

  MNEMOS_TEST_PG_URL=postgresql://user:pw@host:port/db \\
    pytest tests/test_charon_roundtrip.py -v

The DB must already have all 21 migrations applied. The test
truncates the four CHARON tables between phases; it does NOT touch
schema or any other tables. Don't point this at a database with
real data unless you understand what gets cleared.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import pytest
import pytest_asyncio

PG_URL = os.getenv("MNEMOS_TEST_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="requires MNEMOS_TEST_PG_URL pointing at a CHARON test DB",
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pool():
    """asyncpg pool wired into mnemos.core.lifecycle._pool so the portability
    handlers can reach it the same way they would in production."""
    import asyncpg

    import mnemos.core.lifecycle as lc

    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
    prior = lc._pool
    lc._pool = pool
    try:
        yield pool
    finally:
        lc._pool = prior
        await pool.close()


async def _truncate_charon_tables(pool):
    """Wipe the four CHARON surfaces. Order matters because of FKs:
    compression variants reference candidates which reference memories;
    versions also depend on memories. CASCADE handles the rest."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE memory_compressed_variants, memory_compression_candidates, "
            "memory_versions, kg_triples, memories CASCADE"
        )


@pytest_asyncio.fixture
async def fresh_db(pool):
    """Truncate before AND after to keep each test isolated even if a
    prior crash left rows behind."""
    await _truncate_charon_tables(pool)
    yield pool
    await _truncate_charon_tables(pool)


# ─── Seed data ───────────────────────────────────────────────────────────────


SEED_MEMORY_ID = "mem_charon_alice_001"
SEED_KG_ID = "kg_charon_alice_001"
SEED_OWNER = "alice"
SEED_NAMESPACE = "alice-ns"
# memories.created/updated are TIMESTAMP (naive) per migrations.sql;
# memory_versions.snapshot_at and others are TIMESTAMPTZ. Use naive
# UTC and let the handler's _parse_iso normalize on round-trip.
SEED_TIMESTAMP = datetime(2026, 1, 15, 10, 30, 0)
SEED_TIMESTAMP_TZ = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


async def _seed(pool):
    """Insert one row per CHARON surface. The point is to cover every
    column that the export/import codepaths touch, not to be
    exhaustive — minor variations on the same shape would just slow
    the test without adding signal."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. memory
            await conn.execute(
                """
                INSERT INTO memories (
                    id, content, category, subcategory, metadata,
                    quality_rating, owner_id, namespace, permission_mode,
                    source_model, source_provider, source_session, source_agent,
                    created, updated
                )
                VALUES (
                    $1, $2, $3, $4, $5::jsonb,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13,
                    $14, $14
                )
                """,
                SEED_MEMORY_ID, "Paris is the capital of France.",
                "facts", "geography",
                json.dumps({"src": "charon-roundtrip-test"}),
                85, SEED_OWNER, SEED_NAMESPACE, 600,
                "claude-opus-4-7", "anthropic", "session_charon", "tester",
                SEED_TIMESTAMP,
            )

            # 2. kg_triple
            await conn.execute(
                """
                INSERT INTO kg_triples (
                    id, subject, predicate, object,
                    subject_type, object_type,
                    valid_from, memory_id, confidence,
                    created, owner_id, namespace
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6,
                    $7, $8, $9,
                    $10, $11, $12
                )
                """,
                SEED_KG_ID, "Paris", "capitalOf", "France",
                "place", "place",
                SEED_TIMESTAMP_TZ, SEED_MEMORY_ID, 0.95,
                SEED_TIMESTAMP_TZ, SEED_OWNER, SEED_NAMESPACE,
            )

            # 3. memory_version: the mnemos_version_snapshot() trigger
            # has already auto-inserted version 1 on the memories
            # INSERT above (with content-addressed commit_hash). We
            # don't explicitly insert another version row — the
            # trigger-created row IS the seed, and the round-trip
            # must preserve it. Insert version 2 manually so the test
            # also covers a non-trigger-managed branch row.
            await conn.execute(
                """
                INSERT INTO memory_versions (
                    memory_id, version_num, content, category, subcategory,
                    metadata, verbatim_content,
                    owner_id, namespace, permission_mode,
                    source_model, source_provider, source_session, source_agent,
                    snapshot_at, snapshot_by, change_type,
                    commit_hash, branch
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6::jsonb, $7,
                    $8, $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19
                )
                """,
                SEED_MEMORY_ID, 2,
                "Paris is the capital of France.", "facts", "geography",
                json.dumps({"src": "charon-roundtrip-test", "rev": 2}),
                "Paris is the capital of France.",
                SEED_OWNER, SEED_NAMESPACE, 600,
                "claude-opus-4-7", "anthropic", "session_charon", "tester",
                SEED_TIMESTAMP_TZ, "alice", "update",
                "abc123def456", "main",
            )

            # 4. memory_compressed_variants — needs a candidate row first
            # since winner_candidate_id references it.
            cand_id = uuid.uuid4()
            contest_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO memory_compression_candidates (
                    id, memory_id, owner_id, contest_id,
                    engine_id, engine_version,
                    compressed_content, original_tokens, compression_ratio,
                    composite_score, scoring_profile, gpu_used, is_winner,
                    judge_model, created
                )
                VALUES ($1, $2, $3, $4, $5, $6,
                        $7, $8, $9,
                        $10, $11, $12, $13,
                        $14, $15)
                """,
                cand_id, SEED_MEMORY_ID, SEED_OWNER, contest_id,
                "apollo", "1.0",
                "PAR=cap(FRA)", 10, 2.5,
                0.81, "balanced", False, True,
                "claude-opus-4-7", SEED_TIMESTAMP_TZ,
            )
            await conn.execute(
                """
                INSERT INTO memory_compressed_variants (
                    memory_id, owner_id, winner_candidate_id,
                    engine_id, engine_version, compressed_content,
                    compressed_tokens, compression_ratio,
                    quality_score, composite_score,
                    scoring_profile, judge_model, selected_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                SEED_MEMORY_ID, SEED_OWNER, cand_id,
                "apollo", "1.0", "PAR=cap(FRA)",
                4, 2.5,
                0.87, 0.81,
                "balanced", "claude-opus-4-7", SEED_TIMESTAMP_TZ,
            )


async def _snapshot(pool) -> Dict[str, Any]:
    """Return the current state of the four CHARON tables as plain
    dicts so we can compare pre-export vs post-import for equality."""
    async with pool.acquire() as conn:
        memories = await conn.fetch(
            "SELECT id, content, category, subcategory, owner_id, namespace, "
            "permission_mode, quality_rating, source_model, source_provider, "
            "source_session, source_agent, metadata, created, updated "
            "FROM memories WHERE id = $1",
            SEED_MEMORY_ID,
        )
        kg_triples = await conn.fetch(
            "SELECT id, subject, predicate, object, subject_type, object_type, "
            "memory_id, confidence, owner_id, namespace "
            "FROM kg_triples WHERE id = $1",
            SEED_KG_ID,
        )
        versions = await conn.fetch(
            "SELECT id::text AS id, memory_id, version_num, content, category, "
            "subcategory, verbatim_content, owner_id, namespace, "
            "permission_mode, source_model, snapshot_by, change_type, "
            "commit_hash, branch "
            "FROM memory_versions WHERE memory_id = $1 ORDER BY version_num",
            SEED_MEMORY_ID,
        )
        variants = await conn.fetch(
            "SELECT memory_id, owner_id, engine_id, engine_version, "
            "compressed_content, compressed_tokens, compression_ratio, "
            "quality_score, composite_score, scoring_profile, judge_model "
            "FROM memory_compressed_variants WHERE memory_id = $1",
            SEED_MEMORY_ID,
        )
    return {
        "memories": [dict(r) for r in memories],
        "kg_triples": [dict(r) for r in kg_triples],
        "memory_versions": [dict(r) for r in versions],
        "memory_compressed_variants": [dict(r) for r in variants],
    }


def _root_user():
    from mnemos.api.dependencies import UserContext
    return UserContext(
        user_id="root_admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_charon_full_round_trip(fresh_db):
    """End-to-end: seed → export(include_sidecars=True) → truncate
    → import(preserve_owner=True) → snapshot. The before-export and
    after-import snapshots must be identical."""
    pool = fresh_db
    from mnemos.api.routes import portability

    await _seed(pool)
    before = await _snapshot(pool)

    assert len(before["memories"]) == 1, "seed should produce exactly one memory"
    assert len(before["kg_triples"]) == 1
    # Two memory_versions: v1 auto-created by the mnemos_version_snapshot
    # trigger on the memory INSERT, v2 inserted explicitly by _seed.
    assert len(before["memory_versions"]) == 2
    assert len(before["memory_compressed_variants"]) == 1

    # Export under root with the seeded owner+namespace targeted.
    envelope = await portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=SEED_OWNER, namespace=SEED_NAMESPACE,
        include_sidecars=True, user=_root_user(),
    )

    # Sanity: envelope contains exactly the rows we expect.
    assert envelope.record_count == 1
    assert envelope.kg_triples is not None and len(envelope.kg_triples) == 1
    assert envelope.memory_versions is not None and len(envelope.memory_versions) == 2
    assert envelope.compression_manifest is not None and len(envelope.compression_manifest) == 1

    # Wipe the slate. Re-importing into the same DB exercises ON
    # CONFLICT (which we've separately tested); the CHARON contract
    # is that an export+import against an empty DB reproduces state.
    await _truncate_charon_tables(pool)
    cleared = await _snapshot(pool)
    assert all(len(v) == 0 for v in cleared.values()), "truncate should clear all four tables"

    # Round-trip the envelope back in.
    stats = await portability.import_memories(
        envelope=envelope, preserve_owner=True, user=_root_user(),
    )

    assert stats.imported == 1, f"memory import failed: {stats.errors}"
    assert stats.failed == 0, f"unexpected failures: {stats.errors}"
    assert stats.sidecars_imported == {
        "kg_triples": 1,
        "memory_versions": 2,
        "compression_manifest": 1,
    }, f"sidecar import counts off: {stats.sidecars_imported}; errors={stats.errors}"

    after = await _snapshot(pool)

    # Direct equality: every column we care about must match across
    # the round-trip. JSON columns serialize to dicts (asyncpg
    # auto-decodes), so plain == comparison is correct.
    assert after["memories"] == before["memories"], (
        "memory roundtrip lost data:\n"
        f"  before: {before['memories']}\n"
        f"  after:  {after['memories']}"
    )
    assert after["kg_triples"] == before["kg_triples"], (
        "kg_triples roundtrip lost data:\n"
        f"  before: {before['kg_triples']}\n"
        f"  after:  {after['kg_triples']}"
    )
    assert after["memory_versions"] == before["memory_versions"], (
        "memory_versions roundtrip lost data:\n"
        f"  before: {before['memory_versions']}\n"
        f"  after:  {after['memory_versions']}"
    )
    assert after["memory_compressed_variants"] == before["memory_compressed_variants"], (
        "memory_compressed_variants roundtrip lost data:\n"
        f"  before: {before['memory_compressed_variants']}\n"
        f"  after:  {after['memory_compressed_variants']}"
    )


@pytest.mark.asyncio
async def test_charon_re_import_is_idempotent(fresh_db):
    """Re-running the same import against an already-populated DB
    must be a no-op — not an error, not a partial overwrite. The
    on-the-wire ON CONFLICT DO NOTHING contract translates to
    `imported=0, skipped=N` on the second pass."""
    pool = fresh_db
    from mnemos.api.routes import portability

    await _seed(pool)

    envelope = await portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=SEED_OWNER, namespace=SEED_NAMESPACE,
        include_sidecars=True, user=_root_user(),
    )

    # First import is a no-op too — the seeded rows are already there
    # under the same ids.
    stats_1 = await portability.import_memories(
        envelope=envelope, preserve_owner=True, user=_root_user(),
    )
    assert stats_1.imported == 0
    assert stats_1.skipped == 1
    assert stats_1.sidecars_imported == {}
    assert stats_1.sidecars_skipped == {
        "kg_triples": 1,
        "memory_versions": 2,  # trigger-created v1 + manual v2
        "compression_manifest": 1,
    }

    # And the second is also a no-op, identical counts.
    stats_2 = await portability.import_memories(
        envelope=envelope, preserve_owner=True, user=_root_user(),
    )
    assert stats_2.imported == 0
    assert stats_2.skipped == 1
    assert stats_2.sidecars_skipped == stats_1.sidecars_skipped


@pytest.mark.asyncio
async def test_charon_export_omits_sidecars_when_flag_off(fresh_db):
    """With include_sidecars=False (the default), the envelope must
    NOT carry the three sidecar arrays — back-compat with 0.1.0
    consumers that only know about kind=memory."""
    pool = fresh_db
    from mnemos.api.routes import portability

    await _seed(pool)

    envelope = await portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=SEED_OWNER, namespace=SEED_NAMESPACE,
        include_sidecars=False, user=_root_user(),
    )

    assert envelope.record_count == 1
    assert envelope.kg_triples is None
    assert envelope.memory_versions is None
    assert envelope.compression_manifest is None

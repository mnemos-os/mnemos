"""Tests for MORPHEUS slice 2: real cluster + synthesise phases.

Slice 1 shipped the run-row machinery and rollback contract. Slice 2
fills in phase_cluster (cosine grouping) and phase_synthesise
(per-cluster summary memories tagged with morpheus_run_id).

These tests cover:
  - The pure helpers (_cosine_similarity, _parse_pgvector,
    _majority, _first_sentence, _synthesise_cluster_summary
    extractive mode) — no DB needed.
  - phase_cluster against a mocked asyncpg.Pool — ordering,
    threshold, min_size filter, config persistence.
  - phase_synthesise against a mocked pool — INSERT shape,
    source_memories tagging, rollback safety contract.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from mnemos.domain.morpheus.runner import (
    _cosine_similarity,
    _first_sentence,
    _majority,
    _parse_pgvector,
    _synthesise_cluster_summary,
    phase_cluster,
    phase_synthesise,
)

# ── pure helper tests ────────────────────────────────────────────────────────

def test_cosine_similarity_identical_vectors():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert _cosine_similarity(a, a) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([-1.0, 0.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_returns_zero():
    """A degenerate zero embedding must not produce a NaN — clustering
    needs deterministic comparisons even when garbage data sneaks in."""
    a = np.array([0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 1.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == 0.0
    assert _cosine_similarity(b, a) == 0.0


def test_parse_pgvector_text_form():
    """asyncpg returns vector(N) as the literal pgvector text form
    "[0.1, 0.2, ...]" when the type is not registered."""
    result = _parse_pgvector("[0.1, 0.2, 0.3]")
    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float32
    np.testing.assert_array_almost_equal(result, [0.1, 0.2, 0.3])


def test_parse_pgvector_list_form():
    """If a future asyncpg type registration returns a list, that path
    must also work (callers don't care which form they get)."""
    result = _parse_pgvector([0.5, 0.5])
    np.testing.assert_array_almost_equal(result, [0.5, 0.5])


def test_parse_pgvector_null_returns_none():
    assert _parse_pgvector(None) is None


def test_parse_pgvector_garbage_returns_none():
    """Don't crash phase_cluster on a malformed embedding row — skip it."""
    assert _parse_pgvector("not-a-vector") is None


def test_majority_picks_most_common():
    assert _majority(["a", "b", "a", "c", "a"]) == "a"


def test_majority_breaks_ties_by_first_occurrence():
    """Two-way tie should prefer the first-seen value, so two runs over
    the same input produce the same cluster category."""
    assert _majority(["b", "a", "b", "a"]) == "b"


def test_majority_empty_returns_none():
    assert _majority([]) is None


def test_first_sentence_basic():
    assert _first_sentence("Hello world. Second sentence.") == "Hello world"


def test_first_sentence_no_terminator_truncates():
    long = "x" * 500
    assert _first_sentence(long) == "x" * 200


def test_first_sentence_empty():
    assert _first_sentence("") == ""


@pytest.mark.asyncio
async def test_synthesise_extractive_mode():
    """Default (no LLM) synthesis is deterministic and returns
    bullets of first sentences."""
    contents = [
        "First memory content. With a second sentence.",
        "Second memory has structure.",
        "Third memory.",
    ]
    summary = await _synthesise_cluster_summary(contents, use_llm=False)
    assert "MORPHEUS synthesis" in summary
    assert "First memory content" in summary
    assert "Second memory has structure" in summary
    # Bullets, one per member
    assert summary.count("•") == 3


@pytest.mark.asyncio
async def test_synthesise_extractive_handles_empty():
    assert (await _synthesise_cluster_summary([], use_llm=False)) == ""


# ── phase_cluster tests against mocked pool ─────────────────────────────────

class _MockConn:
    """Minimal asyncpg connection mock that records executed statements."""
    def __init__(self, fetchrow_result, fetch_result):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, *_args, **_kwargs):
        return self._fetchrow_result

    async def fetch(self, *_args, **_kwargs):
        return self._fetch_result

    async def fetchval(self, *_args, **_kwargs):
        # phase_synthesise reads config back; this is set explicitly per test.
        return getattr(self, "_fetchval_result", None)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "EXECUTE 1"


class _MockPool:
    """Minimal asyncpg.Pool mock returning a single _MockConn via acquire()."""
    def __init__(self, conn: _MockConn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool._conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _row(memory_id: str, vec: list[float]) -> dict[str, Any]:
    return {"id": memory_id, "embedding": json.dumps(vec)}


@pytest.mark.asyncio
async def test_phase_cluster_groups_similar_vectors():
    """Two near-identical vectors should land in one cluster; the third
    orthogonal vector should be its own (and dropped if min_size > 1)."""
    run_row = {
        "cluster_min_size": 2,
        "window_started_at": "2026-04-25T00:00:00",
        "window_ended_at": "2026-04-25T23:59:59",
        "namespace": None,
    }
    rows = [
        _row("mem_a", [1.0, 0.0, 0.0]),
        _row("mem_b", [0.99, 0.01, 0.0]),       # very close to mem_a
        _row("mem_c", [0.0, 1.0, 0.0]),         # orthogonal — its own cluster
    ]
    conn = _MockConn(fetchrow_result=run_row, fetch_result=rows)
    pool = _MockPool(conn)

    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000001")

    # min_size=2 filters out the singleton mem_c cluster.
    assert n == 1
    # The cluster payload should have been written via UPDATE.
    update_calls = [(s, a) for s, a in conn.executed if "UPDATE morpheus_runs" in s and "config" in s]
    assert len(update_calls) == 1
    payload = json.loads(update_calls[0][1][1])
    assert len(payload) == 1
    assert set(payload[0]["member_memory_ids"]) == {"mem_a", "mem_b"}


@pytest.mark.asyncio
async def test_phase_cluster_threshold_separation(monkeypatch):
    """A threshold raised above the actual similarity should split a
    cluster that would otherwise merge."""
    from mnemos.core import config

    with monkeypatch.context() as scoped:
        scoped.setenv("MNEMOS_MORPHEUS_CLUSTER_THRESHOLD", "0.999")
        config._reset_settings_for_tests()
        run_row = {
            "cluster_min_size": 1,
            "window_started_at": "2026-04-25T00:00:00",
            "window_ended_at": "2026-04-25T23:59:59",
            "namespace": None,
        }
        rows = [
            _row("mem_a", [1.0, 0.0]),
            _row("mem_b", [0.9, 0.4]),  # cosine ~0.91 — under 0.999
        ]
        conn = _MockConn(fetchrow_result=run_row, fetch_result=rows)
        pool = _MockPool(conn)

        n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000002")

    config._reset_settings_for_tests()

    # Both survive (min_size=1) but as separate clusters.
    assert n == 2


@pytest.mark.asyncio
async def test_phase_cluster_no_rows_zero_clusters():
    run_row = {
        "cluster_min_size": 3,
        "window_started_at": "2026-04-25T00:00:00",
        "window_ended_at": "2026-04-25T23:59:59",
        "namespace": None,
    }
    conn = _MockConn(fetchrow_result=run_row, fetch_result=[])
    pool = _MockPool(conn)

    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000003")

    assert n == 0


@pytest.mark.asyncio
async def test_phase_cluster_passes_namespace_to_query():
    """When the run has namespace set, phase_cluster should forward it
    as a query arg so the SQL filter scopes the scan to that tenant."""
    run_row = {
        "cluster_min_size": 1,
        "window_started_at": "2026-04-25T00:00:00",
        "window_ended_at": "2026-04-25T23:59:59",
        "namespace": "tenant-a",
    }

    captured: list = []

    class _Conn:
        async def fetchrow(self, *_args, **_kwargs):
            return run_row

        async def fetch(self, _sql, *args, **_kwargs):
            captured.append(args)
            return []

        async def execute(self, *_args, **_kwargs):
            return "OK"

    conn = _Conn()

    class _Pool:
        def acquire(self_inner):
            class _Ctx:
                async def __aenter__(self_ctx):
                    return conn
                async def __aexit__(self_ctx, *_exc):
                    return False
            return _Ctx()

    n = await phase_cluster(_Pool(), "00000000-0000-0000-0000-000000000005")
    assert n == 0
    # The fetch call should have received the namespace as one of its
    # bound parameters (the query arg list).
    assert captured, "phase_cluster did not call fetch"
    assert "tenant-a" in captured[0]


@pytest.mark.asyncio
async def test_phase_cluster_skips_garbage_embeddings():
    """A row with an unparseable embedding should be skipped, not crash
    the whole phase."""
    run_row = {
        "cluster_min_size": 1,
        "window_started_at": "2026-04-25T00:00:00",
        "window_ended_at": "2026-04-25T23:59:59",
        "namespace": None,
    }
    rows = [
        {"id": "mem_a", "embedding": "garbage-not-a-vector"},
        _row("mem_b", [1.0, 0.0]),
    ]
    conn = _MockConn(fetchrow_result=run_row, fetch_result=rows)
    pool = _MockPool(conn)

    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000004")

    # mem_a is silently dropped; mem_b alone forms one cluster (min_size=1).
    assert n == 1


# ── phase_synthesise tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_synthesise_inserts_one_per_cluster():
    """Two clusters in the run config → two INSERTs into memories,
    each tagged with morpheus_run_id, source_memories, provenance."""
    run_id = "00000000-0000-0000-0000-000000000010"
    config = {
        "clusters": [
            {"cluster_id": 0, "member_memory_ids": ["mem_1", "mem_2"]},
            {"cluster_id": 1, "member_memory_ids": ["mem_3", "mem_4"]},
        ]
    }

    member_rows_by_call = [
        [
            {"id": "mem_1", "content": "First fact about the deploy.", "category": "facts", "owner_id": "default", "namespace": "default"},
            {"id": "mem_2", "content": "Second fact, related to the first.", "category": "facts", "owner_id": "default", "namespace": "default"},
        ],
        [
            {"id": "mem_3", "content": "Decision was made on Tuesday.", "category": "decisions", "owner_id": "default", "namespace": "default"},
            {"id": "mem_4", "content": "Decision rationale captured.", "category": "decisions", "owner_id": "default", "namespace": "default"},
        ],
    ]

    fetch_calls = {"i": 0}

    class _Conn:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        async def fetchval(self, *_args, **_kwargs):
            return config

        async def fetch(self, *_args, **_kwargs):
            i = fetch_calls["i"]
            fetch_calls["i"] += 1
            return member_rows_by_call[i]

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "INSERT 0 1"

    conn = _Conn()
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)

    assert n == 2
    inserts = [(s, a) for s, a in conn.executed if "INSERT INTO memories" in s]
    assert len(inserts) == 2
    # Every insert carries the morpheus_run_id and source_memories.
    for sql, args in inserts:
        assert "morpheus_run_id" in sql
        assert "source_memories" in sql
        assert "'morpheus_local'" in sql
        # args[7] is run_id (1-indexed: $1=id, $2=summary, $3=category,
        # $4=subcat, $5=metadata, $6=owner, $7=ns, $8=run_id, $9=source_memories)
        assert args[7] == run_id


@pytest.mark.asyncio
async def test_phase_synthesise_no_clusters_zero():
    run_id = "00000000-0000-0000-0000-000000000011"
    conn = _MockConn(fetchrow_result=None, fetch_result=[])
    conn._fetchval_result = {"clusters": []}
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)
    assert n == 0


@pytest.mark.asyncio
async def test_phase_synthesise_no_config_zero():
    run_id = "00000000-0000-0000-0000-000000000012"
    conn = _MockConn(fetchrow_result=None, fetch_result=[])
    conn._fetchval_result = None
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)
    assert n == 0


@pytest.mark.asyncio
async def test_phase_synthesise_inherits_majority_category():
    """When a cluster has 3 members across two categories, the new
    summary memory inherits the majority category. Tie-breaking is
    first-occurrence so behavior is reproducible across runs."""
    run_id = "00000000-0000-0000-0000-000000000013"
    config = {
        "clusters": [
            {"cluster_id": 0, "member_memory_ids": ["m1", "m2", "m3"]},
        ]
    }

    members = [
        {"id": "m1", "content": "x.", "category": "facts", "owner_id": "default", "namespace": "default"},
        {"id": "m2", "content": "y.", "category": "decisions", "owner_id": "default", "namespace": "default"},
        {"id": "m3", "content": "z.", "category": "decisions", "owner_id": "default", "namespace": "default"},
    ]

    class _Conn:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        async def fetchval(self, *_args, **_kwargs):
            return config

        async def fetch(self, *_args, **_kwargs):
            return members

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "INSERT 0 1"

    conn = _Conn()
    pool = _MockPool(conn)

    await phase_synthesise(pool, run_id)
    insert = next(((s, a) for s, a in conn.executed if "INSERT INTO memories" in s), None)
    assert insert is not None
    # args[2] is category in the INSERT VALUES order.
    assert insert[1][2] == "decisions"

"""Tests for the MORPHEUS cluster-introspection endpoint.

Slice 2's phase_cluster persists cluster groupings into
morpheus_runs.config["clusters"]. The /v1/morpheus/runs/{id}/clusters
endpoint reads that JSONB payload back and joins it with the
synthesised memories so each cluster's summary id is visible.

These tests call the handler function directly with a mock pool —
same pattern as test_morpheus_slice2.py — to avoid the FakePool
substring-match fragility for new query shapes.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.routes.morpheus import list_clusters
from mnemos.persistence.postgres import PostgresBackend
from types import SimpleNamespace


def _user() -> UserContext:
    return UserContext(
        user_id="u_test",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


class _Conn:
    def __init__(
        self,
        *,
        config: Any = None,
        run_exists: bool = True,
        synth_rows: list | None = None,
    ):
        self._config = config
        self._run_exists = run_exists
        self._synth_rows = synth_rows or []
        self.queries: list = []

    async def fetchval(self, sql: str, *args):
        self.queries.append((sql, args))
        if "SELECT config FROM morpheus_runs" in sql:
            return self._config
        if "SELECT 1 FROM morpheus_runs" in sql:
            return 1 if self._run_exists else None
        return None

    async def fetch(self, sql: str, *args):
        self.queries.append((sql, args))
        if "morpheus_run_id" in sql:
            return self._synth_rows
        return []


class _Pool:
    def __init__(self, conn: _Conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool._conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


@pytest.fixture
def stub_pool(monkeypatch):
    """Install a fake pool on mnemos.core.lifecycle._pool for the duration of the test."""
    holder = {}

    def install(conn: _Conn):
        pool = _Pool(conn)
        monkeypatch.setattr(_lc, "_pool", pool)
        monkeypatch.setattr(_lc, "_pool_manager", None)
        monkeypatch.setattr(_lc, "_persistence_backend", PostgresBackend(pool, SimpleNamespace()))
        holder["conn"] = conn
        holder["pool"] = pool
        return pool

    return install


# ── happy paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_clusters_returns_payload_with_synthesised_id(stub_pool):
    """A run with two clusters and matching synthesised memories should
    return both clusters with their summary ids attached."""
    config = {
        "clusters": [
            {"cluster_id": 0, "member_memory_ids": ["mem_a", "mem_b"]},
            {"cluster_id": 1, "member_memory_ids": ["mem_c", "mem_d"]},
        ]
    }
    synth = [
        {"id": "mem_summary_0", "source_memories": ["mem_a", "mem_b"]},
        {"id": "mem_summary_1", "source_memories": ["mem_d", "mem_c"]},  # order shuffled
    ]
    stub_pool(_Conn(config=config, synth_rows=synth))

    result = await list_clusters("00000000-0000-0000-0000-000000000010", _user())
    assert result.run_id == "00000000-0000-0000-0000-000000000010"
    assert result.count == 2
    assert result.clusters[0].cluster_id == 0
    assert result.clusters[0].member_count == 2
    assert result.clusters[0].synthesised_memory_id == "mem_summary_0"
    # Cluster 1 has shuffled source_memories on the synthesised side; the
    # join should still find it via sorted-tuple match.
    assert result.clusters[1].synthesised_memory_id == "mem_summary_1"


@pytest.mark.asyncio
async def test_list_clusters_handles_no_synthesised_memory(stub_pool):
    """phase_cluster ran but phase_synthesise didn't — clusters return
    with synthesised_memory_id=None, not a crash."""
    config = {"clusters": [{"cluster_id": 0, "member_memory_ids": ["mem_x"]}]}
    stub_pool(_Conn(config=config, synth_rows=[]))

    result = await list_clusters("00000000-0000-0000-0000-000000000020", _user())
    assert result.count == 1
    assert result.clusters[0].synthesised_memory_id is None


@pytest.mark.asyncio
async def test_list_clusters_empty_when_no_clusters_key(stub_pool):
    """A run that completed without producing any clusters returns
    count=0, not 404 (the run exists; it just has nothing to show)."""
    stub_pool(_Conn(config={}))

    result = await list_clusters("00000000-0000-0000-0000-000000000030", _user())
    assert result.count == 0
    assert result.clusters == []


# ── error paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_clusters_404_when_run_does_not_exist(stub_pool):
    """If config is null AND the run row doesn't exist, return 404."""
    stub_pool(_Conn(config=None, run_exists=False))

    with pytest.raises(HTTPException) as exc_info:
        await list_clusters("00000000-0000-0000-0000-000000000099", _user())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_clusters_returns_empty_when_run_exists_but_config_is_null(stub_pool):
    """A row that exists but has no config (shouldn't happen given the
    DEFAULT '{}' but defend against schema drift) returns empty, not 404."""
    stub_pool(_Conn(config=None, run_exists=True))
    result = await list_clusters("00000000-0000-0000-0000-000000000040", _user())
    assert result.count == 0


@pytest.mark.asyncio
async def test_list_clusters_503_when_pool_missing(monkeypatch):
    """If the DB pool isn't initialised, return 503 (mirrors other
    handlers in the file)."""
    monkeypatch.setattr(_lc, "_pool", None)
    with pytest.raises(HTTPException) as exc_info:
        await list_clusters("00000000-0000-0000-0000-000000000050", _user())
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_list_clusters_handles_malformed_cluster_entries(stub_pool):
    """A cluster missing cluster_id or member_memory_ids should not
    crash the endpoint — it gets defaults."""
    config = {
        "clusters": [
            {"member_memory_ids": ["mem_a"]},  # no cluster_id
            {"cluster_id": 5},                  # no members
            {},                                 # both missing
        ]
    }
    stub_pool(_Conn(config=config))

    result = await list_clusters("00000000-0000-0000-0000-000000000060", _user())
    assert result.count == 3
    assert result.clusters[0].member_count == 1
    assert result.clusters[1].member_count == 0
    assert result.clusters[1].cluster_id == 5
    assert result.clusters[2].member_count == 0

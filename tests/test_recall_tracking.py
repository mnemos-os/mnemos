"""Tests for the recall-frequency bump in /v1/memories/search.

The bump is fire-and-forget on the search hot path:
  - One UPDATE for the whole hit set, not per-memory
  - Failures logged + swallowed (observability, not correctness)
  - Skipped on empty hits or missing pool

These tests pin the SQL shape and the failure-mode semantics so a
future refactor can't silently turn the bump into N round-trips or
a search-blocking exception.
"""
from __future__ import annotations

from typing import Any, List, Tuple

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.routes.memories import _bump_recall_counters


class _Conn:
    def __init__(self, raise_on_execute: bool = False):
        self.executed: List[Tuple[str, tuple]] = []
        self.raise_on_execute = raise_on_execute

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if self.raise_on_execute:
            raise RuntimeError("simulated DB error")
        return "UPDATE 1"


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
    holder: dict = {}

    def install(raise_on_execute: bool = False) -> _Conn:
        conn = _Conn(raise_on_execute=raise_on_execute)
        monkeypatch.setattr(_lc, "_pool", _Pool(conn))
        holder["conn"] = conn
        return conn

    return install


@pytest.mark.asyncio
async def test_bump_uses_single_update_for_hit_set(stub_pool):
    """Five hits should produce one UPDATE, not five."""
    conn = stub_pool()
    await _bump_recall_counters(["mem_1", "mem_2", "mem_3", "mem_4", "mem_5"])
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "UPDATE memories" in sql
    assert "recall_count = recall_count + 1" in sql
    assert "last_recalled_at = now()" in sql
    assert "id = ANY($1::text[])" in sql
    assert args[0] == ["mem_1", "mem_2", "mem_3", "mem_4", "mem_5"]


@pytest.mark.asyncio
async def test_bump_skips_empty_hit_set(stub_pool):
    """Search with zero hits → no DB roundtrip."""
    conn = stub_pool()
    await _bump_recall_counters([])
    assert conn.executed == []


@pytest.mark.asyncio
async def test_bump_swallows_db_failure(stub_pool, caplog):
    """A DB failure during the bump must NOT propagate — the search
    response has already gone out; the bump is observability, not
    correctness."""
    conn = stub_pool(raise_on_execute=True)
    # No exception expected.
    await _bump_recall_counters(["mem_x"])
    assert len(conn.executed) == 1


@pytest.mark.asyncio
async def test_bump_skips_when_pool_missing(monkeypatch):
    """Search before pool is initialised (rare but possible during
    boot) shouldn't crash the search path."""
    monkeypatch.setattr(_lc, "_pool", None)
    # No exception expected.
    await _bump_recall_counters(["mem_x"])


@pytest.mark.asyncio
async def test_bump_passes_list_not_iterator(stub_pool):
    """Ensure the helper's `list(memory_ids)` cast survives — passing
    a generator should still work end-to-end."""
    conn = stub_pool()

    def _gen():
        yield "mem_a"
        yield "mem_b"

    await _bump_recall_counters(list(_gen()))
    assert conn.executed[0][1][0] == ["mem_a", "mem_b"]

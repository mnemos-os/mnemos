"""Coverage for optional mnemos_hot acceleration in MORPHEUS clustering.

The MORPHEUS runner defaults to its NumPy per-pair cosine helper. When
MNEMOS_HOT_RS_ENABLED=1 and mnemos_hot is importable, the row-vs-cluster
scoring step dispatches through cosine_batch. These tests use a fake
mnemos_hot module so they pin the opt-in dispatch without requiring a
local Rust wheel.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types
from typing import Any

import numpy as np
import pytest


def _reload_runner_module(monkeypatch, *, hot_enabled: bool, hot_module=None):
    import mnemos.domain.morpheus.runner as _orig

    if hot_enabled:
        monkeypatch.setenv("MNEMOS_HOT_RS_ENABLED", "1")
    else:
        monkeypatch.delenv("MNEMOS_HOT_RS_ENABLED", raising=False)

    if hot_module is None:
        sys.modules.pop("mnemos_hot", None)
    else:
        sys.modules["mnemos_hot"] = hot_module

    return importlib.reload(_orig)


def test_default_branch_uses_python_batch_fallback(monkeypatch):
    runner_mod = _reload_runner_module(monkeypatch, hot_enabled=False)
    assert runner_mod._HOT_RS is None

    query = np.array([1.0, 0.0], dtype=np.float32)
    candidates = [
        np.array([1.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0], dtype=np.float32),
        np.array([-1.0, 0.0], dtype=np.float32),
    ]

    scores = runner_mod._cosine_similarities(query, candidates)

    assert scores == pytest.approx([1.0, 0.0, -1.0])


def test_optin_uses_rust_cosine_batch(monkeypatch):
    calls = []

    def cosine_batch(query, candidates):
        calls.append((query, candidates))
        return [0.25, 0.75]

    fake = types.SimpleNamespace(__version__="fake-0", cosine_batch=cosine_batch)
    runner_mod = _reload_runner_module(monkeypatch, hot_enabled=True, hot_module=fake)

    query = np.array([1.0, 0.0], dtype=np.float32)
    candidates = [
        np.array([1.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0], dtype=np.float32),
    ]

    scores = runner_mod._cosine_similarities(query, candidates)

    assert scores == [0.25, 0.75]
    assert calls == [([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])]


def test_optin_normalizes_batch_before_rust_cosine(monkeypatch):
    normalize_calls = []
    cosine_calls = []

    def normalize_embeddings(vectors):
        normalize_calls.append(vectors)
        return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]

    def cosine_batch(query, candidates):
        cosine_calls.append((query, candidates))
        return [1.0, 0.0]

    fake = types.SimpleNamespace(
        __version__="fake-0",
        normalize_embeddings=normalize_embeddings,
        cosine_batch=cosine_batch,
    )
    runner_mod = _reload_runner_module(monkeypatch, hot_enabled=True, hot_module=fake)

    query = np.array([3.0, 4.0], dtype=np.float32)
    candidates = [
        np.array([6.0, 8.0], dtype=np.float32),
        np.array([0.0, 5.0], dtype=np.float32),
    ]

    scores = runner_mod._cosine_similarities(query, candidates)

    assert scores == [1.0, 0.0]
    assert normalize_calls == [[[3.0, 4.0], [6.0, 8.0], [0.0, 5.0]]]
    assert cosine_calls == [([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])]


def test_optin_falls_back_when_rust_batch_raises(monkeypatch):
    def cosine_batch(_query, _candidates):
        raise RuntimeError("synthetic rust failure")

    fake = types.SimpleNamespace(__version__="fake-0", cosine_batch=cosine_batch)
    runner_mod = _reload_runner_module(monkeypatch, hot_enabled=True, hot_module=fake)

    query = np.array([1.0, 0.0], dtype=np.float32)
    candidates = [
        np.array([1.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0], dtype=np.float32),
    ]

    scores = runner_mod._cosine_similarities(query, candidates)

    assert scores == pytest.approx([1.0, 0.0])


class _MockConn:
    def __init__(self, fetchrow_result, fetch_result):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, *_args, **_kwargs):
        return self._fetchrow_result

    async def fetch(self, *_args, **_kwargs):
        return self._fetch_result

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "EXECUTE 1"


class _MockPool:
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
async def test_phase_cluster_dispatches_row_vs_cluster_scoring_to_rust_batch(monkeypatch):
    calls = []

    def cosine_batch(query, candidates):
        calls.append((query, candidates))
        return [1.0]

    fake = types.SimpleNamespace(__version__="fake-0", cosine_batch=cosine_batch)
    runner_mod = _reload_runner_module(monkeypatch, hot_enabled=True, hot_module=fake)

    run_row = {
        "cluster_min_size": 2,
        "window_started_at": "2026-04-25T00:00:00",
        "window_ended_at": "2026-04-25T23:59:59",
        "namespace": None,
    }
    rows = [
        _row("mem_a", [1.0, 0.0]),
        _row("mem_b", [1.0, 0.0]),
    ]
    conn = _MockConn(fetchrow_result=run_row, fetch_result=rows)

    n = await runner_mod.phase_cluster(
        _MockPool(conn),
        "00000000-0000-0000-0000-000000000084",
    )

    assert n == 1
    assert calls == [([1.0, 0.0], [[1.0, 0.0]])]


def teardown_module(_):
    import mnemos.domain.morpheus.runner as _orig

    os.environ.pop("MNEMOS_HOT_RS_ENABLED", None)
    sys.modules.pop("mnemos_hot", None)
    importlib.reload(_orig)

"""Custom Query mode on /graeae/consult — selector resolution (v3.2).

Direct tests on the `_resolve_selection` + `_tier_lineup` +
`_resolve_models` helpers in api/handlers/consultations, plus a
unit test that consult() respects a supplied selection dict.

Does not exercise the full /v1/consultations HTTP path — the audit-
log + transaction machinery is out of scope here; the prior commits
already pin that. These tests pin the Custom Query RESOLUTION
layer only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mnemos.api.routes import consultations


class _Conn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self._rows


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


class _FakeEngine:
    """Minimal stand-in for GraeaeEngine — we only need `.providers`."""

    def __init__(self, providers):
        self.providers = {name: {"model": f"{name}-default"} for name in providers}


# ─── _resolve_selection: precedence + mutual exclusion ──────────────────────


def test_resolve_no_selectors_returns_none(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai", "claude"])

    result = asyncio.run(consultations._resolve_selection(
        engine=engine, models=None, providers=None, tier=None,
    ))
    assert result is None  # auto lineup


def test_resolve_multiple_selectors_rejected(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai"])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consultations._resolve_selection(
            engine=engine, models=["gpt-5"], providers=["openai"], tier=None,
        ))
    assert exc.value.status_code == 400
    assert "at most one" in exc.value.detail


# ─── _resolve_selection: providers ──────────────────────────────────────────


def test_resolve_providers_maps_to_null_overrides(monkeypatch):
    """`providers=[...]` means "use these providers with their default
    models" — returns {provider: None} so the engine keeps its
    per-provider default."""
    conn = _Conn()
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai", "claude", "groq"])

    # Caller passes registry-side name "anthropic"; handler normalises
    # to the GRAEAE engine key "claude" so consult()'s filter matches.
    result = asyncio.run(consultations._resolve_selection(
        engine=engine, models=None, providers=["openai", "anthropic"], tier=None,
    ))
    assert result == {"openai": None, "claude": None}


def test_resolve_providers_unknown_raises_400(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai"])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consultations._resolve_selection(
            engine=engine, models=None, providers=["openai", "nonexistent"], tier=None,
        ))
    assert exc.value.status_code == 400
    assert "nonexistent" in exc.value.detail


# ─── _resolve_selection: models ─────────────────────────────────────────────


def test_resolve_models_looks_up_provider_per_model(monkeypatch):
    """Each model_id resolves to its provider via model_registry.
    Return shape: {provider_name: model_id}."""
    conn = _Conn(rows=[
        {"provider": "openai",    "model_id": "gpt-5.2-chat-latest"},
        {"provider": "anthropic", "model_id": "claude-opus-4-6"},
    ])
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai", "claude"])

    result = asyncio.run(consultations._resolve_selection(
        engine=engine,
        models=["gpt-5.2-chat-latest", "claude-opus-4-6"],
        providers=None, tier=None,
    ))
    assert result == {
        "openai": "gpt-5.2-chat-latest",
        "claude": "claude-opus-4-6",
    }


def test_resolve_models_unknown_raises_400(monkeypatch):
    """Partial resolution is NOT allowed — if any requested model is
    missing from the registry, reject the whole call."""
    conn = _Conn(rows=[
        {"provider": "openai", "model_id": "gpt-5.2-chat-latest"},
    ])
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai"])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consultations._resolve_selection(
            engine=engine,
            models=["gpt-5.2-chat-latest", "nonexistent-model-9000"],
            providers=None, tier=None,
        ))
    assert exc.value.status_code == 400
    assert "nonexistent-model-9000" in exc.value.detail


# ─── _resolve_selection: tier ───────────────────────────────────────────────


def test_resolve_tier_frontier_returns_registry_slice(monkeypatch):
    conn = _Conn(rows=[
        {"provider": "openai",    "model_id": "gpt-5.2-chat-latest"},
        {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        {"provider": "gemini",    "model_id": "gemini-3-pro-preview"},
    ])
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai", "claude", "gemini"])

    result = asyncio.run(consultations._resolve_selection(
        engine=engine, models=None, providers=None, tier="frontier",
    ))
    assert result == {
        "openai": "gpt-5.2-chat-latest",
        "claude": "claude-opus-4-6",
        "gemini": "gemini-3-pro-preview",
    }
    # Verify the SQL scoped to frontier criteria (arena_rank <= 5 OR graeae_weight >= 0.95)
    sql = conn.fetch_calls[0][0]
    assert "arena_rank" in sql
    assert "graeae_weight >= 0.95" in sql


def test_resolve_tier_budget_uses_cost_order(monkeypatch):
    conn = _Conn(rows=[
        {"provider": "groq", "model_id": "llama-3.3-70b-versatile"},
    ])
    _install(monkeypatch, conn)
    engine = _FakeEngine(["groq"])

    asyncio.run(consultations._resolve_selection(
        engine=engine, models=None, providers=None, tier="budget",
    ))
    sql = conn.fetch_calls[0][0]
    assert "ORDER BY" in sql
    assert "input_cost_per_mtok" in sql


def test_resolve_tier_unknown_raises_400(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai"])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consultations._resolve_selection(
            engine=engine, models=None, providers=None, tier="enterprise",
        ))
    assert exc.value.status_code == 400
    assert "unknown tier" in exc.value.detail


def test_resolve_tier_empty_registry_raises_404(monkeypatch):
    """If the registry has no rows matching the tier criteria, hard-
    fail rather than silently fall back to auto. The caller asked for
    a specific tier and wants to know it was satisfiable."""
    conn = _Conn(rows=[])  # empty registry
    _install(monkeypatch, conn)
    engine = _FakeEngine(["openai"])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consultations._resolve_selection(
            engine=engine, models=None, providers=None, tier="frontier",
        ))
    assert exc.value.status_code == 404
    assert "frontier" in exc.value.detail


# ─── Engine behavior under selection ────────────────────────────────────────


def test_cache_tag_differs_by_selection():
    """Two different selections must produce distinct cache keys so
    a Custom Query doesn't return a cached all-providers result."""
    from mnemos.domain.graeae.engine import _selection_cache_tag

    assert _selection_cache_tag(None) == ""
    assert _selection_cache_tag({}) == ""
    tag_a = _selection_cache_tag({"openai": None, "anthropic": None})
    tag_b = _selection_cache_tag({"openai": "gpt-5.2-chat-latest"})
    tag_c = _selection_cache_tag({"openai": None})
    assert tag_a != tag_b
    assert tag_a != tag_c
    assert tag_b != tag_c
    # Deterministic (key order doesn't affect output)
    tag_d = _selection_cache_tag({"anthropic": None, "openai": None})
    assert tag_a == tag_d

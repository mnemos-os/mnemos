"""/v1/models registry-backed (v3.1.2 Tier 3).

Pins the new behavior: /v1/models queries model_registry instead of
returning a hardcoded list. Discovery is registry-only: chat routing
can still use heuristics, but model listing and lookup do not synthesize
metadata for unregistered models.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext
from api.handlers import openai_compat


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, *, rows=None, row_for_get=None, raise_on_query=False):
        self._rows = rows or []
        self._row_for_get = row_for_get
        self._raise = raise_on_query
        self.fetch_calls = 0
        self.fetchrow_calls = 0

    async def fetch(self, sql: str, *args):
        self.fetch_calls += 1
        if self._raise:
            raise RuntimeError("db exploded")
        return self._rows

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls += 1
        if self._raise:
            raise RuntimeError("db exploded")
        return self._row_for_get


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _install_no_pool(monkeypatch):
    """Simulate a pre-lifespan state where _pool is None."""
    import api.lifecycle as lc
    monkeypatch.setattr(lc, "_pool", None)


# ---- /v1/models ------------------------------------------------------------


def test_list_models_returns_registry_rows(monkeypatch):
    rows = [
        {"provider": "openai",    "model_id": "gpt-5",                 "display_name": "GPT-5"},
        {"provider": "anthropic", "model_id": "claude-4.5-sonnet",     "display_name": "Claude 4.5 Sonnet"},
        {"provider": "gemini",    "model_id": "gemini-2.5-pro",        "display_name": "Gemini 2.5 Pro"},
    ]
    _install(monkeypatch, _Conn(rows=rows))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    ids = [m.id for m in resp.data]
    owners = {m.id: m.owned_by for m in resp.data}

    assert ids == ["gpt-5", "claude-4.5-sonnet", "gemini-2.5-pro"]
    assert owners["gpt-5"] == "OpenAI"
    assert owners["claude-4.5-sonnet"] == "Anthropic"
    assert owners["gemini-2.5-pro"] == "Google"


def test_list_models_empty_registry_returns_empty(monkeypatch):
    _install(monkeypatch, _Conn(rows=[]))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_db_failure_returns_empty_without_synthesizing(monkeypatch):
    _install(monkeypatch, _Conn(raise_on_query=True))

    # Must not synthesize unregistered models through transient DB blips.
    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_no_pool_returns_empty(monkeypatch):
    _install_no_pool(monkeypatch)

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_unknown_provider_capitalized(monkeypatch):
    """An unmapped provider key (say an operator added 'cohere' locally)
    should still produce a reasonable owned_by string."""
    rows = [{"provider": "cohere", "model_id": "command-r+", "display_name": None}]
    _install(monkeypatch, _Conn(rows=rows))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data[0].owned_by == "Cohere"


# ---- /v1/models/{model_id} -------------------------------------------------


def test_get_model_hits_registry(monkeypatch):
    _install(monkeypatch, _Conn(row_for_get={"provider": "openai"}))

    result = asyncio.run(openai_compat.get_model(
        "gpt-5", authorization=None, user=_user(),
    ))
    assert result.id == "gpt-5"
    assert result.owned_by == "OpenAI"


def test_get_model_alias_resolved_before_lookup(monkeypatch):
    """An alias like 'best-coding' should resolve to its concrete model
    first, then that concrete id is what hits the registry."""
    captured_args: list = []

    class _C(_Conn):
        async def fetchrow(self, sql, *args):
            captured_args.append(args)
            return {"provider": "openai"}

    _install(monkeypatch, _C(row_for_get=None))

    # Use a known alias from MODEL_ALIASES
    alias = next(iter(openai_compat.MODEL_ALIASES))
    concrete = openai_compat.MODEL_ALIASES[alias]

    asyncio.run(openai_compat.get_model(
        alias, authorization=None, user=_user(),
    ))
    # The registry query should have been passed the RESOLVED id, not the alias
    assert captured_args, "expected a fetchrow call"
    assert captured_args[0] == (concrete,)


def test_get_model_unknown_returns_404(monkeypatch):
    """A model not in the registry returns 404. Chat routing may still
    use provider heuristics, but discovery must not overstate availability."""
    _install(monkeypatch, _Conn(row_for_get=None))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.get_model(
            "my-local-model", authorization=None, user=_user(),
        ))
    assert exc.value.status_code == 404
    assert exc.value.detail == "model not found"

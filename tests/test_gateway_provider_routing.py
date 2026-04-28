"""Gateway provider resolution via model_registry (v3.2).

Codex memory-OS audit 019dbd11 flagged: "gateway/provider selection
is still heuristic and brittle: substring routing plus a default-to-
Groq fallback." The v3.2 fix queries model_registry first and falls
back to substring heuristics only when the registry has no row.
Unknown models are now rejected with 400 instead of silently
routing to Groq.

Tests:
  - registry hit -> provider comes from DB row
  - registry miss with a known substring -> fallback heuristic
  - registry miss AND no substring match -> 400 (was silent groq)
  - DB query failure -> falls through to heuristic (no 500)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, *, row=None, raise_on_query=False):
        self._row = row
        self._raise = raise_on_query
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if self._raise:
            raise RuntimeError("db blip")
        return self._row


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
    import api.lifecycle as lc
    monkeypatch.setattr(lc, "_pool", None)


# ─── _resolve_provider_for_model ────────────────────────────────────────────


def test_resolve_uses_registry_row_when_present(monkeypatch):
    """Registry stores Anthropic models under provider='anthropic'
    (the Anthropic-side name). engine.providers indexes them under
    'claude' (the GRAEAE-side name). The resolver normalizes back to
    the GRAEAE name so engine.route() can find the dispatch entry —
    a verbatim 'anthropic' return would 503 with 'provider not
    registered'."""
    from api.handlers import openai_compat

    conn = _Conn(row={"provider": "anthropic"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "claude"


def test_resolve_normalises_registry_name_only_when_mapped(monkeypatch):
    """An operator-registered custom provider (not in _REGISTRY_MAP) must
    pass through the resolver verbatim. Locks in the v3.2 contract that
    registry is the source of truth for provider routing — the new
    GRAEAE-normalization step is additive, not a rename pass."""
    from api.handlers import openai_compat

    conn = _Conn(row={"provider": "my-local-anthropic-proxy"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "my-local-anthropic-proxy"


def test_resolve_handles_gateway_namespaced_slash_id(monkeypatch):
    """`<provider>/<bare_api_id>` is the gateway's namespacing convention.
    Many upstream APIs have slash-bearing IDs natively (NVIDIA's
    `meta/llama-3.3-70b-instruct`, Together's `Qwen/Qwen3-…`); the
    resolver must split on the FIRST slash and look up the tail with
    the head as a provider filter so the gateway form resolves the same
    way as the bare upstream form."""
    from api.handlers import openai_compat

    # First fetchrow (direct lookup of full 'together/Qwen/...') misses;
    # second fetchrow (provider='together', model_id='Qwen/Qwen3-…') hits.
    class _TwoStep:
        def __init__(self):
            self.calls = 0
            self.fetchrow_calls = []
        async def fetchrow(self, sql, *args):
            self.calls += 1
            self.fetchrow_calls.append((sql, args))
            if self.calls == 1:
                return None
            return {"provider": "together"}

    conn = _TwoStep()
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model(
        "together/Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    ))
    assert result == "together"
    # Confirm the namespaced lookup actually fired with the split args.
    assert conn.calls == 2
    second_args = conn.fetchrow_calls[1][1]
    assert second_args[0] == "together"
    assert second_args[1] == "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"


def test_resolve_handles_registry_name_in_namespaced_head(monkeypatch):
    """Callers may use either GRAEAE name (`claude/`) or registry name
    (`anthropic/`) as the namespace head. The namespaced lookup must
    accept either by re-mapping the head through _REGISTRY_MAP for the
    WHERE clause and normalising the final answer back to the GRAEAE
    name for the caller."""
    from api.handlers import openai_compat

    class _TwoStep:
        def __init__(self):
            self.calls = 0
            self.fetchrow_calls = []
        async def fetchrow(self, sql, *args):
            self.calls += 1
            self.fetchrow_calls.append((sql, args))
            if self.calls == 1:
                return None
            # Registry stores it as provider='anthropic'.
            return {"provider": "anthropic"}

    conn = _TwoStep()
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model(
        "anthropic/claude-opus-4-7"
    ))
    # GRAEAE-normalised: 'anthropic' → 'claude'.
    assert result == "claude"
    # Namespaced lookup fired with head re-mapped to its registry name
    # (in this case identity — 'anthropic' isn't a GRAEAE provider key
    # and so passes through unchanged for the WHERE clause).
    assert conn.calls == 2
    second_args = conn.fetchrow_calls[1][1]
    assert second_args[0] == "anthropic"
    assert second_args[1] == "claude-opus-4-7"


def test_resolve_falls_back_to_heuristic_on_registry_miss(monkeypatch):
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    # "claude-*" is in _FALLBACK_PROVIDER_MAP
    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-custom-local"))
    assert result == "claude"


def test_resolve_returns_none_on_complete_miss(monkeypatch):
    """Registry miss + no substring match -> None. Caller raises 400."""
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("unknown-xyz-v9"))
    assert result is None


def test_resolve_falls_back_when_db_query_raises(monkeypatch):
    """Transient DB failures must NOT 500 the gateway. Fall through
    to substring heuristic."""
    from api.handlers import openai_compat

    conn = _Conn(raise_on_query=True)
    _install(monkeypatch, conn)

    # gpt-* hits the fallback heuristic
    result = asyncio.run(openai_compat._resolve_provider_for_model("gpt-5-mini"))
    assert result == "openai"


def test_resolve_falls_back_when_pool_missing(monkeypatch):
    """Pre-lifespan state: _pool is None. Still fall through to
    heuristic so /v1/chat/completions stays usable during startup
    or in degenerate environments."""
    from api.handlers import openai_compat

    _install_no_pool(monkeypatch)
    result = asyncio.run(openai_compat._resolve_provider_for_model("llama-3.3-70b-versatile"))
    assert result == "groq"


# ─── _route_to_provider ─────────────────────────────────────────────────────


def test_route_unknown_model_raises_404(monkeypatch):
    """Unknown model_id is an explicit OpenAI-style 404, NOT a silent
    route to groq. Operators see the failure at the edge instead of
    getting garbage responses from the wrong provider."""
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat._route_to_provider(
            model="definitely-not-a-real-model-9999",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.7, max_tokens=100,
            user=_user(),
        ))
    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "model_not_found"


def test_route_uses_registry_hit_over_heuristic(monkeypatch):
    """If the registry says model X belongs to provider A but the
    substring heuristic would say B, the registry wins. Allows
    operators to re-map providers without editing code."""
    from api.handlers import openai_compat

    # Registry says "claude-*" is served by a custom provider
    # registered as "my-local-anthropic-proxy". The substring
    # heuristic would say "claude". Registry wins.
    conn = _Conn(row={"provider": "my-local-anthropic-proxy"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "my-local-anthropic-proxy"

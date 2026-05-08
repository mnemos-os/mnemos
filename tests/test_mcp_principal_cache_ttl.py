"""Slice #203: pin TTL + size bound on the MCP principal context cache.

Audit MED finding (mem_1778221719390_8cb1ba) at
``mnemos/mcp/http.py``: the ``_principal_context_cache`` was an
unbounded ``dict`` that NEVER expired. Operator role / namespace
changes were hidden for the lifetime of the process, and a high-
churn principal-id flow (token rotation, CI matrix) could grow
the cache without bound.

Fix: TTL=300s + size cap=1024. Bare-context entries
(test direct-assignment shape) treated as never-expires for
backward compatibility with the existing tests under
``tests/test_mcp_user_passthrough.py`` and
``tests/test_mcp_nats_sse.py`` that assign
``cache[key] = MCPUserContext(...)`` directly.

This test pins:
1. Constants exist with sane values.
2. Set + get round-trips return the cached context within TTL.
3. Past-TTL lookups evict and return None.
4. Beyond-cap insertion evicts oldest entries (cap holds).
5. Bare-context backward compat still returns the entry on get.
"""
from __future__ import annotations

import os

import pytest

# The mnemos.mcp.http module has an import-time guard that
# `sys.exit(2)`s when neither `MNEMOS_MCP_TOKEN` nor
# `MNEMOS_MCP_TOKENS` is set. Existing tests under
# `tests/test_mcp_user_passthrough.py` and `tests/test_mcp_nats_sse.py`
# satisfy the guard with a `monkeypatch.setenv` fixture, but that
# only fires after import. Set it at module-collection time so this
# file can run in isolation as well as part of the full sweep.
os.environ.setdefault("MNEMOS_MCP_TOKEN",
                      "test-token-203-cache-ttl")


@pytest.fixture
def http():
    """Reset the module-global cache between tests so isolation is
    test-by-test. We can't `importlib.reload` the module — its
    import-time `MNEMOS_MCP_TOKEN` guard `sys.exit(2)`s in the
    test env. Just clear the cache dict and yield the live module.
    """
    from mnemos.mcp import http as mod
    mod._principal_context_cache.clear()
    yield mod
    mod._principal_context_cache.clear()


def test_constants_have_sane_values(http):
    """The TTL and cap should be present and operator-reasonable.
    A future tightening can lower TTL; widening past 1h would be
    the wrong direction (defeats the whole point — role changes
    must propagate within minutes)."""
    assert http._PRINCIPAL_CACHE_TTL_SECONDS > 0
    assert http._PRINCIPAL_CACHE_TTL_SECONDS <= 3600, (
        "MCP principal cache TTL widened past 1h — that defeats "
        "the gap-close from #203. If a longer TTL is intentional, "
        "document why and update this guard."
    )
    assert http._PRINCIPAL_CACHE_MAX >= 64
    assert http._PRINCIPAL_CACHE_MAX <= 100_000


def test_set_and_get_within_ttl_returns_context(http, monkeypatch):
    monkeypatch.setattr(http, "_monotonic", lambda: 1000.0)
    ctx = http.MCPUserContext(user_id="alice", role="user",
                              namespace="alice")
    http._principal_cache_set("p1", ctx)
    monkeypatch.setattr(http, "_monotonic", lambda: 1100.0)
    assert http._principal_cache_get("p1") is ctx


def test_get_past_ttl_evicts_and_returns_none(http, monkeypatch):
    monkeypatch.setattr(http, "_monotonic", lambda: 1000.0)
    ctx = http.MCPUserContext(user_id="alice", role="user",
                              namespace="alice")
    http._principal_cache_set("p1", ctx)
    # 5min + 1s past
    monkeypatch.setattr(
        http, "_monotonic",
        lambda: 1000.0 + http._PRINCIPAL_CACHE_TTL_SECONDS + 1
    )
    assert http._principal_cache_get("p1") is None
    assert "p1" not in http._principal_context_cache, (
        "stale entry should be evicted on get"
    )


def test_set_beyond_cap_evicts_oldest(http, monkeypatch):
    """When the cap fills, set() must evict the OLDEST half by
    expiry time — not random, not newest-first. Codex round-1 of
    #203 noted the prior version of this test only asserted size
    and that `p_overflow` remained, which would let a random
    eviction policy pass too."""
    cap = http._PRINCIPAL_CACHE_MAX
    fake_now = [1000.0]
    monkeypatch.setattr(http, "_monotonic", lambda: fake_now[0])
    # Fill to exactly cap with monotonically-increasing expiry
    for i in range(cap):
        fake_now[0] = 1000.0 + i
        http._principal_cache_set(
            f"p{i}",
            http.MCPUserContext(user_id=f"u{i}", role="user",
                                namespace=f"u{i}"),
        )
    assert len(http._principal_context_cache) == cap

    # One more triggers eviction of the half-oldest
    fake_now[0] = 1000.0 + cap
    http._principal_cache_set(
        "p_overflow",
        http.MCPUserContext(user_id="overflow", role="user",
                            namespace="overflow"),
    )
    # Cap holds.
    assert len(http._principal_context_cache) <= cap, (
        f"cache size grew past cap={cap} — eviction logic broken"
    )
    # The newest insertion must still be present.
    assert "p_overflow" in http._principal_context_cache

    # Specifically verify "oldest half evicted, newest half kept":
    # the bottom (cap//2) entries by insertion order should be
    # gone; the top (cap//2) plus p_overflow should remain.
    half = cap // 2
    evicted_keys = {f"p{i}" for i in range(half)}
    surviving_keys = {f"p{i}" for i in range(half, cap)}
    leaked = evicted_keys & set(http._principal_context_cache)
    missing = surviving_keys - set(http._principal_context_cache)
    assert not leaked, (
        f"oldest-half eviction should have removed {sorted(leaked)} "
        f"but they're still cached — eviction policy may be random "
        f"or newest-first instead of oldest-first by expiry"
    )
    assert not missing, (
        f"newest-half should have survived eviction but "
        f"{sorted(missing)} were dropped — eviction took too much"
    )


def test_bare_context_backward_compat(http):
    """Tests historically did `cache[key] = MCPUserContext(...)`
    (raw, not tuple). The lookup helper must still return that
    bare entry — tests would otherwise break en-masse."""
    ctx = http.MCPUserContext(user_id="bob", role="root",
                              namespace="ops")
    http._principal_context_cache["bob-pid"] = ctx  # bare assign
    assert http._principal_cache_get("bob-pid") is ctx
    # Still in the cache (no eviction for bare entries).
    assert "bob-pid" in http._principal_context_cache

"""Tests for the three HIGH findings from the v3.2 memory-OS audit
(Codex thread 019dbd11):

  H1. Federation visibility — federated memories (owner_id='federation')
      are now readable by non-root callers via search / rehydrate /
      gateway context injection. Mutation paths still hard-filter
      by owner_id.

  H2. Consensus fields — consult() now populates consensus_response,
      consensus_score, winning_muse, cost, latency_ms from
      all_responses instead of leaving them None.

  H3. Gateway reliability — graeae.engine.route() applies the same
      circuit-breaker / rate-limiter / concurrency stack as consult()
      so /v1/chat/completions gets first-class operational controls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


from api.auth import UserContext


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


# ─── H1: federation read-path visibility ─────────────────────────────────────


class _Conn:
    def __init__(self):
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        return []


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install_pool(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def test_h1_fts_fetch_owner_clause_includes_federation(monkeypatch):
    """_fts_fetch with owner_id set must emit a WHERE clause that
    matches either the caller's owner_id OR any row carrying a
    federation_source (i.e., pulled from a peer)."""
    from api.lifecycle import _fts_fetch

    conn = _Conn()
    asyncio.run(_fts_fetch(conn, "query", 10, owner_id="alice"))

    sql = conn.fetches[-1][0]
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql


def test_h1_vector_search_owner_clause_includes_federation(monkeypatch):
    """Same contract for the pgvector variant."""
    from api.lifecycle import _vector_search

    conn = _Conn()
    asyncio.run(_vector_search(conn, [0.1, 0.2], 10, owner_id="alice"))

    sql = conn.fetches[-1][0]
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql


def test_h1_fts_fetch_no_owner_filter_leaves_query_clean(monkeypatch):
    """Root paths (owner_id=None) should NOT inject the federation
    clause — root sees everything anyway, and adding spurious OR
    clauses would degrade query plans."""
    from api.lifecycle import _fts_fetch

    conn = _Conn()
    asyncio.run(_fts_fetch(conn, "query", 10, owner_id=None))

    sql = conn.fetches[-1][0]
    assert "owner_id=$" not in sql
    assert "federation_source IS NOT NULL" not in sql


def test_slice2_fts_fetch_with_group_ids_uses_full_visibility(monkeypatch):
    """When group_ids is also passed, _fts_fetch emits the full
    v1_multiuser-mirror predicate: owner / federation / world /
    group-readable. Slice 2.1 closure — search now matches list/get
    visibility."""
    from api.lifecycle import _fts_fetch

    conn = _Conn()
    asyncio.run(_fts_fetch(
        conn, "query", 10, owner_id="alice", group_ids=["g1", "g2"],
    ))

    sql = conn.fetches[-1][0]
    args = conn.fetches[-1][1]
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql
    assert "permission_mode % 10" in sql           # world-readable
    assert "permission_mode >= 640" in sql         # group-readable threshold
    assert "group_id = ANY(" in sql                # group-membership branch
    assert "alice" in args
    assert ["g1", "g2"] in args                    # group_ids array passed


def test_slice2_vector_search_with_group_ids_uses_full_visibility(monkeypatch):
    """Same closure for the pgvector path."""
    from api.lifecycle import _vector_search

    conn = _Conn()
    asyncio.run(_vector_search(
        conn, [0.1, 0.2], 10, owner_id="alice", group_ids=[],
    ))

    sql = conn.fetches[-1][0]
    assert "permission_mode % 10" in sql
    assert "permission_mode >= 640" in sql
    assert "group_id = ANY(" in sql


def test_h1_gateway_context_search_includes_federation(monkeypatch):
    """/v1/chat/completions _search_mnemos_context uses an inline
    SELECT (not the shared helpers because it has a category-OR).
    That SQL must also match federated rows within the caller's
    namespace."""
    from api.handlers import openai_compat

    conn = _Conn()
    _install_pool(monkeypatch, conn)

    asyncio.run(openai_compat._search_mnemos_context("hello", _alice(), limit=5))

    sql = conn.fetches[-1][0]
    # v3.2 compression-in-hot-paths added table aliases (`m.`, `v.`)
    # and a LEFT JOIN to memory_compressed_variants. The federation
    # clause now reads `m.owner_id = $1 OR m.federation_source IS NOT NULL`.
    assert "owner_id = $1 OR" in sql
    assert "federation_source IS NOT NULL" in sql


# ─── H2: consensus fields populated ──────────────────────────────────────────


def test_h2_consensus_picks_highest_scoring_success():
    from graeae.engine import _compute_consensus

    responses = {
        "openai":    {"status": "success",    "response_text": "alpha", "final_score": 0.82, "latency_ms": 300, "cost": 0.002},
        "anthropic": {"status": "success",    "response_text": "beta",  "final_score": 0.95, "latency_ms": 480, "cost": 0.004},
        "groq":      {"status": "error",      "response_text": "",      "final_score": 0.0,  "latency_ms": 100, "cost": 0.0},
    }
    out = _compute_consensus(responses)
    assert out["winning_muse"] == "anthropic"
    assert out["consensus_response"] == "beta"
    assert abs(out["consensus_score"] - 0.95) < 1e-9
    # Cost is sum across providers (reported cost only)
    assert abs(out["cost"] - (0.002 + 0.004)) < 1e-9
    # Latency is max (parallel fan-out wall clock)
    assert out["latency_ms"] == 480


def test_h2_consensus_no_winner_safe_defaults():
    """When every provider failed, consensus fields are present with
    defensive zero/empty defaults — callers never see them missing."""
    from graeae.engine import _compute_consensus

    responses = {
        "openai": {"status": "error", "response_text": "", "final_score": 0.0, "latency_ms": 100, "cost": 0.0},
        "groq":   {"status": "unavailable", "response_text": "", "final_score": 0.0, "latency_ms": 0, "cost": 0.0},
    }
    out = _compute_consensus(responses)
    assert out["winning_muse"] is None
    assert out["consensus_response"] == ""
    assert out["consensus_score"] == 0.0
    assert out["cost"] == 0.0
    assert out["latency_ms"] == 100  # still the max observed


def test_h2_consensus_contract_has_all_keys_even_when_empty():
    """The contract must always return every field even for an empty
    input — handlers rely on the dict being spreadable into the
    ConsultationResponse without KeyErrors."""
    from graeae.engine import _compute_consensus

    out = _compute_consensus({})
    for k in ("consensus_response", "consensus_score", "winning_muse", "cost", "latency_ms"):
        assert k in out, f"missing {k}"
    assert out["winning_muse"] is None


# ─── H3: route() applies reliability stack ───────────────────────────────────


class _FakeBreakerPool:
    def __init__(self, *, allow=True):
        self._allow = allow
        self.success_calls: list[str] = []
        self.failure_calls: list[str] = []

    def is_allowed(self, name): return self._allow
    def record_success(self, name): self.success_calls.append(name)
    def record_failure(self, name): self.failure_calls.append(name)


class _FakeRateLimiterPool:
    def __init__(self, *, allow=True):
        self._allow = allow
    def is_allowed(self, name): return self._allow


class _FakeConcurrency:
    def __init__(self, *, allow=True):
        self._allow = allow
        self.released: list[str] = []

    async def acquire(self, name): return self._allow

    def release(self, name): self.released.append(name)

    def status(self): return {}


class _FakeQuality:
    def __init__(self):
        self.successes: list[tuple[str, int]] = []
        self.failures: list[str] = []

    def record_success(self, name, latency): self.successes.append((name, latency))
    def record_failure(self, name): self.failures.append(name)

    def dynamic_weight(self, name): return 0.8


def _engine_with_fakes(*, breaker_allow=True, rate_allow=True, conc_allow=True):
    """Build a GraeaeEngine instance with its reliability substructures
    replaced by controllable fakes. We don't need a real key file or
    HTTP client because the _query_provider call is also stubbed in
    the individual tests."""
    from graeae.engine import GraeaeEngine

    engine = GraeaeEngine()
    engine.providers = {
        "openai": {
            "url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-5", "weight": 0.9, "api": "openai", "key_name": "openai",
        },
    }
    engine._circuit_breakers = _FakeBreakerPool(allow=breaker_allow)
    engine._rate_limiters = _FakeRateLimiterPool(allow=rate_allow)
    engine._quality = _FakeQuality()
    engine._concurrency = _FakeConcurrency(allow=conc_allow)
    # Pretend we have a key so the route() pre-check doesn't short-circuit
    from graeae import api_keys
    api_keys._LLM_PROVIDERS["openai"] = {"api_key": "sk-test"}
    return engine


def test_h3_route_refused_when_circuit_open(monkeypatch):
    engine = _engine_with_fakes(breaker_allow=False)
    result = asyncio.run(engine.route("openai", "gpt-5", "prompt", "reasoning"))
    assert result["status"] == "unavailable"
    assert "circuit open" in result["error"]


def test_h3_route_refused_when_rate_limited(monkeypatch):
    engine = _engine_with_fakes(rate_allow=False)
    result = asyncio.run(engine.route("openai", "gpt-5", "prompt", "reasoning"))
    assert result["status"] == "unavailable"
    assert "rate-limited" in result["error"]


def test_h3_route_refused_when_concurrency_saturated(monkeypatch):
    engine = _engine_with_fakes(conc_allow=False)
    result = asyncio.run(engine.route("openai", "gpt-5", "prompt", "reasoning"))
    assert result["status"] == "unavailable"
    assert "concurrency saturated" in result["error"]


def test_h3_route_releases_concurrency_on_success(monkeypatch):
    engine = _engine_with_fakes()

    async def _fake_query(self, provider_name, prompt, task_type, timeout, model_override=None):
        return {"status": "success", "response_text": "ok", "latency_ms": 20, "model_id": "gpt-5", "final_score": 0.9}

    monkeypatch.setattr(engine.__class__, "_query_provider", _fake_query)

    asyncio.run(engine.route("openai", "gpt-5", "prompt", "reasoning"))

    # Concurrency slot released + success credited
    assert engine._concurrency.released == ["openai"]
    assert engine._circuit_breakers.success_calls == ["openai"]
    assert engine._quality.successes and engine._quality.successes[0][0] == "openai"


def test_h3_route_releases_concurrency_on_provider_error(monkeypatch):
    """Even when _query_provider raises, the concurrency slot must be
    released and the breaker/quality tracker must record the failure
    — otherwise the gateway leaks concurrency capacity on errors."""
    engine = _engine_with_fakes()

    async def _fake_query(self, provider_name, prompt, task_type, timeout, model_override=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine.__class__, "_query_provider", _fake_query)

    result = asyncio.run(engine.route("openai", "gpt-5", "prompt", "reasoning"))

    assert result["status"] == "unavailable"
    assert "RuntimeError" in result["error"]
    assert engine._concurrency.released == ["openai"]
    assert engine._circuit_breakers.failure_calls == ["openai"]
    assert engine._quality.failures == ["openai"]

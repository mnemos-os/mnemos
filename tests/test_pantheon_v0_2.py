from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from mnemos.api.dependencies import UserContext, get_current_user


def _user(user_id: str = "alice") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


class _FakeEngineV02:
    def __init__(self):
        self.providers = {
            "cheap": {
                "url": "https://cheap.example/v1/chat/completions",
                "model": "cheap-chat",
                "weight": 0.86,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat"],
                "usage_tier": "agentic_ok",
                "input_cost_per_mtok": 0.10,
                "output_cost_per_mtok": 0.20,
                "p50_latency_ms": 400,
            },
            "slow": {
                "url": "https://slow.example/v1/chat/completions",
                "model": "slow-chat",
                "weight": 0.92,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat", "reasoning"],
                "usage_tier": "agentic_ok",
                "input_cost_per_mtok": 1.00,
                "output_cost_per_mtok": 1.00,
                "p50_latency_ms": 900,
            },
            "anthropic": {
                "url": "https://anthropic.example/v1/chat/completions",
                "model": "claude-consult",
                "weight": 0.95,
                "api": "openai",
                "key_name": "anthropic",
                "capabilities": ["chat", "reasoning"],
                "usage_tier": "consultation_only",
                "input_cost_per_mtok": 3.00,
                "output_cost_per_mtok": 15.00,
                "p50_latency_ms": 700,
            },
        }

    def provider_status(self) -> dict[str, Any]:
        return {
            "circuit_breakers": {
                "cheap": {"state": "closed"},
                "slow": {"state": "closed"},
                "anthropic": {"state": "closed"},
            }
        }


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PolicyConnection:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    async def fetch(self, query: str, *args):
        compact = " ".join(query.split())
        if "FROM model_registry" in compact:
            return []
        if "category='pantheon_routing'" not in compact:
            return []
        window_minutes = int(args[0])
        candidates = set(args[1])
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.rows:
            created = row.get("created") or datetime.now(timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            metadata = row["metadata"]
            backend = metadata.get("resolved_to")
            if backend in candidates and created > cutoff:
                grouped.setdefault(backend, []).append(metadata)

        out = []
        for backend, items in grouped.items():
            out.append({
                "backend": backend,
                "avg_latency_ms": sum(float(item["latency_ms"]) for item in items) / len(items),
                "error_rate": sum(1 for item in items if item.get("outcome") == "error") / len(items),
                "avg_cost": sum(float(item["cost_usd"]) for item in items) / len(items),
            })
        return out


class _PolicyPool:
    def __init__(self, rows: list[dict[str, Any]]):
        self.conn = _PolicyConnection(rows)

    def acquire(self):
        return _AcquireContext(self.conn)


def _routing_rows(*, backend: str, count: int, latency_ms: float, cost_usd: float = 0.01) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
        {
            "created": now,
            "metadata": {
                "resolved_to": backend,
                "latency_ms": latency_ms + i,
                "outcome": "success",
                "cost_usd": cost_usd,
            },
        }
        for i in range(count)
    ]


async def _drain_background_tasks() -> None:
    import mnemos.core.lifecycle as lc

    tasks = list(lc._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _routing_memories(db_pool) -> list[dict[str, Any]]:
    return [
        memory for memory in db_pool.state["memories"].values()
        if memory.get("category") == "pantheon_routing"
    ]


@pytest_asyncio.fixture
async def pantheon_v02_client(monkeypatch, db_pool):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests
    import mnemos.core.lifecycle as lc
    import mnemos.domain.pantheon.catalog as pantheon_catalog
    import mnemos.domain.pantheon.gateway as pantheon_gateway
    from mnemos.domain.pantheon.caps import consultation_cap_bucket
    from tests._fake_backend import FakePoolBackedBackend

    monkeypatch.setenv("MNEMOS_PANTHEON_ENABLED", "true")
    monkeypatch.setenv("MNEMOS_PANTHEON_CONSULTATION_CAP", "50")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", "0.80")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", "10.0")
    _reset_settings_for_tests()
    consultation_cap_bucket.reset()

    fake_engine = _FakeEngineV02()
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_persistence_backend", FakePoolBackedBackend(db_pool))
    monkeypatch.setattr(pantheon_catalog, "get_graeae_engine", lambda: fake_engine)
    monkeypatch.setattr(pantheon_gateway, "get_graeae_engine", lambda: fake_engine)
    monkeypatch.setattr(pantheon_gateway, "get_key", lambda _provider: "test-key")

    async def fake_forward(decision, body):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": decision.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(pantheon_gateway, "forward_chat_completion", fake_forward)

    app.dependency_overrides[get_current_user] = lambda: _user()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, db_pool
    finally:
        await _drain_background_tasks()
        app.dependency_overrides.pop(get_current_user, None)
        consultation_cap_bucket.reset()
        monkeypatch.delenv("MNEMOS_PANTHEON_ENABLED", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_CONSULTATION_CAP", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", raising=False)
        _reset_settings_for_tests()


@pytest.mark.asyncio
async def test_agentic_cap_blocks_fifty_first_same_session(pantheon_v02_client):
    client, _db_pool = pantheon_v02_client
    headers = {"X-Pantheon-Session": "session-a"}
    body = {"model": "claude-consult", "messages": [{"role": "user", "content": "hi"}]}

    for _ in range(50):
        response = await client.post("/pantheon/v1/chat/completions", json=body, headers=headers)
        assert response.status_code == 200

    blocked = await client.post("/pantheon/v1/chat/completions", json=body, headers=headers)

    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["usage_tier"] == "consultation_only"
    assert error["cap"] == 50
    assert "retry_after" in error


@pytest.mark.asyncio
async def test_agentic_cap_is_independent_by_session(pantheon_v02_client):
    client, _db_pool = pantheon_v02_client
    body = {"model": "claude-consult", "messages": [{"role": "user", "content": "hi"}]}

    for _ in range(50):
        response = await client.post(
            "/pantheon/v1/chat/completions",
            json=body,
            headers={"X-Pantheon-Session": "session-a"},
        )
        assert response.status_code == 200

    blocked = await client.post(
        "/pantheon/v1/chat/completions",
        json=body,
        headers={"X-Pantheon-Session": "session-a"},
    )
    other_session = await client.post(
        "/pantheon/v1/chat/completions",
        json=body,
        headers={"X-Pantheon-Session": "session-b"},
    )

    assert blocked.status_code == 429
    assert other_session.status_code == 200


@pytest.mark.asyncio
async def test_routing_log_success_writes_pantheon_memory(pantheon_v02_client):
    client, db_pool = pantheon_v02_client

    response = await client.post(
        "/pantheon/v1/chat/completions",
        json={"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Pantheon-Session": "log-success"},
    )
    await _drain_background_tasks()

    assert response.status_code == 200
    [memory] = _routing_memories(db_pool)
    payload = json.loads(memory["content"])
    assert payload["tenant_user_id"] == "alice"
    assert payload["alias_or_model"] == "cheap-chat"
    assert payload["resolved_to"] == "cheap-chat"
    assert payload["outcome"] == "success"
    assert payload["tokens_in"] == 10
    assert payload["tokens_out"] == 5
    assert payload["cost_usd"] is not None
    assert memory["owner_id"] == "system:pantheon"
    assert memory["namespace"] == "pantheon"
    assert memory["metadata"]["pantheon_version"] == "0.2"
    assert memory["metadata"]["session_id"] == "log-success"
    assert memory["metadata"]["usage_tier"] == "agentic_ok"


@pytest.mark.asyncio
async def test_routing_log_error_writes_error_outcome(monkeypatch, pantheon_v02_client):
    import mnemos.domain.pantheon.gateway as pantheon_gateway

    client, db_pool = pantheon_v02_client

    async def fake_error(decision, body):
        raise pantheon_gateway.PantheonGatewayError(502, "upstream failed")

    monkeypatch.setattr(pantheon_gateway, "forward_chat_completion", fake_error)

    response = await client.post(
        "/pantheon/v1/chat/completions",
        json={"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Pantheon-Session": "log-error"},
    )
    await _drain_background_tasks()

    assert response.status_code == 502
    [memory] = _routing_memories(db_pool)
    payload = json.loads(memory["content"])
    assert payload["outcome"] == "error"
    assert payload["error_class"] == "PantheonGatewayError"
    assert memory["metadata"]["outcome"] == "error"
    assert memory["metadata"]["error_class"] == "PantheonGatewayError"


@pytest.mark.asyncio
async def test_rolling_window_policy_picks_best_scoring_backend():
    from mnemos.domain.pantheon.policy import resolve_with_policy

    rows = [
        *_routing_rows(backend="backend_a", count=5, latency_ms=100, cost_usd=0.02),
        *_routing_rows(backend="backend_b", count=5, latency_ms=500, cost_usd=0.02),
    ]
    route = await resolve_with_policy(
        _PolicyPool(rows),
        "auto:reasoning",
        [
            {"id": "backend_a", "provider": "a", "cost_per_mtok": 1.0},
            {"id": "backend_b", "provider": "b", "cost_per_mtok": 1.0},
        ],
    )

    assert route.selected["id"] == "backend_a"
    assert route.scores["backend_a"]["weighted_score"] < route.scores["backend_b"]["weighted_score"]
    assert route.selection_reason == "best weighted score (latency+error+cost)"


@pytest.mark.asyncio
async def test_empty_rolling_window_falls_back_to_cheapest_first():
    from mnemos.domain.pantheon.policy import resolve_with_policy

    route = await resolve_with_policy(
        _PolicyPool([]),
        "auto:reasoning",
        [
            {"id": "expensive", "provider": "expensive", "cost_per_mtok": 5.0},
            {"id": "cheap", "provider": "cheap", "cost_per_mtok": 0.1},
        ],
    )

    assert route.selected["id"] == "cheap"
    assert route.telemetry_available is False
    assert route.selection_reason == "fallback cheapest-first policy (no rolling telemetry)"


@pytest.mark.asyncio
async def test_route_explain_returns_scores_and_selection_reason(monkeypatch, pantheon_v02_client):
    import mnemos.core.lifecycle as lc

    client, _db_pool = pantheon_v02_client
    rows = [
        *_routing_rows(backend="cheap-chat", count=5, latency_ms=500, cost_usd=0.01),
        *_routing_rows(backend="slow-chat", count=5, latency_ms=100, cost_usd=0.05),
    ]
    monkeypatch.setattr(lc, "_pool", _PolicyPool(rows))

    response = await client.request(
        "GET",
        "/pantheon/v1/route/explain",
        json={"model": "auto:cheap", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["alias"] == "auto:cheap"
    assert data["candidates"]
    assert data["rolling_window_minutes"] == 15
    assert "cheap-chat" in data["scores"]
    assert "slow-chat" in data["scores"]
    assert data["selected"] == "slow-chat"
    assert data["selection_reason"] == "best weighted score (latency+error+cost)"

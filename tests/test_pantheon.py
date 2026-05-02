from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemos.api.dependencies import UserContext, get_current_user


def _user(user_id: str = "alice") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


class _FakeEngine:
    def __init__(self):
        self.providers = {
            "cheap": {
                "url": "https://cheap.example/v1/chat/completions",
                "model": "cheap-chat",
                "weight": 0.70,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat"],
                "usage_tier": "budget",
                "input_cost_per_mtok": 0.10,
                "output_cost_per_mtok": 0.20,
                "p50_latency_ms": 120,
            },
            "reasoner": {
                "url": "https://reasoner.example/v1/chat/completions",
                "model": "reason-pro",
                "weight": 0.94,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat", "reasoning"],
                "usage_tier": "premium",
                "input_cost_per_mtok": 1.00,
                "output_cost_per_mtok": 1.00,
                "p50_latency_ms": 300,
            },
            "coder": {
                "url": "https://coder.example/v1/chat/completions",
                "model": "code-pro",
                "weight": 0.91,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat", "code", "reasoning"],
                "usage_tier": "frontier",
                "input_cost_per_mtok": 2.00,
                "output_cost_per_mtok": 2.00,
                "p50_latency_ms": 500,
            },
        }

    def provider_status(self) -> dict[str, Any]:
        return {
            "circuit_breakers": {
                "cheap": {"state": "closed"},
                "reasoner": {"state": "closed"},
                "coder": {"state": "closed"},
            }
        }


@pytest.fixture
def pantheon_client(monkeypatch):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests
    import mnemos.core.lifecycle as lc
    import mnemos.domain.pantheon.catalog as pantheon_catalog
    import mnemos.domain.pantheon.gateway as pantheon_gateway

    monkeypatch.setenv("MNEMOS_PANTHEON_ENABLED", "true")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", "0.90")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", "10.0")
    _reset_settings_for_tests()
    monkeypatch.setattr(lc, "_pool", None)

    fake_engine = _FakeEngine()
    monkeypatch.setattr(pantheon_catalog, "get_graeae_engine", lambda: fake_engine)
    monkeypatch.setattr(pantheon_gateway, "get_graeae_engine", lambda: fake_engine)
    monkeypatch.setattr(pantheon_gateway, "get_key", lambda _provider: "test-key")

    app.dependency_overrides[get_current_user] = lambda: _user()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_PANTHEON_ENABLED", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", raising=False)
        _reset_settings_for_tests()


def test_models_disabled_returns_503(monkeypatch):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests

    monkeypatch.setenv("MNEMOS_PANTHEON_ENABLED", "false")
    _reset_settings_for_tests()
    app.dependency_overrides[get_current_user] = lambda: _user()
    try:
        with TestClient(app) as client:
            response = client.get("/pantheon/v1/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_PANTHEON_ENABLED", raising=False)
        _reset_settings_for_tests()

    assert response.status_code == 503
    assert response.json()["detail"] == "PANTHEON disabled in this profile"


def test_models_enabled_lists_mocked_graeae_registry(pantheon_client):
    response = pantheon_client.get("/pantheon/v1/models")

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert {model["id"] for model in data["data"]} == {"cheap-chat", "reason-pro", "code-pro"}
    assert data["data"][0]["capabilities"]
    assert data["data"][0]["usage_tier"]
    assert "health" in data["data"][0]


def test_chat_auto_cheap_resolves_to_cheapest_and_calls_provider(monkeypatch, pantheon_client):
    import mnemos.domain.pantheon.gateway as pantheon_gateway

    calls = []

    async def fake_forward(decision, body):
        calls.append((decision, body))
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
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(pantheon_gateway, "forward_chat_completion", fake_forward)

    response = pantheon_client.post(
        "/pantheon/v1/chat/completions",
        json={"model": "auto:cheap", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "cheap-chat"
    assert calls[0][0].provider == "cheap"


def test_chat_auto_reasoning_respects_quality_floor(monkeypatch, pantheon_client):
    import mnemos.domain.pantheon.gateway as pantheon_gateway

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
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(pantheon_gateway, "forward_chat_completion", fake_forward)

    response = pantheon_client.post(
        "/pantheon/v1/chat/completions",
        json={"model": "auto:reasoning", "messages": [{"role": "user", "content": "think"}]},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "reason-pro"


def test_route_explain_returns_resolution_chain(pantheon_client):
    response = pantheon_client.request(
        "GET",
        "/pantheon/v1/route/explain",
        json={"model": "auto:cheap", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["alias"] == "auto:cheap"
    assert data["resolved_model"] == "cheap-chat"
    assert data["resolution_chain"]


@pytest.mark.asyncio
async def test_tool_pantheon_list_models_filters_code(monkeypatch, pantheon_client):
    del pantheon_client
    from mnemos.mcp.tools.models import tool_pantheon_list_models

    result = await tool_pantheon_list_models(filter_capabilities=["code"])

    assert result["success"] is True
    assert [model["id"] for model in result["data"]] == ["code-pro"]


@pytest.mark.asyncio
async def test_tool_pantheon_route_explain_matches_http_shape(monkeypatch, pantheon_client):
    del monkeypatch
    from mnemos.mcp.tools.models import tool_pantheon_route_explain

    http_response = pantheon_client.request(
        "GET",
        "/pantheon/v1/route/explain",
        json={"model": "auto:cheap", "messages": [{"role": "user", "content": "hi"}]},
    )
    tool_response = await tool_pantheon_route_explain(
        messages=[{"role": "user", "content": "hi"}],
        model_or_alias="auto:cheap",
    )

    assert tool_response["success"] is True
    assert tool_response["resolved_model"] == http_response.json()["resolved_model"]
    assert tool_response["resolution_chain"] == http_response.json()["resolution_chain"]


def test_pantheon_rate_limit_is_applied_per_user(monkeypatch, pantheon_client):
    from mnemos.api.main import app

    current = {"user": _user("alice")}
    app.dependency_overrides[get_current_user] = lambda: current["user"]

    for _ in range(60):
        response = pantheon_client.get("/pantheon/v1/models")
        assert response.status_code == 200
    blocked = pantheon_client.get("/pantheon/v1/models")
    assert blocked.status_code == 429

    current["user"] = _user("bob")
    response = pantheon_client.get("/pantheon/v1/models")
    assert response.status_code == 200

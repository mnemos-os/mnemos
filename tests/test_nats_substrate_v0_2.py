from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.domain.pantheon.routing_log import (
    PANTHEON_ROUTING_SCHEMA_VERSION,
    PANTHEON_ROUTING_SUBJECT,
)
from mnemos.nats import publisher as nats_publisher
from mnemos.workers import pantheon_routing_audit_consumer as audit_consumer


def _user(user_id: str = "alice") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.providers = {
            "cheap": {
                "url": "https://cheap.example/v1/chat/completions",
                "model": "cheap-chat",
                "weight": 0.90,
                "api": "openai",
                "key_name": "openai",
                "capabilities": ["chat", "reasoning"],
                "usage_tier": "agentic_ok",
                "input_cost_per_mtok": 0.10,
                "output_cost_per_mtok": 0.20,
                "p50_latency_ms": 400,
            }
        }

    def provider_status(self) -> dict[str, Any]:
        return {"circuit_breakers": {"cheap": {"state": "closed"}}}


async def _drain_background_tasks() -> None:
    import mnemos.core.lifecycle as lc

    for _ in range(5):
        tasks = list(lc._background_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)


def _routing_memories(db_pool) -> list[dict[str, Any]]:
    return [
        memory for memory in db_pool.state["memories"].values()
        if memory.get("category") == "pantheon_routing"
    ]


@asynccontextmanager
async def _pantheon_client(monkeypatch: pytest.MonkeyPatch, db_pool, *, publish_nats: bool):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests
    import mnemos.core.lifecycle as lc
    import mnemos.domain.pantheon.catalog as pantheon_catalog
    import mnemos.domain.pantheon.gateway as pantheon_gateway
    from mnemos.domain.pantheon.caps import consultation_cap_bucket
    from tests._fake_backend import FakePoolBackedBackend

    monkeypatch.setenv("MNEMOS_PANTHEON_ENABLED", "true")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", "0.80")
    monkeypatch.setenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", "10.0")
    monkeypatch.setenv("MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING", "1" if publish_nats else "0")
    _reset_settings_for_tests()
    consultation_cap_bucket.reset()

    fake_engine = _FakeEngine()
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
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR", raising=False)
        monkeypatch.delenv("MNEMOS_PANTHEON_DEFAULT_MAX_COST", raising=False)
        monkeypatch.delenv("MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING", raising=False)
        _reset_settings_for_tests()


@pytest.fixture
def nats_publish_calls(monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def fake_publish(subject: str, payload: dict[str, Any], *, msg_id: str | None = None):
        calls.append({"subject": subject, "payload": payload, "msg_id": msg_id})

    monkeypatch.setattr(nats_publisher, "publish_event", fake_publish)
    return calls


@pytest.mark.asyncio
async def test_pantheon_gateway_publish_enabled_writes_memory_and_nats(
    monkeypatch: pytest.MonkeyPatch,
    db_pool,
    nats_publish_calls,
):
    async with _pantheon_client(monkeypatch, db_pool, publish_nats=True) as (client, pool):
        response = await client.post(
            "/pantheon/v1/chat/completions",
            json={"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "req-nats-on", "X-Pantheon-Session": "session-a"},
        )
        await _drain_background_tasks()

    assert response.status_code == 200
    memories = _routing_memories(pool)
    assert len(memories) == 1
    memory_payload = json.loads(memories[0]["content"])
    assert memory_payload["request_id"] == "req-nats-on"
    assert nats_publish_calls == [
        {
            "subject": PANTHEON_ROUTING_SUBJECT,
            "payload": {
                **memory_payload,
                "metadata": {
                    **memories[0]["metadata"],
                    "schema_version": PANTHEON_ROUTING_SCHEMA_VERSION,
                },
            },
            "msg_id": "pantheon.routing.req-nats-on",
        }
    ]


@pytest.mark.asyncio
async def test_pantheon_gateway_publish_disabled_writes_only_memory(
    monkeypatch: pytest.MonkeyPatch,
    db_pool,
    nats_publish_calls,
):
    async with _pantheon_client(monkeypatch, db_pool, publish_nats=False) as (client, pool):
        response = await client.post(
            "/pantheon/v1/chat/completions",
            json={"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "req-nats-off"},
        )
        await _drain_background_tasks()

    assert response.status_code == 200
    assert len(_routing_memories(pool)) == 1
    assert nats_publish_calls == []


@pytest.mark.asyncio
async def test_pantheon_gateway_nats_publish_failure_does_not_fail_request(
    monkeypatch: pytest.MonkeyPatch,
    db_pool,
    caplog: pytest.LogCaptureFixture,
):
    async def raise_publish(subject: str, payload: dict[str, Any], *, msg_id: str | None = None):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(nats_publisher, "publish_event", raise_publish)
    caplog.set_level(logging.WARNING, logger="mnemos.domain.pantheon.routing_log")

    async with _pantheon_client(monkeypatch, db_pool, publish_nats=True) as (client, pool):
        response = await client.post(
            "/pantheon/v1/chat/completions",
            json={"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "req-nats-raises"},
        )
        await _drain_background_tasks()

    assert response.status_code == 200
    assert len(_routing_memories(pool)) == 1
    assert "routing NATS publish failed" in caplog.text


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AuditConnection:
    def __init__(self) -> None:
        self.rows = []

    async def execute(self, sql: str, *args):
        self.rows.append({
            "sql": sql,
            "request_id": args[0],
            "tenant_user_id": args[1],
            "alias_or_model": args[2],
            "resolved_to": args[3],
            "outcome": args[4],
            "latency_ms": args[5],
            "tokens_in": args[6],
            "tokens_out": args[7],
            "cost_usd": args[8],
            "error_class": args[9],
            "payload": json.loads(args[10]),
        })
        return "INSERT 0 1"


class _AuditPool:
    def __init__(self) -> None:
        self.conn = _AuditConnection()

    def acquire(self):
        return _AcquireContext(self.conn)


class _FakeMsg:
    def __init__(self, payload: dict[str, Any]):
        self.subject = PANTHEON_ROUTING_SUBJECT
        self.data = json.dumps(payload).encode("utf-8")


@pytest.mark.asyncio
async def test_pantheon_routing_audit_consumer_inserts_expected_columns():
    pool = _AuditPool()
    payload = {
        "request_id": "req-audit",
        "tenant_user_id": "alice",
        "alias_or_model": "auto:cheap",
        "resolved_to": "cheap-chat",
        "outcome": "success",
        "latency_ms": 12.7,
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": "0.0123",
        "error_class": None,
        "metadata": {"schema_version": PANTHEON_ROUTING_SCHEMA_VERSION},
    }

    await audit_consumer.handle_message(pool, _FakeMsg(payload))

    assert len(pool.conn.rows) == 1
    row = pool.conn.rows[0]
    assert "INSERT INTO pantheon_routing_audit" in row["sql"]
    assert row["request_id"] == "req-audit"
    assert row["tenant_user_id"] == "alice"
    assert row["alias_or_model"] == "auto:cheap"
    assert row["resolved_to"] == "cheap-chat"
    assert row["outcome"] == "success"
    assert row["latency_ms"] == 13
    assert row["tokens_in"] == 10
    assert row["tokens_out"] == 5
    assert row["cost_usd"] == Decimal("0.0123")
    assert row["payload"] == payload


@pytest.mark.skip(
    reason=(
        "Requires a real NATS broker and Postgres: set MNEMOS_NATS_URL, "
        "optional MNEMOS_NATS_TOKEN, MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING=1, "
        "MNEMOS_NATS_AUDIT_CONSUMER_ENABLED=1, and apply "
        "db/migrations_v4_2_pantheon_routing_audit.sql."
    )
)
def test_live_pantheon_routing_nats_audit_smoke():
    """End-to-end smoke placeholder for operator-run NATS/Postgres validation."""

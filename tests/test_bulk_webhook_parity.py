"""Bulk memory create webhook parity regressions."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from api.auth import UserContext, get_current_user

pytestmark = pytest.mark.asyncio


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BulkConn:
    def __init__(self, pool: "_BulkPool"):
        self.pool = pool
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self):
        return _AsyncNullContext()

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO memories "):
            self.pool.inserts.append(
                {
                    "conn": self,
                    "id": args[0],
                    "content": args[1],
                    "category": args[2],
                    "subcategory": args[3],
                    "owner_id": args[6],
                    "namespace": args[7],
                }
            )
            return "INSERT 0 1"
        return "OK"


class _AcquireContext:
    def __init__(self, pool: "_BulkPool"):
        self.pool = pool
        self.conn: _BulkConn | None = None

    async def __aenter__(self):
        self.conn = _BulkConn(self.pool)
        self.pool.connections.append(self.conn)
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BulkPool:
    def __init__(self):
        self.connections: list[_BulkConn] = []
        self.inserts: list[dict[str, Any]] = []

    def acquire(self):
        return _AcquireContext(self)


@pytest.fixture
def current_user_override():
    from api_server import app

    current = {"user": _alice()}

    async def override_user():
        return current["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _install_pool(monkeypatch: pytest.MonkeyPatch, pool: _BulkPool) -> None:
    import api.lifecycle as lc
    from api_server import app

    monkeypatch.setattr(lc, "_pool", pool)
    monkeypatch.setattr(lc, "_cache", None)
    app.state.pool = pool


def _memory(content: str, category: str = "facts", subcategory: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "category": category}
    if subcategory is not None:
        body["subcategory"] = subcategory
    return body


async def test_bulk_create_emits_memory_created_for_each_success(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    pool = _BulkPool()
    _install_pool(monkeypatch, pool)
    events: list[dict[str, Any]] = []

    async def fake_dispatch(event_type, payload, *, conn=None, owner_id, namespace):
        events.append(
            {
                "conn": conn,
                "event_type": event_type,
                "payload": payload,
                "owner_id": owner_id,
                "namespace": namespace,
            }
        )

    from api import webhook_dispatcher

    monkeypatch.setattr(webhook_dispatcher, "dispatch", fake_dispatch)

    resp = await client.post(
        "/v1/memories/bulk",
        json={
            "memories": [
                _memory("first", "facts"),
                _memory("second", "decisions", "architecture"),
                _memory("third", "notes"),
            ]
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    memory_ids = data["memory_ids"]
    assert data == {"created": 3, "memory_ids": memory_ids, "errors": []}
    assert [insert["id"] for insert in pool.inserts] == memory_ids
    assert [event["payload"]["memory_id"] for event in events] == memory_ids
    assert [event["event_type"] for event in events] == ["memory.created"] * 3
    assert {event["owner_id"] for event in events} == {"alice"}
    assert {event["namespace"] for event in events} == {"alice-ns"}
    assert {event["payload"]["owner_id"] for event in events} == {"alice"}
    assert {event["payload"]["namespace"] for event in events} == {"alice-ns"}
    assert len(pool.connections) == 1
    assert {event["conn"] for event in events} == {pool.connections[0]}
    assert {insert["conn"] for insert in pool.inserts} == {pool.connections[0]}


async def test_bulk_create_dispatches_only_successful_items(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    pool = _BulkPool()
    _install_pool(monkeypatch, pool)
    events: list[dict[str, Any]] = []

    async def fake_dispatch(event_type, payload, *, conn=None, owner_id, namespace):
        events.append({"payload": payload, "owner_id": owner_id, "namespace": namespace})

    from api import webhook_dispatcher

    monkeypatch.setattr(webhook_dispatcher, "dispatch", fake_dispatch)

    resp = await client.post(
        "/v1/memories/bulk",
        json={
            "memories": [
                _memory("valid one", "facts"),
                _memory("   ", "facts"),
                _memory("valid two", "notes"),
            ]
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["created"] == 2
    assert len(data["memory_ids"]) == 2
    assert data["errors"] == ["[1] content is empty"]
    assert [insert["id"] for insert in pool.inserts] == data["memory_ids"]
    assert [event["payload"]["memory_id"] for event in events] == data["memory_ids"]
    assert [event["payload"]["content"] for event in events] == ["valid one", "valid two"]
    assert {event["owner_id"] for event in events} == {"alice"}
    assert {event["namespace"] for event in events} == {"alice-ns"}


async def test_bulk_create_fails_when_outbox_enqueue_fails(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    pool = _BulkPool()
    _install_pool(monkeypatch, pool)
    dispatch_attempts: list[dict[str, Any]] = []
    caplog.set_level(logging.WARNING, logger="api.handlers.memories")

    async def failing_dispatch(event_type, payload, *, conn=None, owner_id, namespace):
        dispatch_attempts.append(payload)
        raise RuntimeError("dispatcher unavailable")

    from api import webhook_dispatcher

    monkeypatch.setattr(webhook_dispatcher, "dispatch", failing_dispatch)

    resp = await client.post(
        "/v1/memories/bulk",
        json={"memories": [_memory("committed one"), _memory("committed two")]},
        headers=auth_headers,
    )

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == "Bulk memory creation failed"
    assert dispatch_attempts
    assert "webhook dispatch failed" not in caplog.text

"""Bulk memory create webhook parity regressions."""

from __future__ import annotations

from typing import Any

import pytest

from mnemos.api.dependencies import UserContext, get_current_user
from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


@pytest.fixture
def current_user_override():
    from mnemos.api.main import app

    current = {"user": _alice()}

    async def override_user():
        return current["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


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
    backend = install_fake_backend(monkeypatch)

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
    insert_calls = [payload for name, payload in backend.memories.calls if name == "insert_memory"]
    webhook_calls = [payload for name, payload in backend.webhooks.calls if name == "dispatch_event"]
    assert [call["memory_id"] for call in insert_calls] == memory_ids
    assert [call["payload"]["memory_id"] for call in webhook_calls] == memory_ids
    assert [call["event_type"] for call in webhook_calls] == ["memory.created"] * 3
    assert {call["owner_id"] for call in webhook_calls} == {"alice"}
    assert {call["namespace"] for call in webhook_calls} == {"alice-ns"}
    assert {call["payload"]["owner_id"] for call in webhook_calls} == {"alice"}
    assert {call["payload"]["namespace"] for call in webhook_calls} == {"alice-ns"}
    assert backend.commits == 3
    assert backend.rollbacks == 0


async def test_bulk_create_dispatches_only_successful_items(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)

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
    insert_calls = [payload for name, payload in backend.memories.calls if name == "insert_memory"]
    webhook_calls = [payload for name, payload in backend.webhooks.calls if name == "dispatch_event"]
    assert [call["memory_id"] for call in insert_calls] == data["memory_ids"]
    assert [call["payload"]["memory_id"] for call in webhook_calls] == data["memory_ids"]
    assert [call["payload"]["content"] for call in webhook_calls] == ["valid one", "valid two"]
    assert {call["owner_id"] for call in webhook_calls} == {"alice"}
    assert {call["namespace"] for call in webhook_calls} == {"alice-ns"}
    assert backend.commits == 2
    assert backend.rollbacks == 0


async def test_bulk_create_fails_when_outbox_enqueue_fails(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    backend.webhooks.configure_raise(RuntimeError("dispatcher unavailable"))

    resp = await client.post(
        "/v1/memories/bulk",
        json={"memories": [_memory("committed one"), _memory("committed two")]},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["created"] == 0
    assert data["memory_ids"] == []
    assert data["errors"] == [
        "[0] dispatcher unavailable",
        "[1] dispatcher unavailable",
    ]
    assert [name for name, _payload in backend.memories.calls] == [
        "insert_memory",
        "insert_memory",
    ]
    assert [name for name, _payload in backend.webhooks.calls] == [
        "dispatch_event",
        "dispatch_event",
    ]
    assert backend.commits == 0
    assert backend.rollbacks == 2

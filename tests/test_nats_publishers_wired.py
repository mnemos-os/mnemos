"""Endpoint wiring regressions for v4.2 NATS publishes."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from mnemos.api.dependencies import UserContext, get_current_user
from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio

NODE_NAME = "test-node"


def _alice(namespace: str = "alice.ns") -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace=namespace,
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


@pytest.fixture
def publish_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr("mnemos.nats.publisher.publish_event", mock)
    monkeypatch.setattr("mnemos.nats.publish_event", mock)
    monkeypatch.setattr("mnemos.nats.client.get_node_name", lambda: NODE_NAME)
    return mock


def _memory_row(memory_id: str = "mem_1") -> dict:
    now = datetime(2026, 4, 30, 12, 0, 0)
    return {
        "id": memory_id,
        "content": "remember this",
        "category": "facts",
        "subcategory": None,
        "created": now,
        "updated": now,
        "metadata": {},
        "quality_rating": 75,
        "compressed_content": None,
        "verbatim_content": "remember this",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice.ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


async def test_create_memory_publishes_memory_created(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    monkeypatch.setattr("mnemos.api.routes.memories.new_memory_id", lambda: "mem_create")
    backend.memories.configure_return("get_memory", _memory_row("mem_create"))

    resp = await client.post(
        "/v1/memories",
        json={"content": "remember this", "category": "facts"},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    publish_mock.assert_awaited_once()
    subject, payload = publish_mock.await_args.args
    assert subject == "mnemos.memory.created.alice_ns"
    assert payload == {
        "memory_id": "mem_create",
        "namespace": "alice.ns",
        "category": "facts",
        "source_node": NODE_NAME,
    }
    assert publish_mock.await_args.kwargs["msg_id"] == "mem_create.created"


async def test_bulk_create_publishes_memory_created_per_success(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
):
    install_fake_backend(monkeypatch)

    resp = await client.post(
        "/v1/memories/bulk",
        json={"memories": [{"content": "one"}, {"content": "two", "category": "notes"}]},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert publish_mock.await_count == 2
    assert [call.args[0] for call in publish_mock.await_args_list] == [
        "mnemos.memory.created.alice_ns",
        "mnemos.memory.created.alice_ns",
    ]
    memory_ids = resp.json()["memory_ids"]
    for call in publish_mock.await_args_list:
        assert set(call.args[1]) == {"memory_id", "namespace", "category", "source_node"}
        assert call.args[1]["namespace"] == "alice.ns"
        assert call.args[1]["source_node"] == NODE_NAME
    assert [call.kwargs["msg_id"] for call in publish_mock.await_args_list] == [
        f"{memory_ids[0]}.created",
        f"{memory_ids[1]}.created",
    ]


async def test_update_memory_publishes_memory_updated(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("update_memory", _memory_row("mem_update"))

    resp = await client.patch(
        "/v1/memories/mem_update",
        json={"category": "notes"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    publish_mock.assert_awaited_once()
    subject, payload = publish_mock.await_args.args
    assert subject == "mnemos.memory.updated.alice_ns"
    assert payload == {
        "memory_id": "mem_update",
        "namespace": "alice.ns",
        "category": "facts",
        "source_node": NODE_NAME,
    }
    assert publish_mock.await_args.kwargs["msg_id"].startswith("mem_update.updated.")


async def test_delete_memory_publishes_memory_deleted(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("delete_memory", _memory_row("mem_delete"))

    resp = await client.delete("/v1/memories/mem_delete", headers=auth_headers)

    assert resp.status_code == 204, resp.text
    publish_mock.assert_awaited_once_with(
        "mnemos.memory.deleted.alice_ns",
        {
            "memory_id": "mem_delete",
            "namespace": "alice.ns",
            "category": "facts",
            "source_node": NODE_NAME,
        },
        msg_id="mem_delete.deleted",
    )


async def test_consultation_publishes_completed(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
):
    resp = await client.post(
        "/v1/consultations",
        json={"prompt": "where is paris", "task_type": "reasoning", "mode": "auto"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    consultation_id = resp.json()["consultation_id"]
    publish_mock.assert_awaited_once()
    subject, payload = publish_mock.await_args.args
    assert subject == "mnemos.consultation.completed.alice_ns"
    assert payload == {
        "consultation_id": consultation_id,
        "task_type": "reasoning",
        "mode": "auto",
        "winning_muse": "openai",
        "consensus_score": 0.95,
        "namespace": "alice.ns",
        "user_id": "alice",
        "source_node": NODE_NAME,
    }
    assert publish_mock.await_args.kwargs["msg_id"] == f"{consultation_id}.completed"


async def test_webhook_create_publishes_subscription_created(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("mnemos.api.routes.webhooks._validate_url", AsyncMock())

    resp = await client.post(
        "/v1/webhooks",
        json={"url": "https://hooks.example.com/mnemos", "events": ["memory.created"]},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    webhook_id = resp.json()["id"]
    publish_mock.assert_awaited_once()
    subject, payload = publish_mock.await_args.args
    assert subject == "mnemos.webhook.subscription.created.alice_ns"
    assert payload == {
        "webhook_id": webhook_id,
        "url": "https://hooks.example.com/mnemos",
        "event_types": ["memory.created"],
        "namespace": "alice.ns",
        "owner_id": "alice",
        "source_node": NODE_NAME,
    }
    assert publish_mock.await_args.kwargs["msg_id"] == f"webhook.{webhook_id}.subscription.created"

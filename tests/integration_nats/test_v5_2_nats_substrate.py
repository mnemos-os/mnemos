"""Live-NATS coverage for the v5.2.0 NATS substrate v0.3 paths."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any

import pytest

from mnemos.nats import client as nats_client
from mnemos.persistence import nats_events
from mnemos.workers import federation_memory_nats_consumer as federation_consumer
from mnemos.workers import webhooks_dispatch_nats_consumer as webhook_consumer

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MNEMOS_NATS_TEST_URL"),
        reason="MNEMOS_NATS_TEST_URL not set",
    ),
]


async def _add_test_stream(js: Any, stream_name: str, subject: str) -> None:
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    await js.add_stream(
        config=StreamConfig(
            name=stream_name,
            subjects=[subject],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.MEMORY,
            max_age=60,
            max_msgs=100,
        )
    )


async def test_webhooks_outbox_v03_publish_and_consume(
    js,
    stream_cleanup: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = f"tenant_{stream_cleanup.lower()}"
    subject = nats_events.webhook_outbox_subject(tenant=tenant, event_type="memory.created")
    await _add_test_stream(js, stream_cleanup, subject)
    monkeypatch.setenv("MNEMOS_NATS_WEBHOOKS_ENABLED", "true")
    monkeypatch.setattr(nats_client, "_jetstream", js)
    monkeypatch.setattr(nats_events.nats_client, "get_node_name", lambda: "publisher-node")
    monkeypatch.setattr(webhook_consumer, "get_node_name", lambda: "consumer-node")

    sub = await js.subscribe(
        subject,
        durable=f"webhooks_v52_{stream_cleanup.lower()}",
        stream=stream_cleanup,
    )

    await nats_events.publish_webhook_outbox_insert(
        delivery_id="11111111-1111-4111-8111-111111111111",
        subscription_id="22222222-2222-4222-8222-222222222222",
        event_type="memory.created",
        url="https://hooks.example.com/mnemos",
        payload_hash="abc123",
        namespace="shared.ns",
        owner_id=tenant,
    )

    msg = await sub.next_msg(timeout=2)
    scheduled: list[asyncio.Task[bool]] = []
    recorded: list[tuple[str, str]] = []
    attempted: list[str] = []

    async def record_dispatch(_pool, event_id: str, msg_subject: str) -> bool:
        recorded.append((event_id, msg_subject))
        return True

    async def attempt(delivery_id: str, *, pool) -> bool:
        attempted.append(delivery_id)
        return True

    def schedule(coro):
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    await webhook_consumer.handle_message(
        None,
        msg,
        schedule=schedule,
        attempt=attempt,
        record_dispatch=record_dispatch,
    )
    await asyncio.gather(*scheduled)
    await msg.ack()

    assert msg.subject == subject
    assert recorded == [("11111111-1111-4111-8111-111111111111", subject)]
    assert attempted == ["11111111-1111-4111-8111-111111111111"]


async def test_federation_memory_v03_publish_and_consume(
    js,
    nats_url: str,
    stream_cleanup: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = f"ns_{stream_cleanup.lower()}"
    subject = nats_events.federation_memory_subject(namespace)
    await _add_test_stream(js, stream_cleanup, subject)
    monkeypatch.setenv("MNEMOS_NATS_FEDERATION_ENABLED", "true")
    monkeypatch.setattr(nats_client, "_jetstream", js)
    monkeypatch.setattr(nats_events.nats_client, "get_node_name", lambda: "upstream-node")
    monkeypatch.setattr(federation_consumer, "get_node_name", lambda: "local-node")

    sub = await js.subscribe(
        subject,
        durable=f"federation_v52_{stream_cleanup.lower()}",
        stream=stream_cleanup,
    )

    updated = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    event = nats_events.federation_memory_upsert_event(
        {
            "id": "mem_live_nats",
            "content": "remote memory body",
            "verbatim_content": "remote memory body",
            "category": "facts",
            "subcategory": None,
            "metadata": {"k": "v"},
            "quality_rating": 88,
            "owner_id": "alice",
            "namespace": namespace,
            "permission_mode": 604,
            "source_model": None,
            "source_provider": None,
            "source_session": None,
            "source_agent": "test",
            "created": updated,
            "updated": updated,
            "archived_at": None,
            "federation_source": None,
            "deleted_at": None,
            "consolidated_into": None,
        }
    )
    assert event is not None
    await nats_events.publish_federation_memory_upsert_event(event)

    msg = await sub.next_msg(timeout=2)
    peer = federation_consumer.FederationMemoryPeer(
        name="peer-a",
        nats_url=nats_url,
        namespace_filter=(namespace,),
        category_filter=("facts",),
    )
    recorded: list[tuple[str, str]] = []
    stored: list[dict[str, Any]] = []

    async def record_dispatch(_pool, event_id: str, msg_subject: str) -> bool:
        recorded.append((event_id, msg_subject))
        return True

    async def store_memory(_pool, _peer, memory: dict[str, Any]) -> None:
        stored.append(memory)

    await federation_consumer.handle_message(
        None,
        peer,
        msg,
        record_dispatch=record_dispatch,
        store_memory=store_memory,
    )
    await msg.ack()

    assert msg.subject == subject
    assert recorded == [(event["event_id"], subject)]
    assert stored[0]["id"] == "mem_live_nats"
    assert stored[0]["content"] == "remote memory body"
    assert stored[0]["namespace"] == namespace

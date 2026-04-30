"""Ingest NATS publish intent regressions."""
from __future__ import annotations

import pytest

from mnemos.api.routes import memories

pytestmark = pytest.mark.asyncio


class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"


async def test_insert_memory_returns_nats_intent_without_publishing_inline(monkeypatch):
    dispatch_calls = []
    publish_calls = []

    async def dispatch(event_type, payload, *, conn, owner_id, namespace):
        dispatch_calls.append((event_type, payload, conn, owner_id, namespace))

    async def publish_event(*args, **kwargs):
        publish_calls.append((args, kwargs))

    monkeypatch.setattr("mnemos.webhooks.dispatcher.dispatch", dispatch)
    monkeypatch.setattr("mnemos.nats.publish_event", publish_event)
    monkeypatch.setattr("mnemos.nats.client.get_node_name", lambda: "test-node")

    conn = _Conn()
    intents = await memories._insert_memory_with_created_webhook(
        conn=conn,
        mem_id="mem_ingest",
        content="secret session body",
        category="session_activity",
        owner_id="alice",
        namespace="alice.ns",
    )

    assert len(conn.executed) == 1
    assert dispatch_calls[0][0] == "memory.created"
    assert publish_calls == []
    assert intents == [
        (
            "mnemos.memory.created.alice_ns",
            {
                "memory_id": "mem_ingest",
                "namespace": "alice.ns",
                "category": "session_activity",
                "source_node": "test-node",
            },
            "mem_ingest.created",
        )
    ]
    assert "secret session body" not in intents[0][1].values()

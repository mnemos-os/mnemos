"""Federation NATS push consumer regressions."""

from __future__ import annotations

import asyncio
import json
import pytest

from mnemos.core import config
from mnemos.federation import nats_consumer as consumer

pytestmark = pytest.mark.asyncio


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "DELETE 1"


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _PoolCtx(self.conn)


class _FakeMsg:
    def __init__(self, subject: str, payload):
        self.subject = subject
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.acked = False

    async def ack(self):
        self.acked = True


class _FakeSubscription:
    def __init__(self, messages):
        self.messages = list(messages)

    async def next_msg(self, timeout=1):
        await asyncio.sleep(0)
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise asyncio.TimeoutError()


class _FakeJetStream:
    def __init__(self, messages):
        self.messages = messages
        self.subscribe_calls = []

    async def subscribe(self, subject, **kwargs):
        self.subscribe_calls.append((subject, kwargs))
        return _FakeSubscription(self.messages)


def _peer() -> consumer.FederationNatsPeer:
    return consumer.FederationNatsPeer(
        name="pythia",
        nats_url="nats://192.168.207.67:4222",
        nats_token="token",
        subjects=("mnemos.memory.created.>",),
    )


async def test_consumer_ignores_empty_federation_nats_peers(monkeypatch):
    monkeypatch.delenv("MNEMOS_FEDERATION_NATS_PEERS", raising=False)
    config._reset_settings_for_tests()
    try:
        assert consumer.configured_nats_peers() == []
        await consumer.run_configured_consumers(_FakePool())
    finally:
        config._reset_settings_for_tests()


async def test_consumer_reads_message_and_calls_store_with_federation_shape():
    pool = _FakePool()
    msg = _FakeMsg(
        "mnemos.memory.created.default",
        {
            "memory_id": "mem_123",
            "content": "remote note",
            "category": "facts",
            "subcategory": "systems",
            "namespace": "upstream.ns",
            "metadata": {"k": "v"},
            "updated": "2026-04-30T12:00:00Z",
        },
    )
    calls = []

    async def store(conn, peer_name, memories):
        calls.append((conn, peer_name, memories))
        return (1, 0)

    await consumer.handle_message(pool, _peer(), msg, store=store)

    assert calls[0][0] is pool.conn
    assert calls[0][1] == "pythia"
    assert calls[0][2] == [
        {
            "id": "mem_123",
            "content": "remote note",
            "verbatim_content": "remote note",
            "category": "facts",
            "subcategory": "systems",
            "namespace": "upstream.ns",
            "quality_rating": 75,
            "metadata": {"k": "v", "fed_origin": "pythia"},
            "source_model": None,
            "source_provider": None,
            "source_session": None,
            "source_agent": "federation-nats",
            "created": None,
            "updated": "2026-04-30T12:00:00Z",
        }
    ]


async def test_hard_fault_on_single_bad_event_does_not_kill_loop(monkeypatch):
    pool = _FakePool()
    bad = _FakeMsg("mnemos.memory.created.default", b"not json")
    good = _FakeMsg("mnemos.memory.updated.default", {"memory_id": "mem_good", "content": "ok"})
    sub = _FakeSubscription([bad, good])
    calls = []

    async def store(conn, peer_name, memories):
        calls.append(memories[0]["id"])
        return (0, 1)

    original_handle_message = consumer.handle_message

    async def handle_message(pool_arg, peer_arg, msg_arg):
        await original_handle_message(pool_arg, peer_arg, msg_arg, store=store)

    monkeypatch.setattr(consumer, "handle_message", handle_message)
    task = asyncio.create_task(consumer._consume_subscription(pool, _peer(), sub))
    for _ in range(20):
        if calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls == ["mem_good"]
    assert bad.acked is True
    assert good.acked is True


async def test_memory_deleted_event_triggers_delete_path():
    pool = _FakePool()
    calls = []

    async def delete(pool_arg, peer_name, memory_id):
        calls.append((pool_arg, peer_name, memory_id))
        return 1

    await consumer.handle_message(
        pool,
        _peer(),
        _FakeMsg("mnemos.memory.deleted.default", {"memory_id": "mem_dead"}),
        delete=delete,
    )

    assert calls == [(pool, "pythia", "mem_dead")]


async def test_fake_jetstream_subscription_uses_deliver_policy_new_shape():
    js = _FakeJetStream([_FakeMsg("mnemos.memory.created.default", {"memory_id": "m"})])

    sub = await consumer._subscribe(js, _peer(), "mnemos.memory.created.>")

    assert isinstance(sub, _FakeSubscription)
    subject, kwargs = js.subscribe_calls[0]
    assert subject == "mnemos.memory.created.>"
    assert kwargs["durable"].startswith("mnemos_federation_pythia_")
    assert kwargs["stream"] == "MNEMOS_MEMORY"
    config_obj = kwargs["config"]
    if config_obj is not None:
        assert "NEW" in str(getattr(config_obj, "deliver_policy", "NEW"))

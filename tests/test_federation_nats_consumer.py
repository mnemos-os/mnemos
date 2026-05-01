"""Federation NATS push consumer regressions."""

from __future__ import annotations

import asyncio
import json
import pytest

from mnemos.core import config
from mnemos.domain import federation as federation_domain
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


async def test_consumer_fetches_body_from_authorized_feed_before_store():
    pool = _FakePool()
    msg = _FakeMsg(
        "mnemos.memory.created.default",
        {"memory_id": "mem_123", "category": "facts", "namespace": "upstream.ns"},
    )
    fetched = [{"id": "mem_123", "content": "remote note", "category": "facts"}]
    calls = []

    async def fetch(peer, memory_id):
        assert peer.name == "pythia"
        assert memory_id == "mem_123"
        return fetched

    async def store(conn, peer_name, memories):
        calls.append((conn, peer_name, memories))
        return (1, 0)

    await consumer.handle_message(pool, _peer(), msg, store=store, fetch=fetch)

    assert calls == [(pool.conn, "pythia", fetched)]


async def test_consumer_fetches_memory_by_id_before_store(monkeypatch):
    pool = _FakePool()
    msg = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_x"})
    body = {"id": "mem_x", "content": "remote note", "category": "facts"}
    calls = []

    async def pull_memory_by_id(base_url, auth_token, memory_id, namespace_filter, category_filter):
        assert base_url == "https://proteus.example"
        assert auth_token == "feed-token"
        assert memory_id == "mem_x"
        assert namespace_filter == ["shared"]
        assert category_filter == ["facts"]
        return [body]

    async def store(conn, peer_name, memories):
        calls.append((conn, peer_name, memories))
        return (1, 0)

    peer = consumer.FederationNatsPeer(
        name="proteus",
        nats_url="nats://example:4222",
        base_url="https://proteus.example",
        auth_token="feed-token",
        namespace_filter=("shared",),
        category_filter=("facts",),
    )
    monkeypatch.setattr(consumer, "pull_memory_by_id", pull_memory_by_id)

    await consumer.handle_message(pool, peer, msg, store=store)

    assert calls == [(pool.conn, "proteus", [body])]


async def test_self_loop_event_is_skipped_and_remote_event_is_processed(monkeypatch):
    monkeypatch.setattr(consumer, "get_node_name", lambda: "pythia")
    pool = _FakePool()
    calls = []

    async def store(conn, peer_name, memories):
        calls.append((conn, peer_name, memories))
        return (1, 0)

    await consumer.handle_message(
        pool,
        _peer(),
        _FakeMsg(
            "mnemos.memory.created.default",
            {"memory_id": "mem_self", "content": "self", "source_node": "pythia"},
        ),
        store=store,
    )
    assert calls == []

    await consumer.handle_message(
        pool,
        _peer(),
        _FakeMsg(
            "mnemos.memory.created.default",
            {"memory_id": "mem_remote", "source_node": "proteus"},
        ),
        store=store,
        fetch=lambda peer, memory_id: _async_list([{"id": memory_id, "content": "remote"}]),
    )

    assert len(calls) == 1
    assert calls[0][0] is pool.conn
    assert calls[0][1] == "pythia"
    assert calls[0][2][0]["id"] == "mem_remote"


async def test_poison_event_is_acked_and_does_not_kill_loop(monkeypatch):
    pool = _FakePool()
    bad = _FakeMsg("mnemos.memory.created.default", b"not json")
    good = _FakeMsg("mnemos.memory.updated.default", {"memory_id": "mem_good", "content": "ok"})
    sub = _FakeSubscription([bad, good])
    calls = []

    async def store(conn, peer_name, memories):
        calls.append(memories[0]["id"])
        return (0, 1)

    original_handle_message = consumer.handle_message

    async def fetch(peer_arg, memory_id):
        return [{"id": memory_id, "content": "ok"}]

    async def handle_message(pool_arg, peer_arg, msg_arg):
        await original_handle_message(pool_arg, peer_arg, msg_arg, store=store, fetch=fetch)

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
    # Default (single-replica) shape: NO queue arg.
    assert "queue" not in kwargs
    config_obj = kwargs["config"]
    if config_obj is not None:
        assert "NEW" in str(getattr(config_obj, "deliver_policy", "NEW"))
        # No deliver_group when queue_group is empty.
        assert getattr(config_obj, "deliver_group", None) is None


async def test_queue_durable_name_distinct_namespace_from_legacy():
    """Queue-mode durable lives in a separate namespace so legacy and
    queue-mode consumers never collide on the same JetStream consumer
    object during a partial-fleet rollout."""
    legacy = consumer._durable_name("pythia", "mnemos.memory.created.>")
    q1 = consumer._queue_durable_name("fed_pool", "pythia", "mnemos.memory.created.>")
    assert legacy != q1
    assert q1.startswith("mnemos_federation_q_fed_pool_")
    assert not legacy.startswith("mnemos_federation_q_")


async def test_queue_durable_name_collision_resistant_under_long_inputs():
    """v4.2.0a8 round-3: codex Finding 2 — naive truncation could
    cut the subject suffix off and merge created/updated/deleted
    consumers for the same peer. Hash suffix preserves uniqueness
    even when the readable middle gets truncated."""
    long_group = "a_very_long_team_pool_for_demo_2026_q4_runtime"
    long_peer = "us-west-2-prod-cluster-3-shard-pythia-tier-1"
    d_created = consumer._queue_durable_name(long_group, long_peer, "mnemos.memory.created.>")
    d_updated = consumer._queue_durable_name(long_group, long_peer, "mnemos.memory.updated.>")
    d_deleted = consumer._queue_durable_name(long_group, long_peer, "mnemos.memory.deleted.>")

    # All three subjects must produce DISTINCT durable names — even
    # though the readable prefix overlaps heavily.
    assert d_created != d_updated
    assert d_updated != d_deleted
    assert d_created != d_deleted

    # All within the 128-char NATS durable cap.
    assert len(d_created) <= 128
    assert len(d_updated) <= 128
    assert len(d_deleted) <= 128

    # Distinct peers must also produce distinct durables.
    other = consumer._queue_durable_name(long_group, "different-peer", "mnemos.memory.created.>")
    assert d_created != other


async def test_subscribe_with_queue_group_sets_queue_and_deliver_group():
    """v4.2.0a8: Audit Finding 5 — multi-replica federation receiver
    via JetStream queue-group sharding.

    Per nats-py 2.14: ``js.subscribe(queue=Q, durable=D)`` requires
    ``D == Q`` (it treats the queue name as the durable). And the
    consumer's ``deliver_group`` must match the subscriber's queue.
    So all three end up the same string.

    Distinct ``_q_<group>_`` namespace from the legacy
    ``mnemos_federation_<peer>_<subject>`` durable so legacy a7-shape
    replicas and a8-queue-mode replicas can coexist on the same
    broker without colliding on the consumer object.
    """
    js = _FakeJetStream([])

    await consumer._subscribe(
        js,
        _peer(),
        "mnemos.memory.created.>",
        queue_group="fed_pool",
    )

    subject, kwargs = js.subscribe_calls[0]
    assert subject == "mnemos.memory.created.>"
    expected_durable = consumer._queue_durable_name(
        "fed_pool", "pythia", "mnemos.memory.created.>"
    )
    assert kwargs["durable"] == expected_durable
    assert kwargs["queue"] == expected_durable, (
        "nats-py requires queue == durable for queue-mode subscribe"
    )
    # Distinct namespace from legacy durable.
    assert expected_durable.startswith("mnemos_federation_q_fed_pool_")
    legacy_durable = consumer._durable_name("pythia", "mnemos.memory.created.>")
    assert expected_durable != legacy_durable

    config_obj = kwargs["config"]
    assert config_obj is not None
    assert getattr(config_obj, "deliver_group", None) == expected_durable


async def _async_list(value):
    return value


async def test_transient_store_error_is_not_acked(monkeypatch):
    pool = _FakePool()
    msg = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_retry"})
    sub = _FakeSubscription([msg])

    async def fetch(peer, memory_id):
        return [{"id": memory_id, "content": "retry me"}]

    async def store(conn, peer_name, memories):
        raise RuntimeError("db unavailable")

    original_handle_message = consumer.handle_message

    async def handle_message(pool_arg, peer_arg, msg_arg):
        await original_handle_message(pool_arg, peer_arg, msg_arg, store=store, fetch=fetch)

    monkeypatch.setattr(consumer, "handle_message", handle_message)
    task = asyncio.create_task(consumer._consume_subscription(pool, _peer(), sub))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert msg.acked is False


async def test_fetch_authorized_memories_uses_federation_memory_endpoint(monkeypatch):
    peer = consumer.FederationNatsPeer(
        name="pythia",
        nats_url="nats://example:4222",
        base_url="https://peer.example",
        auth_token="feed-token",
        namespace_filter=("shared",),
        category_filter=("facts",),
    )
    calls = []

    async def pull(base_url, auth_token, memory_id, namespace_filter, category_filter):
        calls.append((base_url, auth_token, memory_id, namespace_filter, category_filter))
        return [{"id": memory_id}]

    monkeypatch.setattr(consumer, "pull_memory_by_id", pull)

    assert await consumer._fetch_authorized_memories(peer, "mem_1") == [{"id": "mem_1"}]
    assert calls == [("https://peer.example", "feed-token", "mem_1", ["shared"], ["facts"])]


async def test_pull_memory_by_id_uses_explicit_endpoint(monkeypatch):
    calls = []

    class _Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "mem_1", "content": "ok"}

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params, headers):
            calls.append((url, params, headers, self.kwargs))
            return _Response()

    monkeypatch.setattr(federation_domain.httpx, "AsyncClient", _Client)

    memories = await federation_domain.pull_memory_by_id(
        "https://peer.example/",
        "feed-token",
        "mem_1",
        ["shared"],
        ["facts"],
    )

    assert memories == [{"id": "mem_1", "content": "ok"}]
    assert calls == [
        (
            "https://peer.example/v1/federation/memory/mem_1",
            {"namespace": "shared", "category": "facts"},
            {"Authorization": "Bearer feed-token"},
            {"timeout": federation_domain.FEDERATION_HTTP_TIMEOUT},
        )
    ]


# --- v4.2.0a7 round-3: receive/handle/ack scope-split coverage ---


async def test_handler_runtime_error_does_not_kill_loop(monkeypatch):
    """Non-DB handler exception (e.g. RuntimeError from store path) must
    stay local — the NATS subscription is healthy, JetStream redelivers."""
    pool = _FakePool()
    first = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_a"})
    second = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_b"})
    sub = _FakeSubscription([first, second])
    seen: list[str] = []

    async def handle_message(pool_arg, peer_arg, msg_arg):
        payload = json.loads(msg_arg.data.decode())
        seen.append(payload["memory_id"])
        if payload["memory_id"] == "mem_a":
            raise RuntimeError("transient downstream error")

    monkeypatch.setattr(consumer, "handle_message", handle_message)
    task = asyncio.create_task(consumer._consume_subscription(pool, _peer(), sub))
    for _ in range(50):
        if "mem_b" in seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert seen == ["mem_a", "mem_b"], "loop must survive handler error and process mem_b"
    assert first.acked is False, "errored handler must not ack — JetStream redelivers"
    assert second.acked is True


async def test_handler_interface_error_does_not_kill_loop(monkeypatch):
    """asyncpg.InterfaceError from a closed-connection handler path must
    stay local. Pre-fix this would have escaped to reconnect NATS."""
    import asyncpg

    pool = _FakePool()
    first = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_x"})
    second = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_y"})
    sub = _FakeSubscription([first, second])
    seen: list[str] = []

    async def handle_message(pool_arg, peer_arg, msg_arg):
        payload = json.loads(msg_arg.data.decode())
        seen.append(payload["memory_id"])
        if payload["memory_id"] == "mem_x":
            raise asyncpg.InterfaceError("connection is closed")

    monkeypatch.setattr(consumer, "handle_message", handle_message)
    task = asyncio.create_task(consumer._consume_subscription(pool, _peer(), sub))
    for _ in range(50):
        if "mem_y" in seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert seen == ["mem_x", "mem_y"]
    assert first.acked is False
    assert second.acked is True


async def test_receive_error_escapes_for_reconnect(monkeypatch):
    """Non-timeout next_msg failure (e.g. broker shutdown) must escape
    out of _consume_subscription so the outer drain+reconnect path runs."""
    pool = _FakePool()
    sub = _FakeSubscription([ConnectionResetError("broker gone")])

    async def handle_message(pool_arg, peer_arg, msg_arg):  # noqa: ARG001
        pytest.fail("handler must not be called when next_msg fails")

    monkeypatch.setattr(consumer, "handle_message", handle_message)

    with pytest.raises(ConnectionResetError):
        await consumer._consume_subscription(pool, _peer(), sub)


async def test_ack_error_escapes_for_reconnect(monkeypatch):
    """A failure inside _ack is a NATS issue (the broker is what we're
    acking to). Must escape so the reconnect path runs."""
    pool = _FakePool()
    msg = _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_ok"})
    sub = _FakeSubscription([msg])

    async def handle_message(pool_arg, peer_arg, msg_arg):
        return None

    async def broken_ack(msg_arg):
        raise ConnectionResetError("ack send failed")

    monkeypatch.setattr(consumer, "handle_message", handle_message)
    monkeypatch.setattr(consumer, "_ack", broken_ack)

    with pytest.raises(ConnectionResetError):
        await consumer._consume_subscription(pool, _peer(), sub)

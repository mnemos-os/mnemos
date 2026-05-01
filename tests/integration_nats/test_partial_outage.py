"""Live-broker partial-outage tests (Audit Finding 11).

The unit-level fakes in tests/test_federation_nats_consumer.py +
tests/test_webhook_nats_trigger.py prove the consume-loop
reconnect+backoff path in isolation by hand-feeding exceptions.
These tests close the gap by driving the PRODUCTION
``mnemos.federation.nats_consumer.consumer_loop`` against a real
nats-server subprocess that the test owns and can stop/restart at
will.

The contract under test isn't "we can call nats.connect twice" —
it's "the production consumer_loop survives broker outages and
catches up afterwards." So the tests:

  1. Start consumer_loop as an asyncio task pointed at the
     managed broker.
  2. Publish messages, observe the test's fake-store records them.
  3. Trigger an outage (broker restart / consumer deletion).
  4. Publish more messages, observe the consumer_loop drained,
     reconnected with backoff, and the new messages still land.

The store + handler are faked at the boundary the consumer_loop
uses (``handle_message`` -> ``store`` callback) so we don't need
a real Postgres for this test. The consumer's reconnect/drain/
backoff machinery IS the production code path being exercised.

Skips with a clear message when nats-server isn't installed.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

import pytest

from mnemos.federation import nats_consumer as consumer
from mnemos.federation.nats_consumer import FederationNatsPeer

pytestmark = pytest.mark.asyncio


async def _ensure_test_stream(js, name: str, subjects: list[str]):
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    config = StreamConfig(
        name=name,
        subjects=subjects,
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=int(timedelta(minutes=5).total_seconds()),
        max_bytes=4 * 1024 * 1024,
        duplicate_window=int(timedelta(seconds=30).total_seconds()),
    )
    await js.add_stream(config=config)


class _FakePool:
    """asyncpg.Pool stand-in for the federation handler.

    consumer_loop calls ``handle_message`` which acquires a
    connection from the pool and calls a ``store`` callback. We
    monkey-patch ``handle_message`` to a controllable shape that
    appends payloads to a list, so the consumer_loop's drain /
    backoff / reconnect machinery is the only piece that has to
    actually work end-to-end against the broker.
    """
    def acquire(self):
        return _PoolCtx()


class _PoolCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return False


async def _spin_consumer(
    monkeypatch,
    managed_broker,
    *,
    peer_name: str = "outage_test_peer",
    subjects: tuple[str, ...] = ("mnemos.memory.created.>",),
):
    """Spin up consumer_loop pointed at managed_broker; capture
    handler-received payloads.

    Returns ``(task, received, peer)`` so the test can:
      * await on `received` waiting for new entries
      * trigger broker outages
      * cancel the task in teardown
    """
    received: list[dict] = []

    async def _fake_handle_message(pool, peer, msg, store=None, fetch=None, delete=None):
        # Production handle_message calls fetch+store. We just
        # collect the raw message subject + data so the test can
        # assert "consumer_loop saw this message after reconnect."
        import json as _json
        try:
            payload = _json.loads(msg.data.decode())
        except Exception:
            payload = {"_raw": msg.data.decode("utf-8", errors="replace")}
        received.append({"subject": msg.subject, "data": payload})

    monkeypatch.setattr(consumer, "handle_message", _fake_handle_message)

    peer = FederationNatsPeer(
        name=peer_name,
        nats_url=managed_broker.url,
        subjects=subjects,
    )
    pool = _FakePool()
    # consumer_loop's reconnect backoff caps at retry_seconds. Use
    # a short cap so test runtime stays reasonable while still
    # exercising the backoff path.
    task = asyncio.create_task(consumer.consumer_loop(pool, peer, retry_seconds=2.0))
    # Give the loop a moment to connect + subscribe.
    await asyncio.sleep(0.5)
    return task, received, peer


async def _wait_for_received_count(received: list, target: int, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while len(received) < target and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _connection_to(managed_broker):
    import nats
    nc = await nats.connect(servers=[managed_broker.url])
    try:
        yield nc
    finally:
        with contextlib.suppress(Exception):
            await nc.drain()


async def test_consumer_loop_recovers_after_broker_restart(monkeypatch, managed_broker):
    """Production consumer_loop must survive a broker restart.

    Pre-outage: consumer_loop receives 3 messages.
    Outage: broker is hard-killed and restarted on the same store_dir.
    Post-outage: consumer_loop reconnects via OUR backoff path
    (auto-reconnect at the nats-py layer is disabled), sees 3 new
    messages.

    Codex round-2: naive version could pass via nats-py's
    internal allow_reconnect=True. We override the consumer's
    connect callable to disable auto-reconnect AND count spawn
    attempts, so a regression in our drain+ReconnectBackoff path
    is forced to fail rather than be masked.
    """
    import nats

    # Set up the federation stream the consumer_loop expects.
    async with _connection_to(managed_broker) as nc:
        js = nc.jetstream()
        await _ensure_test_stream(js, "MNEMOS_MEMORY", ["mnemos.memory.>"])

    received: list[dict] = []
    spawn_count = 0

    async def _fake_handle_message(pool, peer, msg, store=None, fetch=None, delete=None):
        import json as _json
        try:
            payload = _json.loads(msg.data.decode())
        except Exception:
            payload = {"_raw": msg.data.decode("utf-8", errors="replace")}
        received.append({"subject": msg.subject, "data": payload})

    monkeypatch.setattr(consumer, "handle_message", _fake_handle_message)

    async def _no_autorecon_connect(peer):
        nonlocal spawn_count
        spawn_count += 1
        client = await nats.connect(
            servers=[peer.nats_url],
            allow_reconnect=False,
        )
        return client, client.jetstream()

    peer = FederationNatsPeer(
        name="outage_restart_test",
        nats_url=managed_broker.url,
    )
    task = asyncio.create_task(
        consumer.consumer_loop(
            _FakePool(), peer, retry_seconds=2.0, connect=_no_autorecon_connect
        )
    )
    await asyncio.sleep(0.5)

    try:
        # Pre-outage: publish + observe.
        async with _connection_to(managed_broker) as pub_nc:
            pub_js = pub_nc.jetstream()
            for i in range(3):
                await pub_js.publish(
                    "mnemos.memory.created.default",
                    f'{{"memory_id":"pre-{i}"}}'.encode(),
                )
        await _wait_for_received_count(received, 3, timeout=5.0)
        assert len(received) == 3
        assert sorted(r["data"]["memory_id"] for r in received) == ["pre-0", "pre-1", "pre-2"]
        baseline_spawn_count = spawn_count

        # Outage: async-restart (we're in a running event loop;
        # sync restart() would call asyncio.run() and deadlock).
        await managed_broker.async_restart()

        async with _connection_to(managed_broker) as nc2:
            js2 = nc2.jetstream()

            # Consumer_loop should be in reconnect-backoff. With
            # retry_seconds=2.0 + jitter the next spawn attempt is
            # within ~0-2s; the second attempt within ~0-4s.
            await asyncio.sleep(3.0)

            for i in range(3):
                await js2.publish(
                    "mnemos.memory.created.default",
                    f'{{"memory_id":"post-{i}"}}'.encode(),
                )

        await _wait_for_received_count(received, 6, timeout=15.0)

        # PROOF that consumer_loop ran the OUTER reconnect: spawn
        # count must have increased post-restart. With auto-reconnect
        # disabled the only way to re-establish the connection is
        # consumer_loop's drain+backoff+spawn cycle.
        assert spawn_count > baseline_spawn_count, (
            "consumer_loop did NOT spawn a fresh connection after "
            "broker restart. baseline_spawn_count="
            f"{baseline_spawn_count}, final={spawn_count}. "
            "This indicates the drain+backoff path is broken — "
            "auto-reconnect at the nats-py layer was disabled, so "
            "only OUR loop could have re-established the connection."
        )

        assert len(received) == 6, (
            "consumer_loop did NOT recover from broker restart — "
            f"expected 6 received messages, got {len(received)}; "
            f"task state: done={task.done()}, exception="
            f"{task.exception() if task.done() else 'still running'}"
        )
        post_ids = sorted(
            r["data"]["memory_id"]
            for r in received
            if r["data"]["memory_id"].startswith("post-")
        )
        assert post_ids == ["post-0", "post-1", "post-2"]

        assert not task.done() or task.cancelled(), (
            "consumer_loop should be running OR cancelled, never "
            f"errored. task.done={task.done()}; "
            f"exception={task.exception() if task.done() else None}"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_consumer_loop_survives_handler_pause(monkeypatch, managed_broker):
    """The consumer_loop's three-scope split (v4.2.0a7 round-3)
    promises that handler-side errors stay LOCAL — they don't
    tear down the NATS subscription. This test exercises the
    promise: pause the handler artificially (RuntimeError on
    every call), then verify the consumer_loop keeps the
    subscription alive and resumes processing once the handler
    starts succeeding.
    """
    async with _connection_to(managed_broker) as nc:
        js = nc.jetstream()
        await _ensure_test_stream(js, "MNEMOS_MEMORY", ["mnemos.memory.>"])

    fail_count = 0
    received: list[dict] = []

    async def _flapping_handler(pool, peer, msg, store=None, fetch=None, delete=None):
        nonlocal fail_count
        import json as _json
        if fail_count < 5:
            fail_count += 1
            raise RuntimeError(f"synthetic handler failure {fail_count}")
        received.append(
            {"subject": msg.subject, "data": _json.loads(msg.data.decode())}
        )

    monkeypatch.setattr(consumer, "handle_message", _flapping_handler)

    peer = FederationNatsPeer(
        name="outage_handler_test",
        nats_url=managed_broker.url,
    )
    task = asyncio.create_task(
        consumer.consumer_loop(_FakePool(), peer, retry_seconds=2.0)
    )
    await asyncio.sleep(0.5)

    try:
        async with _connection_to(managed_broker) as pub_nc:
            pub_js = pub_nc.jetstream()
            # Publish 10 messages. The first 5 raise from the
            # handler (don't ack → JetStream redelivers after
            # ack-wait). After the handler stops failing, the
            # remaining messages plus the redelivered ones
            # accumulate in `received`.
            for i in range(10):
                await pub_js.publish(
                    "mnemos.memory.created.default",
                    f'{{"memory_id":"flap-{i}"}}'.encode(),
                )

        # Allow generous time for ack-wait redeliveries. The
        # JetStream default ack_wait is 30s, so 60s is enough for
        # 1-2 redelivery cycles on the first 5 messages.
        await _wait_for_received_count(received, 10, timeout=60.0)

        # The consumer_loop MUST still be alive — handler errors
        # are scope-2 (local), not scope-1/3 (NATS-issue, escapes).
        assert not task.done() or task.cancelled(), (
            "handler RuntimeError should NOT tear down the consumer "
            f"loop; got task.done={task.done()}, "
            f"exception={task.exception() if task.done() else None}"
        )

        # HARD assertion (codex round-2 finding 4): must see ALL 10
        # unique memory_ids exactly. Loose `>= 5` would mask data
        # loss — if the first 5 handler-failures had been
        # incorrectly acked, those messages would be lost forever
        # and the trailing 5 alone would still satisfy `>= 5`. The
        # exact-set assertion catches that regression.
        seen_ids = {r["data"]["memory_id"] for r in received}
        expected_ids = {f"flap-{i}" for i in range(10)}
        assert seen_ids == expected_ids, (
            f"handler-pause test must observe ALL 10 messages "
            f"after redelivery — JetStream's at-least-once contract "
            f"requires that unacked messages are redelivered, NOT "
            f"silently dropped. missing: {expected_ids - seen_ids}; "
            f"unexpected: {seen_ids - expected_ids}; "
            f"total received entries: {len(received)}"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_ensure_streams_safe_across_managed_broker_restart(managed_broker):
    """End-to-end exercise of mnemos.nats.client.ensure_streams
    across a broker restart. JetStream's store_dir persists
    streams across the cycle, so the second ensure_streams call
    hits the matching-redeclare path that pre-v4.2.0a9 round-9
    used to swallow drift errors silently.
    """
    import nats
    from mnemos.nats.client import ensure_streams

    nc = await nats.connect(servers=[managed_broker.url])
    try:
        result1 = await ensure_streams(nc.jetstream())
        assert result1 is True, "first ensure_streams must succeed on a fresh broker"
    finally:
        await nc.drain()

    # Restart the broker (preserving store_dir → streams persist).
    managed_broker.restart()

    # Second ensure_streams call against the restarted broker.
    nc2 = await nats.connect(servers=[managed_broker.url])
    try:
        result2 = await ensure_streams(nc2.jetstream())
        assert result2 is True, (
            "second ensure_streams against restarted broker (with "
            "persisted streams) must be idempotent"
        )
    finally:
        # Clean up canonical streams so they don't leak between
        # test runs against the same managed broker.
        js2 = nc2.jetstream()
        for stream in ("MNEMOS_MEMORY", "MNEMOS_CONSULTATION", "MNEMOS_WEBHOOK"):
            with contextlib.suppress(Exception):
                await js2.delete_stream(stream)
        await nc2.drain()

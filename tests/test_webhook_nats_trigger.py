"""Webhook NATS trigger consumer regressions."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from mnemos.webhooks import nats_trigger as trigger

pytestmark = pytest.mark.asyncio


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
    def __init__(self):
        self.subscribe_calls = []

    async def subscribe(self, subject, **kwargs):
        self.subscribe_calls.append((subject, kwargs))
        return _FakeSubscription([])


def _payload(delivery_id: str = "delivery_1") -> dict:
    return {
        "delivery_id": delivery_id,
        "subscription_id": "sub_1",
        "event_type": "memory.created",
        "url": "https://example.test/hook",
        "payload_hash": "abc123",
        "namespace": "default",
        "owner_id": "alice",
        "source_node": "pythia",
    }


async def test_trigger_consumer_calls_attempt_delivery_on_message_receipt():
    calls = []
    tasks = []

    async def attempt(delivery_id, *, pool):
        calls.append((delivery_id, pool))
        return True

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    pool = object()
    await trigger.handle_message(
        pool,
        _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("delivery_123")),
        schedule=schedule,
        attempt=attempt,
    )
    await asyncio.gather(*tasks)

    assert calls == [("delivery_123", pool)]


async def test_already_claimed_delivery_noops_gracefully():
    tasks = []

    async def attempt(delivery_id, *, pool):
        return False

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    await trigger.handle_message(
        object(),
        _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("already_claimed")),
        schedule=schedule,
        attempt=attempt,
    )

    assert await tasks[0] is False


async def test_bad_shape_message_logs_skips_and_does_not_kill_loop(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="mnemos.webhooks.nats_trigger")
    good_calls = []
    tasks = []

    async def attempt(delivery_id, *, pool):
        good_calls.append(delivery_id)
        return True

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(trigger, "_attempt_delivery", attempt)
    monkeypatch.setattr(trigger, "_schedule_attempt", schedule)
    bad = _FakeMsg("mnemos.webhook.delivery.queued.default", {"delivery_id": "missing_fields"})
    good = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("good_delivery"))
    sub = _FakeSubscription([bad, good])

    task = asyncio.create_task(trigger._consume_subscription(object(), sub))
    for _ in range(20):
        if good_calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, *tasks, return_exceptions=True)

    assert good_calls == ["good_delivery"]
    assert bad.acked is True
    assert good.acked is True
    assert "webhook nats trigger poison message" in caplog.text


async def test_stream_subscription_declared_with_right_subject():
    js = _FakeJetStream()

    sub = await trigger._subscribe(js)

    assert isinstance(sub, _FakeSubscription)
    subject, kwargs = js.subscribe_calls[0]
    assert subject == "mnemos.webhook.delivery.queued.>"
    assert kwargs["stream"] == "MNEMOS_WEBHOOK"
    # Per-node durable, no queue group — see _node_durable docstring.
    assert kwargs["durable"].startswith("mnemos_webhook_delivery_trigger_")
    assert "queue" not in kwargs
    config_obj = kwargs["config"]
    if config_obj is not None:
        assert "NEW" in str(getattr(config_obj, "deliver_policy", "NEW"))


async def test_transient_handle_error_is_not_acked(monkeypatch):
    msg = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("retry_delivery"))
    sub = _FakeSubscription([msg])

    async def handle_message(pool, msg_arg):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(trigger, "handle_message", handle_message)
    task = asyncio.create_task(trigger._consume_subscription(object(), sub))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert msg.acked is False


# --- v4.2.0a7 round-3: receive/handle/ack scope-split coverage ---


async def test_handler_runtime_error_does_not_kill_loop(monkeypatch):
    """Generic RuntimeError from handle_message must stay local — outbox
    polling fallback re-drives missed deliveries; tearing down the NATS
    subscription would just delay unrelated webhooks behind backoff."""
    first = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("first"))
    second = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("second"))
    sub = _FakeSubscription([first, second])
    seen: list[str] = []

    async def handle_message(pool, msg_arg):
        payload = json.loads(msg_arg.data.decode())
        seen.append(payload["delivery_id"])
        if payload["delivery_id"] == "first":
            raise RuntimeError("transient downstream error")

    monkeypatch.setattr(trigger, "handle_message", handle_message)
    task = asyncio.create_task(trigger._consume_subscription(object(), sub))
    for _ in range(50):
        if "second" in seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert seen == ["first", "second"]
    assert first.acked is False
    assert second.acked is True


async def test_handler_interface_error_does_not_kill_loop(monkeypatch):
    """asyncpg.InterfaceError from handler must stay local."""
    import asyncpg

    first = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("first"))
    second = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("second"))
    sub = _FakeSubscription([first, second])
    seen: list[str] = []

    async def handle_message(pool, msg_arg):
        payload = json.loads(msg_arg.data.decode())
        seen.append(payload["delivery_id"])
        if payload["delivery_id"] == "first":
            raise asyncpg.InterfaceError("connection is closed")

    monkeypatch.setattr(trigger, "handle_message", handle_message)
    task = asyncio.create_task(trigger._consume_subscription(object(), sub))
    for _ in range(50):
        if "second" in seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert seen == ["first", "second"]
    assert first.acked is False
    assert second.acked is True


async def test_receive_error_escapes_for_reconnect(monkeypatch):
    """Non-timeout next_msg failure must escape so the consumer_loop
    can drain + reconnect with backoff."""
    sub = _FakeSubscription([ConnectionResetError("broker gone")])

    async def handle_message(pool, msg_arg):  # noqa: ARG001
        pytest.fail("handler must not be called when next_msg fails")

    monkeypatch.setattr(trigger, "handle_message", handle_message)

    with pytest.raises(ConnectionResetError):
        await trigger._consume_subscription(object(), sub)


async def test_ack_error_escapes_for_reconnect(monkeypatch):
    """Ack failure is a NATS issue → must escape for reconnect."""
    msg = _FakeMsg("mnemos.webhook.delivery.queued.default", _payload("ok"))
    sub = _FakeSubscription([msg])

    async def handle_message(pool, msg_arg):
        return None

    async def broken_ack(msg_arg):
        raise ConnectionResetError("ack send failed")

    monkeypatch.setattr(trigger, "handle_message", handle_message)
    monkeypatch.setattr(trigger, "_ack", broken_ack)

    with pytest.raises(ConnectionResetError):
        await trigger._consume_subscription(object(), sub)

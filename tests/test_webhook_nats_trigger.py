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
    assert kwargs["durable"] == "mnemos_webhook_delivery_trigger"
    assert kwargs["queue"] == "mnemos_webhook_delivery_workers"
    config_obj = kwargs["config"]
    if config_obj is not None:
        assert "NEW" in str(getattr(config_obj, "deliver_policy", "NEW"))
        assert getattr(config_obj, "deliver_group", None) == "mnemos_webhook_delivery_workers"


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

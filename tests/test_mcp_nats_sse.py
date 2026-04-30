"""MCP HTTP NATS-backed SSE event bridge tests."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types

import pytest

pytestmark = pytest.mark.asyncio


class _FakeMsg:
    def __init__(self, subject: str, payload):
        self.subject = subject
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")


class _FakeSubscription:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.unsubscribed = False
        self.waiting = asyncio.Event()

    async def next_msg(self, timeout=1):
        await asyncio.sleep(0)
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.waiting.set()
        await asyncio.sleep(60)
        raise asyncio.TimeoutError()

    async def unsubscribe(self):
        self.unsubscribed = True


class _FakeJetStream:
    def __init__(self, subscriptions=None):
        self.subscriptions = list(subscriptions or [])
        self.subscribe_calls = []

    async def subscribe(self, subject, **kwargs):
        self.subscribe_calls.append((subject, kwargs))
        if self.subscriptions:
            return self.subscriptions.pop(0)
        return _FakeSubscription()


def _request(subjects: str | None = None):
    query_params = {}
    if subjects is not None:
        query_params["subjects"] = subjects
    return types.SimpleNamespace(query_params=query_params)


def _fresh_http(monkeypatch):
    from mnemos.core import config
    from mnemos.nats import client as nats_client

    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "alice:alice-token")
    config._reset_settings_for_tests()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    sys.modules.pop("mnemos.mcp.http", None)
    return importlib.import_module("mnemos.mcp.http")


async def test_sse_accept_opens_nats_subscription(monkeypatch):
    http = _fresh_http(monkeypatch)
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)

    response = await http.handle_nats_event_stream(
        _request("mnemos.memory.>,mnemos.consultation.completed.>")
    )
    await response.body_iterator.aclose()

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert [call[0] for call in js.subscribe_calls] == [
        "mnemos.memory.>",
        "mnemos.consultation.completed.>",
    ]


async def test_nats_message_becomes_sse_frame(monkeypatch):
    http = _fresh_http(monkeypatch)
    sub = _FakeSubscription([
        _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1", "source_node": "PYTHIA"})
    ])
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))

    response = await http.handle_nats_event_stream(_request())
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    text = frame.decode("utf-8")
    assert "event: mnemos.memory.created.default\n" in text
    assert "data: " in text
    assert "mem_1" in text
    assert "PYTHIA" in text


async def test_client_disconnect_unsubscribes(monkeypatch):
    http = _fresh_http(monkeypatch)
    sub = _FakeSubscription([
        _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1"})
    ])
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))

    response = await http.handle_nats_event_stream(_request())
    await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert sub.unsubscribed is True


async def test_nats_down_returns_503(monkeypatch):
    http = _fresh_http(monkeypatch)
    monkeypatch.setattr("mnemos.nats.client._jetstream", None)

    response = await http.handle_nats_event_stream(_request())

    assert response.status_code == 503
    assert response.body == b"NATS unavailable"

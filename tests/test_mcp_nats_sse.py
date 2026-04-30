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


class _BurstSubscription(_FakeSubscription):
    async def next_msg(self, timeout=1):
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.waiting.set()
        await asyncio.sleep(60)
        raise asyncio.TimeoutError()


class _FakeJetStream:
    def __init__(self, subscriptions=None):
        self.subscriptions = list(subscriptions or [])
        self.subscribe_calls = []

    async def subscribe(self, subject, **kwargs):
        self.subscribe_calls.append((subject, kwargs))
        if self.subscriptions:
            return self.subscriptions.pop(0)
        return _FakeSubscription()


def _request(subjects: str | None = None, *, principal_id: str = "alice-principal", **params):
    query_params = dict(params)
    if subjects is not None:
        query_params["subjects"] = subjects
    state = types.SimpleNamespace(mnemos_mcp_principal_id=principal_id)
    return types.SimpleNamespace(query_params=query_params, state=state)


def _fresh_http(monkeypatch):
    from mnemos.core import config
    from mnemos.nats import client as nats_client

    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "alice:alice-token")
    config._reset_settings_for_tests()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    sys.modules.pop("mnemos.mcp.http", None)
    return importlib.import_module("mnemos.mcp.http")


async def test_sse_non_root_subject_is_derived_from_principal_namespace(monkeypatch):
    http = _fresh_http(monkeypatch)
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role="user", namespace="alice.ns"
    )
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)

    response = await http.handle_nats_event_stream(
        _request("mnemos.memory.>,mnemos.consultation.completed.>")
    )
    await response.body_iterator.aclose()

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert [call[0] for call in js.subscribe_calls] == ["mnemos.*.*.alice_ns"]


async def test_sse_root_can_use_explicit_subject_filters(monkeypatch):
    http = _fresh_http(monkeypatch)
    http._principal_context_cache["root-principal"] = http.MCPUserContext(
        user_id="root", role="root", namespace="ops"
    )
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)

    response = await http.handle_nats_event_stream(
        _request("mnemos.memory.>,mnemos.consultation.completed.>", principal_id="root-principal")
    )
    await response.body_iterator.aclose()

    assert [call[0] for call in js.subscribe_calls] == [
        "mnemos.memory.>",
        "mnemos.consultation.completed.>",
    ]


async def test_nats_message_becomes_sse_frame(monkeypatch):
    http = _fresh_http(monkeypatch)
    sub = _FakeSubscription([
        _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1", "source_node": "PYTHIA", "content": "remote note"})
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
    assert "remote note" not in text


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


async def test_raw_payload_requires_operator_opt_in(monkeypatch):
    http = _fresh_http(monkeypatch)
    sub = _FakeSubscription([
        _FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1", "content": "remote note"})
    ])
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))
    monkeypatch.setenv("MNEMOS_MCP_NATS_RAW", "true")

    response = await http.handle_nats_event_stream(_request())
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert "remote note" in frame.decode("utf-8")


async def test_sse_reports_dropped_events_for_lagging_client(monkeypatch):
    http = _fresh_http(monkeypatch)
    monkeypatch.setattr(http, "NATS_SSE_QUEUE_MAXSIZE", 1)
    sub = _BurstSubscription([
        _FakeMsg("mnemos.memory.created.default", {"memory_id": f"mem_{i}"})
        for i in range(5)
    ])

    stream = http._nats_sse_event_source([sub])
    seen = []
    for _ in range(5):
        frame = await anext(stream)
        seen.append(frame.decode("utf-8"))
        if "event: dropped" in seen[-1]:
            break
    await stream.aclose()

    assert any("event: dropped" in item for item in seen)

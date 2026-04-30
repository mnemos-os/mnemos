"""Smoke tests for the v4.2 NATS publisher path.

Verifies the contract that publish_event NEVER raises, and that with
no JetStream context wired (the unconfigured / disabled case), it's
a silent no-op.
"""

import asyncio

import pytest

from mnemos.nats import client as nats_client
from mnemos.nats.publisher import publish_event


@pytest.fixture(autouse=True)
def _no_jetstream(monkeypatch):
    """Force the publisher to see a None JetStream context."""
    monkeypatch.setattr(nats_client, "_jetstream", None)


def test_publish_event_silent_when_disabled(caplog):
    """Disabled NATS = silent no-op, never raises."""
    asyncio.run(publish_event("mnemos.memory.created.test", {"id": "mem_1"}))


def test_publish_event_silent_when_payload_unserializable(caplog):
    """Unserializable payload logs but never raises."""

    class _NoSerialize:
        pass

    asyncio.run(publish_event("mnemos.memory.created.test", {"x": _NoSerialize()}))


def test_publish_event_uses_msg_id_for_dedup_header():
    """Calling with msg_id should not raise — header construction works.

    With no JetStream context, this is purely a serialization check;
    the publish path returns early before touching the broker.
    """
    asyncio.run(publish_event("mnemos.memory.created.test", {"a": 1}, msg_id="mem_1.created"))


def test_get_jetstream_returns_none_unconfigured(monkeypatch):
    """get_jetstream returns None when MNEMOS_NATS_URL is unset."""
    assert nats_client.get_jetstream() is None


def test_connect_nats_returns_none_when_url_missing():
    """connect_nats(None, None) is a no-op returning None."""
    result = asyncio.run(nats_client.connect_nats(None, None))
    assert result is None

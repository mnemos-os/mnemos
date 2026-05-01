"""Smoke tests for the v4.2 NATS publisher path.

Verifies the contract that publish_event NEVER raises, and that with
no JetStream context wired (the unconfigured / disabled case), it's
a silent no-op.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from mnemos.nats import client as nats_client
from mnemos.nats.publisher import publish_event


@pytest.fixture(autouse=True)
def _no_jetstream(monkeypatch):
    """Force the publisher to see a None JetStream context."""
    monkeypatch.setattr(nats_client, "_jetstream", None)
    monkeypatch.setattr(nats_client, "_publishing_enabled", False)


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


def test_connect_nats_disables_publishing_when_streams_not_verified(monkeypatch):
    class _NC:
        def jetstream(self):
            return object()

    async def connect(**kwargs):
        return _NC()

    async def ensure_streams(js):
        return False

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))
    monkeypatch.setattr(nats_client, "ensure_streams", ensure_streams)

    result = asyncio.run(nats_client.connect_nats("nats://example:4222", None))

    assert result is None
    assert nats_client.get_jetstream() is None
    assert nats_client.publishing_enabled() is False


def test_connect_nats_enables_publishing_only_after_streams_verified(monkeypatch):
    js = object()

    class _NC:
        def jetstream(self):
            return js

    async def connect(**kwargs):
        return _NC()

    async def ensure_streams(js_arg):
        assert js_arg is js
        return True

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))
    monkeypatch.setattr(nats_client, "ensure_streams", ensure_streams)

    result = asyncio.run(nats_client.connect_nats("nats://example:4222", None))

    assert result is js
    assert nats_client.get_jetstream() is js
    assert nats_client.publishing_enabled() is True


# v4.2.0a9 round-6: codex Audit Finding 11 / round-5 follow-up.
#
# ensure_streams used to lump "stream exists with MATCHING config"
# (idempotent no-op) and "stream exists with DIFFERENT config"
# (drift, broker keeps OLD config) into the same "already in use"
# substring branch and silently returned True for both. A redeploy
# with a drifted max_bytes would silently get publishing enabled
# while the broker kept the old config — operator never sees the
# drift unless they look at the stream directly.
#
# These tests verify the new disambiguation: matching → True
# (publishing safe), drifted → False (operator must intervene).


def _stream_config_obj(**overrides):
    """Build a minimal StreamConfig-like object covering the fields
    ensure_streams' drift detector reads."""
    base = dict(
        name=overrides.get("name", "MNEMOS_MEMORY"),
        subjects=overrides.get("subjects", ["mnemos.memory.>"]),
        max_age=overrides.get("max_age", 30 * 24 * 60 * 60),
        max_bytes=overrides.get("max_bytes", 10 * 1024**3),
        duplicate_window=overrides.get("duplicate_window", 2 * 60),
    )
    return SimpleNamespace(**base)


# Per-stream subject map mirrors ensure_streams' canonical declarations.
_CANONICAL_SUBJECTS = {
    "MNEMOS_MEMORY": ["mnemos.memory.>"],
    "MNEMOS_CONSULTATION": ["mnemos.consultation.>"],
    "MNEMOS_WEBHOOK": ["mnemos.webhook.>"],
}


def test_ensure_streams_returns_true_when_existing_matches(monkeypatch):
    """The classic idempotent case — operator's redeploy ships the
    SAME config that's already declared. ensure_streams must report
    success and let publishing proceed."""

    class _Js:
        async def add_stream(self, config=None, **_):
            # Simulate broker rejection because stream exists.
            raise RuntimeError("nats: API error: code=400 description=stream name already in use with a different configuration")

        async def stream_info(self, name):
            # Existing config matches what ensure_streams was about
            # to declare for this specific stream.
            return SimpleNamespace(
                config=_stream_config_obj(
                    name=name,
                    subjects=_CANONICAL_SUBJECTS[name],
                )
            )

    js = _Js()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    result = asyncio.run(nats_client.ensure_streams(js))

    assert result is True, (
        "matching-config redeclare must be idempotent and return True"
    )


def test_ensure_streams_returns_false_when_existing_drifts(monkeypatch, caplog):
    """A redeploy with drifted max_bytes (or any retention dim) must
    return False so connect_nats disables publishing — operator sees
    the broker keeping the OLD config and can intervene with
    nats stream update / delete+recreate before traffic resumes
    against stale retention.
    """
    import logging

    caplog.set_level(logging.ERROR, logger="mnemos.nats.client")

    class _Js:
        async def add_stream(self, config=None, **_):
            raise RuntimeError("nats: stream name already in use")

        async def stream_info(self, name):
            # Running stream has the OLD max_age (15 days); operator's
            # redeploy ships 30 days. This is real config drift, not
            # the documented insufficient-storage fallback.
            return SimpleNamespace(
                config=_stream_config_obj(
                    name=name,
                    subjects=_CANONICAL_SUBJECTS[name],
                    max_age=15 * 24 * 60 * 60,
                )
            )

    js = _Js()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    result = asyncio.run(nats_client.ensure_streams(js))

    assert result is False, (
        "drifted redeclare must return False so publishing stays "
        "disabled until the operator intervenes"
    )
    # Operator-facing log must name the field that drifted so they
    # know what to fix. Don't be picky about exact wording — just
    # the field name.
    assert any("max_age" in rec.message for rec in caplog.records), (
        f"drift log must name the drifted field. caplog: "
        f"{[r.message for r in caplog.records]}"
    )


def test_ensure_streams_max_bytes_smaller_treated_as_storage_fallback(monkeypatch, caplog):
    """The 1GB insufficient-storage fallback is documented runtime
    behavior; on next boot the existing 1GB stream must NOT trip the
    drift-disables-publishing branch. Otherwise operators who ever
    hit insufficient-storage permanently lose NATS publishing on
    every subsequent restart.
    """
    import logging

    caplog.set_level(logging.INFO)

    class _Js:
        async def add_stream(self, config=None, **_):
            raise RuntimeError("nats: stream name already in use")

        async def stream_info(self, name):
            # Existing stream has 1GB max_bytes — the fallback that
            # ensure_streams itself created on a previous boot under
            # insufficient-storage. ensure_streams' canonical config
            # asks for 10GB.
            return SimpleNamespace(
                config=_stream_config_obj(
                    name=name,
                    subjects=_CANONICAL_SUBJECTS[name],
                    max_bytes=1024**3,
                )
            )

    js = _Js()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    result = asyncio.run(nats_client.ensure_streams(js))

    assert result is True, (
        "max_bytes-smaller-than-desired must be treated as the "
        "documented insufficient-storage fallback path, not as drift "
        "that disables publishing"
    )
    # Should leave an INFO log naming the fallback explicitly.
    assert any(
        "fallback" in rec.message.lower() and "max_bytes" in rec.message.lower()
        for rec in caplog.records
    ), (
        "fallback path must log info naming the field. caplog: "
        f"{[r.message for r in caplog.records]}"
    )


def test_ensure_streams_storage_or_retention_drift_disables_publishing(monkeypatch, caplog):
    """Codex round-6 finding: retention and storage policies must be
    part of the drift surface. A broker with MEMORY storage where
    we declare FILE has different durability semantics — silently
    enabling publishing against it is a correctness bug.
    """
    import logging

    caplog.set_level(logging.ERROR, logger="mnemos.nats.client")

    class _Js:
        async def add_stream(self, config=None, **_):
            raise RuntimeError("nats: stream name already in use")

        async def stream_info(self, name):
            # All numeric/subject fields match. Storage drifts to
            # MEMORY (broker won't survive restart) — must catch.
            return SimpleNamespace(
                config=SimpleNamespace(
                    name=name,
                    subjects=_CANONICAL_SUBJECTS[name],
                    max_age=30 * 24 * 60 * 60,
                    max_bytes=10 * 1024**3,
                    duplicate_window=2 * 60,
                    retention=SimpleNamespace(value="LIMITS"),
                    storage=SimpleNamespace(value="MEMORY"),  # drift
                )
            )

    js = _Js()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    result = asyncio.run(nats_client.ensure_streams(js))

    assert result is False, "storage-policy drift must disable publishing"
    assert any("storage" in rec.message for rec in caplog.records), (
        f"drift log must name 'storage'. caplog: {[r.message for r in caplog.records]}"
    )


def test_ensure_streams_drift_detector_returns_empty_on_match():
    """Direct unit test of the drift helper — matching configs
    produce an empty drift dict (= idempotent)."""
    from mnemos.nats.client import _stream_config_drift

    class _Js:
        async def stream_info(self, name):
            return SimpleNamespace(config=_stream_config_obj(name=name))

    desired = _stream_config_obj()
    drift = asyncio.run(_stream_config_drift(_Js(), desired))
    assert drift == {}


def test_ensure_streams_drift_detector_reports_field_drift():
    """Direct unit test — every drifted field shows up in the dict
    with its (running, desired) tuple."""
    from mnemos.nats.client import _stream_config_drift

    class _Js:
        async def stream_info(self, name):
            return SimpleNamespace(
                config=_stream_config_obj(
                    name=name,
                    max_bytes=1024**3,            # drift
                    max_age=15 * 24 * 60 * 60,    # drift
                )
            )

    desired = _stream_config_obj()  # ensure_streams' canonical config
    drift = asyncio.run(_stream_config_drift(_Js(), desired))
    assert "max_bytes" in drift
    assert "max_age" in drift
    assert "duplicate_window" not in drift  # this field matched

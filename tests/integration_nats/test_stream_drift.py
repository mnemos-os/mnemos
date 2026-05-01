"""Stream config-drift tests against a live NATS broker.

Pre-v4.2.0a9 the suite never proved what happens when a redeploy
ships ``ensure_streams()`` with mismatched ``max_age`` /
``max_bytes`` / ``duplicate_window`` against an existing stream.
The runbook in ``docs/NATS_OPERATIONS.md`` claims:

    add_stream is idempotent for MATCHING configs and raises for
    mismatched configs. ... the running stream keeps the OLD config.

These tests pin that contract.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytestmark = pytest.mark.asyncio


async def test_redeclare_with_matching_config_is_idempotent_noop(js, stream_cleanup):
    """Re-declaring a stream with identical config must not raise.

    This is the core of ``ensure_streams()`` — it runs on every
    process startup, and a green redeploy MUST not blow up just
    because the stream already exists.
    """
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    name = stream_cleanup
    config = StreamConfig(
        name=name,
        subjects=[f"{name.lower()}.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=int(timedelta(minutes=5).total_seconds() * 1_000_000_000),
        max_bytes=1024 * 1024,  # 1 MiB
        duplicate_window=int(timedelta(seconds=30).total_seconds() * 1_000_000_000),
    )

    info_a = await js.add_stream(config=config)
    info_b = await js.add_stream(config=config)

    assert info_a.config.name == info_b.config.name == name
    assert info_a.config.max_bytes == info_b.config.max_bytes == 1024 * 1024


async def test_redeclare_with_drift_raises_and_keeps_old_config(js, stream_cleanup):
    """A redeploy that ships a different ``max_bytes`` must raise.

    The runbook contract: the running stream keeps the OLD config.
    Operators are expected to ``nats stream update`` manually or
    delete + recreate. Without this guard a silent ALTER would
    surprise operators and could lose retained messages.
    """
    from nats.errors import Error as NatsError
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    name = stream_cleanup
    base = StreamConfig(
        name=name,
        subjects=[f"{name.lower()}.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=int(timedelta(minutes=5).total_seconds() * 1_000_000_000),
        max_bytes=1024 * 1024,
        duplicate_window=int(timedelta(seconds=30).total_seconds() * 1_000_000_000),
    )
    await js.add_stream(config=base)

    drifted = StreamConfig(
        name=name,
        subjects=[f"{name.lower()}.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=int(timedelta(minutes=10).total_seconds() * 1_000_000_000),  # drift
        max_bytes=2 * 1024 * 1024,  # drift
        duplicate_window=int(timedelta(seconds=30).total_seconds() * 1_000_000_000),
    )

    with pytest.raises((NatsError, Exception)):
        await js.add_stream(config=drifted)

    info = await js.stream_info(name)
    assert info.config.max_bytes == 1024 * 1024, (
        "drifted redeclare must NOT silently mutate the running stream"
    )


async def test_ensure_streams_recovers_from_existing_streams(js, nats_url, nats_token):
    """The mnemos-side helper ``ensure_streams`` must be safe to run
    against a broker that already has the streams declared (e.g. a
    rolling restart, or a second mnemos process joining)."""
    import nats

    from mnemos.nats.client import ensure_streams

    # First call creates the streams.
    kwargs: dict = {"servers": [nats_url]}
    if nats_token:
        kwargs["token"] = nats_token

    nc = await nats.connect(**kwargs)
    try:
        js_ctx = nc.jetstream()
        try:
            await ensure_streams(js_ctx)
            # Second call must be a no-op, not raise.
            await ensure_streams(js_ctx)
        finally:
            for stream in ("MNEMOS_MEMORY", "MNEMOS_CONSULTATION", "MNEMOS_WEBHOOK"):
                try:
                    await js_ctx.delete_stream(stream)
                except Exception:
                    pass
    finally:
        await nc.drain()

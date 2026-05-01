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


async def test_redeclare_three_times_with_matching_config(js, stream_cleanup):
    """Equivalent of ``ensure_streams`` re-run resilience without
    touching the production stream names.

    The original draft of this test called ``mnemos.nats.client.
    ensure_streams`` directly and then deleted ``MNEMOS_MEMORY`` /
    ``MNEMOS_CONSULTATION`` / ``MNEMOS_WEBHOOK`` to clean up. That
    is destructive against any shared/staging/prod broker it points
    at — the operator-facing docs explicitly invite running this
    suite against pre-prod, and a delete of those fixed names would
    take real retained messages with it. So instead we verify the
    SAME idempotency contract using a per-test isolated stream:
    three add_stream calls back-to-back must not raise, and must
    leave the stream config unchanged.
    """
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    name = stream_cleanup
    config = StreamConfig(
        name=name,
        subjects=[f"{name.lower()}.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=int(timedelta(minutes=5).total_seconds() * 1_000_000_000),
        max_bytes=4 * 1024 * 1024,
        duplicate_window=int(timedelta(seconds=30).total_seconds() * 1_000_000_000),
    )

    info1 = await js.add_stream(config=config)
    info2 = await js.add_stream(config=config)
    info3 = await js.add_stream(config=config)

    # All three returned the same logical stream.
    assert info1.config.name == info2.config.name == info3.config.name == name
    assert info1.config.max_bytes == info2.config.max_bytes == info3.config.max_bytes
    assert info1.config.max_age == info2.config.max_age == info3.config.max_age

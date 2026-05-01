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
        max_age=int(timedelta(minutes=5).total_seconds()),
        max_bytes=1024 * 1024,  # 1 MiB
        duplicate_window=int(timedelta(seconds=30).total_seconds()),
    )

    info_a = await js.add_stream(config=config)
    info_b = await js.add_stream(config=config)

    assert info_a.config.name == info_b.config.name == name
    assert info_a.config.max_bytes == info_b.config.max_bytes == 1024 * 1024


@pytest.mark.parametrize(
    "drift_field,drift_value",
    [
        ("max_bytes", 2 * 1024 * 1024),
        ("max_age", int(timedelta(minutes=10).total_seconds())),
        ("duplicate_window", int(timedelta(seconds=60).total_seconds())),
    ],
    ids=["max_bytes", "max_age", "duplicate_window"],
)
async def test_redeclare_with_drift_raises_and_keeps_old_config(
    js, stream_cleanup, drift_field, drift_value
):
    """A redeploy that ships a different retention config must raise
    a ``BadRequestError`` AND leave the running stream untouched
    across ALL three retention dimensions.

    The runbook contract: the running stream keeps the OLD config.
    Operators are expected to ``nats stream update`` manually or
    delete + recreate. Without this guard a silent ALTER would
    surprise operators and could lose retained messages.

    Parameterized so we exercise drift in each retention field
    independently — codex round-3 finding: a single test that
    only mutated max_bytes could create false green for max_age
    or duplicate_window drift.
    """
    from nats.js.errors import BadRequestError
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    name = stream_cleanup
    base_max_bytes = 1024 * 1024
    base_max_age = int(timedelta(minutes=5).total_seconds())
    base_duplicate_window = int(timedelta(seconds=30).total_seconds())

    base_kwargs = dict(
        name=name,
        subjects=[f"{name.lower()}.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=base_max_age,
        max_bytes=base_max_bytes,
        duplicate_window=base_duplicate_window,
    )
    await js.add_stream(config=StreamConfig(**base_kwargs))

    drifted_kwargs = {**base_kwargs, drift_field: drift_value}
    drifted = StreamConfig(**drifted_kwargs)

    # Specific exception class — a generic "any Exception" catch
    # would let a timeout, permission error, or other unrelated
    # failure look like a successful drift-rejection.
    with pytest.raises(BadRequestError):
        await js.add_stream(config=drifted)

    # ALL THREE retention dimensions must be unchanged after the
    # rejected redeclare. A drift in one field that was silently
    # accepted would otherwise pass a one-field check.
    info = await js.stream_info(name)
    assert info.config.max_bytes == base_max_bytes, (
        f"drift in {drift_field}: max_bytes mutated from "
        f"{base_max_bytes} to {info.config.max_bytes}"
    )
    assert info.config.max_age == base_max_age * 1_000_000_000, (
        f"drift in {drift_field}: max_age mutated"
    )
    # Note: nats-py serializes max_age/duplicate_window in
    # nanoseconds on the wire; the stream_info comes back in
    # nanoseconds as well, hence the *1e9 here. add_stream
    # accepts seconds.
    assert info.config.duplicate_window == base_duplicate_window * 1_000_000_000, (
        f"drift in {drift_field}: duplicate_window mutated"
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
        max_age=int(timedelta(minutes=5).total_seconds()),
        max_bytes=4 * 1024 * 1024,
        duplicate_window=int(timedelta(seconds=30).total_seconds()),
    )

    info1 = await js.add_stream(config=config)
    info2 = await js.add_stream(config=config)
    info3 = await js.add_stream(config=config)

    # All three returned the same logical stream.
    assert info1.config.name == info2.config.name == info3.config.name == name
    assert info1.config.max_bytes == info2.config.max_bytes == info3.config.max_bytes
    assert info1.config.max_age == info2.config.max_age == info3.config.max_age

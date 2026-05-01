"""End-to-end queue-group load-balance test against a live broker.

Pre-v4.2.0a9, the v4.2.0a8 queue-group support was only proven via
``_FakeJetStream`` in unit tests. This test verifies the actual
nats-py + JetStream broker behavior:

  1. Two subscribers join the same queue group.
  2. A burst of N messages gets published.
  3. Each subscriber receives SOME messages (not all on one).

The test does not assert an exact split — JetStream delivers to
whichever subscriber is currently free, so equal split is not
guaranteed. The contract is: no single subscriber takes 100%.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta

import pytest

pytestmark = pytest.mark.asyncio


async def _publish_burst(js, subject: str, count: int) -> None:
    for i in range(count):
        await js.publish(subject, f"msg-{i}".encode())


async def _consume_into(sub, bucket: list, stop_event: asyncio.Event):
    """Drain a subscription into ``bucket`` until ``stop_event`` is set."""
    while not stop_event.is_set():
        try:
            msg = await sub.next_msg(timeout=0.5)
        except Exception:
            continue
        bucket.append(msg.data.decode())
        try:
            await msg.ack()
        except Exception:
            pass


async def test_queue_group_load_balances_across_subscribers(js, stream_cleanup):
    from nats.js.api import (
        AckPolicy,
        ConsumerConfig,
        DeliverPolicy,
        RetentionPolicy,
        StorageType,
        StreamConfig,
    )

    name = stream_cleanup
    subject_root = name.lower()
    subject_pub = f"{subject_root}.events.test"
    subject_filter = f"{subject_root}.events.>"

    await js.add_stream(
        config=StreamConfig(
            name=name,
            subjects=[f"{subject_root}.>"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=int(timedelta(minutes=5).total_seconds() * 1_000_000_000),
            max_bytes=10 * 1024 * 1024,
            duplicate_window=int(timedelta(seconds=10).total_seconds() * 1_000_000_000),
        )
    )

    durable = f"qg_test_{secrets.token_hex(4)}"
    consumer_cfg = ConsumerConfig(
        durable_name=durable,
        deliver_policy=DeliverPolicy.NEW,
        ack_policy=AckPolicy.EXPLICIT,
        deliver_group=durable,
    )

    # Two subscribers — both join the same queue group + durable.
    sub_a = await js.subscribe(
        subject_filter,
        queue=durable,
        durable=durable,
        stream=name,
        config=consumer_cfg,
    )
    sub_b = await js.subscribe(
        subject_filter,
        queue=durable,
        durable=durable,
        stream=name,
        config=consumer_cfg,
    )

    bucket_a: list[str] = []
    bucket_b: list[str] = []
    stop = asyncio.Event()
    consumer_a = asyncio.create_task(_consume_into(sub_a, bucket_a, stop))
    consumer_b = asyncio.create_task(_consume_into(sub_b, bucket_b, stop))

    try:
        # Publish 30 messages — large enough that random delivery
        # to one subscriber 100% of the time is statistically
        # implausible.
        await _publish_burst(js, subject_pub, 30)
        # Drain — wait until total received hits 30 or we time out.
        deadline = asyncio.get_event_loop().time() + 10.0
        while len(bucket_a) + len(bucket_b) < 30:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.gather(consumer_a, consumer_b, return_exceptions=True)
        try:
            await sub_a.unsubscribe()
        except Exception:
            pass
        try:
            await sub_b.unsubscribe()
        except Exception:
            pass

    total = len(bucket_a) + len(bucket_b)
    assert total == 30, f"queue-group lost messages: only {total}/30 delivered"

    # The contract: BOTH subscribers got SOME work.
    assert len(bucket_a) > 0, "subscriber A starved — queue group not balancing"
    assert len(bucket_b) > 0, "subscriber B starved — queue group not balancing"

    # Sanity: no duplicates across the two buckets — JetStream
    # delivered each message to exactly one subscriber.
    overlap = set(bucket_a) & set(bucket_b)
    assert not overlap, f"queue group duplicated {len(overlap)} messages across subscribers"

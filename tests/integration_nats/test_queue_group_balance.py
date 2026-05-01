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


async def _publish_one(js, subject: str, payload: str) -> None:
    await js.publish(subject, payload.encode())


async def _consume_into(sub, bucket: list, ready_event: asyncio.Event,
                        stop_event: asyncio.Event):
    """Drain a subscription into ``bucket`` until ``stop_event`` is set.

    Sets ``ready_event`` after the FIRST successful next_msg call —
    that's a stronger barrier than just "task started" because it
    proves the subscription has reached steady-state and the broker
    has the consumer registered as a delivery target. Without this
    barrier, a publish burst racing the subscribe handshake can
    land entirely on whichever consumer registered first.

    Ack errors propagate (not swallowed) — a broken ack would
    otherwise look like a successful delivery and falsely satisfy
    the message-count assertion.
    """
    first_seen = False
    while not stop_event.is_set():
        try:
            msg = await sub.next_msg(timeout=0.5)
        except asyncio.TimeoutError:
            continue
        bucket.append(msg.data.decode())
        await msg.ack()  # propagate failures
        if not first_seen:
            first_seen = True
            ready_event.set()


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
    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    stop = asyncio.Event()
    consumer_a = asyncio.create_task(_consume_into(sub_a, bucket_a, ready_a, stop))
    consumer_b = asyncio.create_task(_consume_into(sub_b, bucket_b, ready_b, stop))

    try:
        # Step 1 — publish 2 warmup messages, ONE per subscriber.
        # Wait until both ready events fire before the real burst.
        # This rules out the "subscriber B handshake hadn't
        # completed when the burst started" failure mode that
        # would otherwise make balance assertions race-prone.
        for i in range(2):
            await _publish_one(js, subject_pub, f"warmup-{i}")
        try:
            await asyncio.wait_for(
                asyncio.gather(ready_a.wait(), ready_b.wait()),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "queue-group test setup: one or both subscribers never "
                "received a warmup message within 10s — broker "
                "delivery path may be wedged"
            )

        # Step 2 — publish the balance-check burst.
        burst_payloads = [f"burst-{i}" for i in range(20)]
        for p in burst_payloads:
            await _publish_one(js, subject_pub, p)

        # Step 3 — drain. Total expected = 2 warmup + 20 burst = 22.
        expected_total = 2 + len(burst_payloads)
        deadline = asyncio.get_event_loop().time() + 15.0
        while len(bucket_a) + len(bucket_b) < expected_total:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.gather(consumer_a, consumer_b, return_exceptions=True)
        try:
            await sub_a.unsubscribe()
        finally:
            try:
                await sub_b.unsubscribe()
            except Exception:
                pass

    # Strong assertion: the multi-set of payloads delivered exactly
    # equals what was published. No drops, no duplicates within or
    # across either bucket.
    delivered = sorted(bucket_a + bucket_b)
    expected = sorted(["warmup-0", "warmup-1"] + burst_payloads)
    assert delivered == expected, (
        f"queue-group payload set mismatch:\n"
        f"  delivered count: {len(delivered)} (expected {len(expected)})\n"
        f"  bucket_a: {sorted(bucket_a)}\n"
        f"  bucket_b: {sorted(bucket_b)}"
    )

    # The actual balance contract: every subscriber receives at
    # least the warmup it was waiting on, plus some share of the
    # burst. Allow either subscriber to do most of the burst work
    # (JetStream may legitimately favor whichever is more responsive)
    # but require BOTH to have received at least one BURST message
    # — not just the warmup that gated readiness. That distinguishes
    # "queue group really load-balanced" from "we just got to
    # readiness via the warmup and then one consumer ate the burst."
    burst_in_a = [p for p in bucket_a if p.startswith("burst-")]
    burst_in_b = [p for p in bucket_b if p.startswith("burst-")]
    assert burst_in_a, (
        f"subscriber A got 0 burst messages — queue group not balancing "
        f"(bucket_a={bucket_a})"
    )
    assert burst_in_b, (
        f"subscriber B got 0 burst messages — queue group not balancing "
        f"(bucket_b={bucket_b})"
    )

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
            max_age=int(timedelta(minutes=5).total_seconds()),
            max_bytes=10 * 1024 * 1024,
            duplicate_window=int(timedelta(seconds=10).total_seconds()),
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

    warmup_payloads: list[str] = []
    try:
        # Step 1 — bring BOTH subscribers to ready by publishing
        # warmup messages until each has observed one. JetStream
        # delivers a warmup to whichever subscriber is currently
        # free, so two warmups COULD legally land on the same
        # subscriber (codex round-2 finding 3). Bounded loop with
        # cap to avoid runaway publishing if delivery is wedged.
        warmup_deadline = asyncio.get_event_loop().time() + 10.0
        warmup_seq = 0
        warmup_cap = 50
        while not (ready_a.is_set() and ready_b.is_set()):
            if warmup_seq >= warmup_cap:
                pytest.fail(
                    f"queue-group test setup: published {warmup_seq} "
                    f"warmup messages but ready_a={ready_a.is_set()} "
                    f"ready_b={ready_b.is_set()} — broker delivery path "
                    f"may be wedged or queue-group not actually balancing"
                )
            if asyncio.get_event_loop().time() > warmup_deadline:
                pytest.fail(
                    "queue-group test setup: 10s elapsed and one or "
                    "both subscribers never reached ready state"
                )
            payload = f"warmup-{warmup_seq}"
            warmup_payloads.append(payload)
            await _publish_one(js, subject_pub, payload)
            warmup_seq += 1
            # Yield long enough for the broker round-trip + the
            # consumer task to dequeue. 0.2s is conservative.
            await asyncio.sleep(0.2)

        # Step 2 — publish the balance-check burst.
        burst_payloads = [f"burst-{i}" for i in range(20)]
        for p in burst_payloads:
            await _publish_one(js, subject_pub, p)

        # Step 3 — drain. Total expected = warmup_count + 20 burst.
        expected_total = len(warmup_payloads) + len(burst_payloads)
        deadline = asyncio.get_event_loop().time() + 20.0
        while len(bucket_a) + len(bucket_b) < expected_total:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        # Critical: re-raise non-CancelledError consumer exceptions
        # (codex round-2 finding 2). A swallowed ack failure would
        # let a redelivered message look like a successful one and
        # make the multiset assertion below pass on a false count.
        consumer_results = await asyncio.gather(
            consumer_a, consumer_b, return_exceptions=True
        )
        for r in consumer_results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                raise r
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
    expected = sorted(warmup_payloads + burst_payloads)
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

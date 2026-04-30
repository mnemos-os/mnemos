"""Webhook lifespan worker loops and recovery claiming."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Iterable

import asyncpg

from . import repair as webhook_repair
from . import types as webhook_types
from .types import _ClaimedDelivery, _get_send_semaphore

logger = logging.getLogger(__name__)


async def repair_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: repair superseded retry rows on its own cadence.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("webhook retry repair worker started")
    loop = asyncio.get_running_loop()
    repair_burst_deadline = loop.time() + webhook_types.REPAIR_BURST_SECONDS
    next_repair_at = loop.time()
    while True:
        try:
            now = loop.time()
            if now >= next_repair_at:
                in_burst = now < repair_burst_deadline
                await webhook_repair._repair_superseded_retrying_deliveries_safely(
                    pool,
                    phase="burst" if in_burst else "periodic",
                )
                next_repair_at = now + (
                    webhook_types.REPAIR_BURST_INTERVAL if in_burst else webhook_types.REPAIR_PERIODIC_INTERVAL
                )

            await asyncio.sleep(max(0.0, next_repair_at - loop.time()))
        except asyncio.CancelledError:
            logger.info("webhook retry repair worker cancelled")
            raise
        except Exception:  # pragma: no cover - log and keep running
            logger.exception("webhook retry repair worker iteration failed")
            await asyncio.sleep(webhook_types.REPAIR_BURST_INTERVAL)


async def delivery_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: picks up pending deliveries whose scheduled_at has arrived.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("webhook delivery recovery worker started")
    while True:
        try:
            await _recover_due_deliveries(pool)
            await asyncio.sleep(webhook_types.RECOVERY_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("webhook delivery recovery worker cancelled")
            raise
        except Exception:  # pragma: no cover - log and keep running
            logger.exception("webhook delivery recovery worker iteration failed")
            await asyncio.sleep(webhook_types.RECOVERY_POLL_INTERVAL)


async def recovery_worker_loop(pool: asyncpg.Pool) -> None:
    """Compatibility wrapper for the delivery recovery loop."""
    await delivery_worker_loop(pool)


async def _recover_due_deliveries(pool: asyncpg.Pool, *, limit: int = 50) -> int:
    """Recover due deliveries by scheduling lifecycle-tracked send attempts."""
    claim_limit = min(50, limit, _semaphore_available())
    if claim_limit <= 0:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            claimed_deliveries = await _claim_recoverable_deliveries(conn, limit=claim_limit)
    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
    from .sender import _attempt_delivery
    for claimed in claimed_deliveries:
        _schedule_delivery_attempt(
            _attempt_delivery(str(claimed.delivery["id"]), pool=pool, claimed=claimed)
        )
    recovered = len(claimed_deliveries)
    if recovered:
        await asyncio.sleep(0)
    return recovered


def _semaphore_available() -> int:
    """Return the current best-effort free slot count for recovery batch sizing."""
    return max(0, _get_send_semaphore()._value)


async def _claim_recoverable_deliveries(
    conn: asyncpg.Connection,
    *,
    limit: int = 50,
    lease_seconds: int | None = None,
) -> list[_ClaimedDelivery]:
    """Claim due recovery rows before scheduling send tasks."""
    if limit <= 0:
        return []
    if lease_seconds is None:
        lease_seconds = webhook_types.WEBHOOK_LEASE_SECONDS
    lease_token = str(uuid.uuid4())
    pre_claim_monotonic = time.monotonic()
    rows = await conn.fetch(
        """
        WITH claim_clock AS (
          SELECT clock_timestamp() AS claim_now
        ),
        recoverable AS (
          SELECT d.id
          FROM webhook_deliveries d, claim_clock
          WHERE d.scheduled_at <= claim_clock.claim_now
            AND d.attempt_num <= $1
            AND d.status NOT IN ('succeeded', 'abandoned')
            AND NOT d.superseded
            AND d.status IN ('pending', 'retrying')
            AND (d.lease_token IS NULL OR d.lease_expires_at < claim_clock.claim_now)
            AND d.writer_revision = $5
            AND NOT EXISTS (
              SELECT 1
              FROM webhook_deliveries peer
              WHERE peer.subscription_id = d.subscription_id
                AND peer.event_type = d.event_type
                AND peer.payload_hash = d.payload_hash
                AND peer.status = 'succeeded'
            )
            AND (
              d.status = 'pending'
              OR (
                d.status = 'retrying'
                AND NOT EXISTS (
                  SELECT 1
                  FROM webhook_deliveries newer
                  WHERE newer.subscription_id = d.subscription_id
                    AND newer.event_type = d.event_type
                    AND newer.payload_hash = d.payload_hash
                    AND newer.attempt_num > d.attempt_num
                )
              )
            )
          ORDER BY d.scheduled_at
          LIMIT $3
          FOR UPDATE SKIP LOCKED
        )
        UPDATE webhook_deliveries d
        SET lease_token=$2::uuid,
            lease_expires_at=claim_clock.claim_now + ($4::int * INTERVAL '1 second'),
            status=CASE WHEN d.status = 'pending' THEN 'retrying' ELSE d.status END
        FROM recoverable, webhook_subscriptions s, claim_clock
        WHERE d.id = recoverable.id
          AND s.id = d.subscription_id
        RETURNING d.id, d.subscription_id, d.event_type, d.payload,
                  d.payload_hash, d.attempt_num, d.status,
                  d.lease_expires_at, claim_clock.claim_now AS claim_db_now,
                  $2::uuid AS lease_token,
                  s.url, s.secret, s.revoked, s.owner_id, s.namespace
        """,
        webhook_types.MAX_ATTEMPTS,
        lease_token,
        limit,
        lease_seconds,
        webhook_types.NEW_CODE_WRITER_REVISION,
    )
    return [
        _ClaimedDelivery(
            delivery=row,
            lease_token=lease_token,
            pre_claim_monotonic=pre_claim_monotonic,
        )
        for row in rows
    ]


async def _recoverable_delivery_ids(
    conn: asyncpg.Connection,
    *,
    limit: int = 50,
) -> Iterable[asyncpg.Record]:
    """Return due delivery rows without claiming them, for diagnostics and tests."""
    return await conn.fetch(
        """
        SELECT d.id FROM webhook_deliveries d
        WHERE d.scheduled_at <= clock_timestamp()
          AND d.attempt_num <= $1
          AND d.status NOT IN ('succeeded', 'abandoned')
          AND NOT d.superseded
          AND d.status IN ('pending', 'retrying')
          AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
          AND d.writer_revision = $3
          AND NOT EXISTS (
            SELECT 1
            FROM webhook_deliveries peer
            WHERE peer.subscription_id = d.subscription_id
              AND peer.event_type = d.event_type
              AND peer.payload_hash = d.payload_hash
              AND peer.status = 'succeeded'
          )
          AND (
            d.status = 'pending'
            OR (
              d.status = 'retrying'
              AND NOT EXISTS (
                SELECT 1
                FROM webhook_deliveries newer
                WHERE newer.subscription_id = d.subscription_id
                  AND newer.event_type = d.event_type
                  AND newer.payload_hash = d.payload_hash
                  AND newer.attempt_num > d.attempt_num
              )
            )
          )
        ORDER BY d.scheduled_at
        LIMIT $2
        """,
        webhook_types.MAX_ATTEMPTS, limit, webhook_types.NEW_CODE_WRITER_REVISION,
    )

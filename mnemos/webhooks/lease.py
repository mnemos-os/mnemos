"""Webhook delivery lease acquisition, validation, and release."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from . import chain as webhook_chain
from . import types as webhook_types
from .types import _ClaimedDelivery, _DeliveryResult

logger = logging.getLogger(__name__)


async def _claim_delivery(
    pool: asyncpg.Pool,
    delivery_id: str,
    *,
    lease_token: str,
    lease_seconds: int | None = None,
) -> Optional[_ClaimedDelivery]:
    """Persist a short lease for one due live delivery row."""
    if lease_seconds is None:
        lease_seconds = webhook_types.WEBHOOK_LEASE_SECONDS
    async with pool.acquire() as conn:
        async with conn.transaction():
            delivery = await webhook_chain._load_delivery_for_claim(conn, delivery_id)
            if not delivery:
                return None

            await webhook_chain._lock_delivery_chain(conn, delivery)
            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                await webhook_chain._abandon_current_attempt_after_succeeded_chain_peer(conn, delivery_id)
                return None

            if delivery["status"] == "retrying" and await webhook_chain._has_successor_attempt(conn, delivery):
                await conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=TRUE,
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid AND status='retrying' AND NOT superseded
                    """,
                    delivery_id,
                )
                return None

            pre_claim_monotonic = time.monotonic()
            claimed = await conn.fetchrow(
                """
                UPDATE webhook_deliveries d
                SET lease_token=$2::uuid,
                    lease_expires_at=claim_clock.claim_now + ($3::int * INTERVAL '1 second'),
                    status=CASE WHEN d.status = 'pending' THEN 'retrying' ELSE d.status END
                FROM webhook_subscriptions s,
                     (SELECT clock_timestamp() AS claim_now) claim_clock
                WHERE s.id = d.subscription_id
                  AND d.id=$1::uuid
                  AND d.scheduled_at <= claim_clock.claim_now
                  AND d.attempt_num <= $4
                  AND NOT d.superseded
                  AND d.status IN ('pending', 'retrying')
                  AND (d.lease_token IS NULL OR d.lease_expires_at < claim_clock.claim_now)
                  AND d.writer_revision = $5
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
                RETURNING d.id, d.subscription_id, d.event_type, d.payload,
                          d.payload_hash, d.attempt_num, d.status,
                          d.lease_expires_at, claim_clock.claim_now AS claim_db_now,
                          s.url, s.secret, s.revoked, s.owner_id, s.namespace
                """,
                delivery_id,
                lease_token,
                lease_seconds,
                webhook_types.MAX_ATTEMPTS,
                webhook_types.NEW_CODE_WRITER_REVISION,
            )
            if claimed is None:
                return None
            return _ClaimedDelivery(
                delivery=claimed,
                lease_token=lease_token,
                pre_claim_monotonic=pre_claim_monotonic,
            )


async def _guard_preclaimed_delivery_before_send(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    lease_token: str,
) -> bool:
    """Re-check a recovery-preclaimed row under the chain lock before POSTing."""
    delivery_id = str(delivery["id"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)
            if not await _preclaimed_delivery_is_live_and_owned(conn, delivery_id, lease_token):
                await _release_owned_lease_for_reclaim(conn, delivery_id, lease_token)
                return False

            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    _DeliveryResult(
                        succeeded=False,
                        error="succeeded-chain-peer-before-send",
                    ),
                )
                return False

            if await webhook_chain._has_live_successor_attempt(conn, delivery):
                await webhook_chain._abandon_owned_attempt_after_live_successor(
                    conn,
                    delivery_id,
                    lease_token,
                )
                return False

            return True


async def _preclaimed_delivery_is_live_and_owned(
    conn: asyncpg.Connection,
    delivery_id: str,
    lease_token: str,
) -> bool:
    """Return whether a preclaimed row is still live under this worker's lease."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries
          WHERE id=$1::uuid
            AND lease_token=$2::uuid
            AND lease_expires_at > clock_timestamp()
            AND status IN ('pending', 'retrying')
            AND NOT superseded
        )
        """,
        delivery_id,
        lease_token,
    )


def _claim_remaining_send_window_seconds(
    delivery: asyncpg.Record,
    *,
    pre_claim_monotonic: float,
) -> float:
    """Compute remaining send time from DB claim timestamps plus app elapsed time."""
    lease_expires_at = _as_aware_utc(delivery["lease_expires_at"])
    claim_db_now = _as_aware_utc(delivery["claim_db_now"])
    db_claim_window = (lease_expires_at - claim_db_now).total_seconds()
    now_monotonic = time.monotonic()
    elapsed_since_pre_claim = max(0.0, now_monotonic - pre_claim_monotonic)
    return db_claim_window - elapsed_since_pre_claim - webhook_types.WEBHOOK_FINALIZE_BUFFER_SECONDS


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize asyncpg timestamp values for arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _release_owned_lease_for_reclaim(
    conn: asyncpg.Connection,
    delivery_id: str,
    lease_token: str,
) -> bool:
    """Clear this worker's lease after no POST occurred, without consuming a retry."""
    released = await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
        RETURNING id, status, superseded
        """,
        delivery_id,
        lease_token,
    )
    return (
        released is not None
        and released["status"] in webhook_types.LIVE_DELIVERY_STATUSES
        and not released["superseded"]
    )


async def _clear_stale_owned_lease_after_terminal_finalize(
    conn: asyncpg.Connection,
    delivery_id: str,
    lease_token: str,
) -> None:
    """Release our lease when another writer terminalized the same row first."""
    cleared = await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
        RETURNING id, status, superseded
        """,
        delivery_id,
        lease_token,
    )
    if cleared is None:
        logger.warning(
            "webhook delivery %s success finalize found no live owned row; stale lease was already gone",
            delivery_id,
        )
        return
    if cleared["status"] not in webhook_types.LIVE_DELIVERY_STATUSES or cleared["superseded"]:
        logger.warning(
            "webhook delivery %s was already terminal at success finalize time; stale lease cleared",
            delivery_id,
        )
        return
    logger.warning(
        "webhook delivery %s success finalize found no live owned row; stale lease cleared",
        delivery_id,
    )

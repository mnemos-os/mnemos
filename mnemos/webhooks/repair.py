"""Webhook retry-chain repair sweeps."""
from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL = """
    UPDATE webhook_deliveries d
    SET status = 'abandoned',
        superseded = TRUE,
        status_updated_at = clock_timestamp(),
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE d.status IN ('pending', 'retrying')
      AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
      AND (
        EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = d.subscription_id
            AND newer.event_type = d.event_type
            AND newer.payload_hash = d.payload_hash
            AND newer.attempt_num > d.attempt_num
        )
        OR EXISTS (
          SELECT 1
          FROM webhook_deliveries peer
          WHERE peer.subscription_id = d.subscription_id
            AND peer.event_type = d.event_type
            AND peer.payload_hash = d.payload_hash
            AND peer.status = 'succeeded'
        )
      )
"""


async def _repair_superseded_retrying_deliveries_safely(
    pool: asyncpg.Pool,
    *,
    phase: str,
) -> None:
    """Run one repair sweep without killing the recovery worker on failure."""
    try:
        result = await repair_superseded_retrying_deliveries(pool)
        logger.info("webhook retry repair %s sweep result: %s", phase, result)
    except Exception:  # pragma: no cover - log and keep running
        logger.exception("webhook retry repair %s sweep failed", phase)


async def repair_superseded_retrying_deliveries(pool: asyncpg.Pool) -> str:
    """Terminalize live attempts made obsolete by successor rows or predecessor success."""
    async with pool.acquire() as conn:
        return await conn.execute(WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL)

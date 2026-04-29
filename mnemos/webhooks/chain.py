"""Webhook retry-chain locking and successor management."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

import asyncpg

from . import types as webhook_types
from .types import _DeliveryResult


async def _load_delivery_for_claim(conn: asyncpg.Connection, delivery_id: str) -> Optional[asyncpg.Record]:
    """Load a due live delivery candidate for a short lease attempt."""
    return await conn.fetchrow(
        """
        SELECT d.id, d.subscription_id, d.event_type, d.payload,
               d.payload_hash, d.attempt_num, d.status,
               s.url, s.secret, s.revoked
        FROM webhook_deliveries d
        JOIN webhook_subscriptions s ON s.id = d.subscription_id
        WHERE d.id = $1::uuid
          AND d.scheduled_at <= clock_timestamp()
          AND d.attempt_num <= $2
          AND NOT d.superseded
          AND d.status IN ('pending', 'retrying')
          AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
        """,
        delivery_id, webhook_types.MAX_ATTEMPTS,
    )


async def _insert_successor_delivery(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
    next_attempt: int,
    scheduled_at: datetime,
) -> Optional[asyncpg.Record]:
    """Insert the next live retry attempt if another writer has not already won."""
    return await conn.fetchrow(
        """
        INSERT INTO webhook_deliveries
          (subscription_id, event_type, payload, payload_hash,
           attempt_num, status, scheduled_at, writer_revision)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)
        ON CONFLICT (subscription_id, event_type, payload_hash, attempt_num)
          WHERE status IN ('pending', 'retrying') AND NOT superseded
        DO NOTHING
        RETURNING id
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload"],
        delivery["payload_hash"],
        next_attempt,
        scheduled_at,
        webhook_types.NEW_CODE_WRITER_REVISION,
    )


async def _lock_delivery_chain(conn: asyncpg.Connection, delivery: asyncpg.Record) -> None:
    """Serialize new-code recovery claims and successor inserts per chain."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _delivery_chain_lock_key(delivery))


def _delivery_chain_lock_key(delivery: asyncpg.Record, _hashlib_mod=None) -> int:
    """Stable signed-int64 lock key for one webhook retry chain."""
    if _hashlib_mod is None:
        _hashlib_mod = hashlib
    digest = _hashlib_mod.sha256(
        (
            "webhook-chain:"
            f"{delivery['subscription_id']}:{delivery['event_type']}:{delivery['payload_hash']}"
        ).encode("utf-8")
    ).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    if key >= 2**63:
        key -= 2**64
    return key


async def _has_successor_attempt(conn: asyncpg.Connection, delivery: asyncpg.Record) -> bool:
    """Return whether a newer attempt already exists for this delivery chain."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = $1
            AND newer.event_type = $2
            AND newer.payload_hash = $3
            AND newer.attempt_num > $4
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _has_live_successor_attempt(conn: asyncpg.Connection, delivery: asyncpg.Record) -> bool:
    """Return whether a live newer attempt owns the chain's forward direction."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = $1
            AND newer.event_type = $2
            AND newer.payload_hash = $3
            AND newer.attempt_num > $4
            AND newer.status IN ('pending', 'retrying')
            AND NOT newer.superseded
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _has_succeeded_chain_attempt(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
    delivery_id: str,
) -> bool:
    """Return whether any attempt already completed the chain."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries peer
          WHERE peer.subscription_id = $1
            AND peer.event_type = $2
            AND peer.payload_hash = $3
            AND peer.status = 'succeeded'
            AND peer.id <> $4::uuid
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery_id,
    )


async def _abandon_owned_attempt_after_live_successor(
    conn: asyncpg.Connection,
    delivery_id: str,
    lease_token: str,
) -> Optional[asyncpg.Record]:
    """Terminalize an owned preclaimed attempt made obsolete before POST."""
    return await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
          AND lease_expires_at > clock_timestamp()
          AND status IN ('pending', 'retrying')
          AND NOT superseded
        RETURNING id
        """,
        delivery_id,
        lease_token,
    )


async def _abandon_current_attempt_after_succeeded_chain_peer(
    conn: asyncpg.Connection,
    delivery_id: str,
) -> None:
    """Terminalize a lease-free attempt whose chain already converged."""
    await conn.execute(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND status IN ('pending', 'retrying')
          AND NOT superseded
          AND (lease_token IS NULL OR lease_expires_at < clock_timestamp())
        """,
        delivery_id,
    )


async def _abandon_owned_attempt_after_succeeded_chain_peer(
    conn: asyncpg.Connection,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
    *,
    require_unexpired_lease: bool = True,
) -> Optional[asyncpg.Record]:
    """Finalize an active duplicate without extending a completed chain."""
    if require_unexpired_lease:
        return await conn.fetchrow(
            """
            UPDATE webhook_deliveries
            SET status='abandoned',
                superseded=TRUE,
                response_status=$3,
                response_body=$4,
                error=$5,
                lease_token=NULL,
                lease_expires_at=NULL
            WHERE id=$1::uuid
              AND lease_token=$2::uuid
              AND lease_expires_at >= clock_timestamp()
              AND status IN ('pending', 'retrying')
              AND NOT superseded
            RETURNING id
            """,
            delivery_id,
            lease_token,
            result.response_status,
            result.response_body,
            result.error,
        )

    return await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            response_status=$3,
            response_body=$4,
            error=$5,
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
          AND status IN ('pending', 'retrying')
          AND NOT superseded
        RETURNING id
        """,
        delivery_id,
        lease_token,
        result.response_status,
        result.response_body,
        result.error,
    )


async def _find_live_unleased_successor_attempts(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
) -> list[asyncpg.Record]:
    """Return all free live successors that would duplicate this success."""
    return await conn.fetch(
        """
        SELECT newer.id
        FROM webhook_deliveries newer
        WHERE newer.subscription_id = $1
          AND newer.event_type = $2
          AND newer.payload_hash = $3
          AND newer.attempt_num > $4
          AND newer.status IN ('pending', 'retrying')
          AND NOT newer.superseded
          AND (newer.lease_token IS NULL OR newer.lease_expires_at < clock_timestamp())
        ORDER BY newer.attempt_num ASC
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _abandon_live_successor_attempt(conn: asyncpg.Connection, successor_id: str) -> None:
    """Mark a free successor superseded after a predecessor has succeeded."""
    await conn.execute(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND status IN ('pending', 'retrying')
          AND NOT superseded
          AND (lease_token IS NULL OR lease_expires_at < clock_timestamp())
        """,
        successor_id,
    )

"""Webhook dispatcher — records delivery intent, fires HTTP POST, retries on failure.

Usage from handlers:
    from api.webhook_dispatcher import dispatch
    await dispatch(conn, "memory.created", {"memory_id": ..., "content": ...})

Design notes
------------
- Delivery is durable via the `webhook_deliveries` table. A row is written
  before the HTTP call so crashes between queue and send can be replayed.
- Initial attempt runs inline as a background task (asyncio.create_task via
  _schedule_background). On failure, a new delivery row is scheduled at the
  next backoff interval; the failed attempt is marked `retry_scheduled` so it
  remains available for audit without being replayed. A recovery worker
  started at app lifespan wakes periodically and locks due `pending` rows,
  plus due `retrying` rows that do not already have a successor attempt,
  with `FOR UPDATE SKIP LOCKED` before any outbound POST is attempted.
- HMAC-SHA256 signature over the raw JSON body bytes. Receivers verify with
  the per-subscription secret returned once at create time.

Retry schedule: 1 minute, 5 minutes, 30 minutes, 2 hours. After 4 failed
attempts a delivery is marked 'abandoned'.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Retry schedule in seconds: 1m, 5m, 30m, 2h
BACKOFF_SCHEDULE = [60, 300, 1800, 7200]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE)  # = 4
DELIVERY_TIMEOUT = 10.0                # seconds per HTTP POST
RECOVERY_POLL_INTERVAL = 30.0          # seconds between recovery-worker passes
SUPERSEDED_RETRY_STATUS = "retry_scheduled"
TERMINAL_DELIVERY_STATUSES = frozenset((
    "succeeded",
    "abandoned",
    SUPERSEDED_RETRY_STATUS,
))
LIVE_DELIVERY_STATUSES = frozenset(("pending", "retrying"))

WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL = """
    UPDATE webhook_deliveries d
    SET status = 'retry_scheduled'
    WHERE d.status = 'retrying'
      AND EXISTS (
        SELECT 1
        FROM webhook_deliveries newer
        WHERE newer.subscription_id = d.subscription_id
          AND newer.event_type = d.event_type
          AND newer.payload_hash = d.payload_hash
          AND newer.attempt_num > d.attempt_num
      )
"""


# ── Public surface ────────────────────────────────────────────────────────────


async def dispatch(
    conn: asyncpg.Connection,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> None:
    """Fan out an event to all matching subscriptions.

    Records a `webhook_deliveries` row per subscription, then schedules
    each delivery as a background task. Safe to call from inside any
    handler that already has a DB connection.
    """
    subs = await _matching_subscriptions(conn, event_type, owner_id, namespace)
    if not subs:
        return

    body = json.dumps({
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }, separators=(",", ":"), sort_keys=True)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    for sub in subs:
        delivery_id = await conn.fetchval(
            """
            INSERT INTO webhook_deliveries
              (subscription_id, event_type, payload, payload_hash, status)
            VALUES ($1, $2, $3, $4, 'pending')
            RETURNING id
            """,
            sub["id"], event_type, body, body_hash,
        )
        # Schedule the send via the lifecycle-tracked background registry
        # so pending tasks are awaited at shutdown. Import lazily to avoid
        # circular imports at module load time.
        from api.lifecycle import _schedule_background  # noqa: WPS433
        _schedule_background(_attempt_delivery(str(delivery_id)))


async def recovery_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: picks up pending deliveries whose scheduled_at has arrived.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("webhook recovery worker started")
    while True:
        try:
            await asyncio.sleep(RECOVERY_POLL_INTERVAL)
            await _recover_due_deliveries(pool)
        except asyncio.CancelledError:
            logger.info("webhook recovery worker cancelled")
            raise
        except Exception:  # pragma: no cover — log and keep running
            logger.exception("webhook recovery worker iteration failed")


# ── Internals ─────────────────────────────────────────────────────────────────


async def repair_superseded_retrying_deliveries(pool: asyncpg.Pool) -> str:
    """Terminalize retrying attempts that already have a newer successor row."""
    async with pool.acquire() as conn:
        return await conn.execute(WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL)


async def _recover_due_deliveries(pool: asyncpg.Pool, *, limit: int = 50) -> int:
    """Recover due deliveries under row locks, one send transaction at a time."""
    recovered = 0
    for _ in range(limit):
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await _recoverable_delivery_ids(conn, limit=1)
                if not rows:
                    return recovered
                await _attempt_delivery_locked(conn, str(rows[0]["id"]))
                recovered += 1
    return recovered


async def _recoverable_delivery_ids(
    conn: asyncpg.Connection,
    *,
    limit: int = 50,
) -> Iterable[asyncpg.Record]:
    """Return due delivery rows that are safe for recovery to schedule."""
    return await conn.fetch(
        """
        SELECT d.id FROM webhook_deliveries d
        WHERE d.scheduled_at <= NOW()
          AND d.attempt_num <= $1
          AND d.status IN ('pending', 'retrying')
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
        FOR UPDATE SKIP LOCKED
        """,
        MAX_ATTEMPTS, limit,
    )


async def _matching_subscriptions(
    conn: asyncpg.Connection,
    event_type: str,
    owner_id: Optional[str],
    namespace: Optional[str],
) -> Iterable[asyncpg.Record]:
    """Find non-revoked subscriptions that include this event_type.

    If owner_id/namespace are provided, filter to subscriptions with matching
    ownership. Otherwise, return all non-revoked matches (useful for
    system-level events not bound to a caller).
    """
    query = """
        SELECT id, url, events, secret, owner_id, namespace
        FROM webhook_subscriptions
        WHERE NOT revoked AND $1 = ANY(events)
    """
    args: list = [event_type]
    if owner_id is not None:
        query += " AND owner_id = $2"
        args.append(owner_id)
        if namespace is not None:
            query += " AND namespace = $3"
            args.append(namespace)
    return await conn.fetch(query, *args)


async def _attempt_delivery(delivery_id: str) -> None:
    """Fire one HTTP POST for a single delivery row. Update status."""
    from api.lifecycle import _pool  # noqa: WPS433
    if not _pool:
        logger.warning("webhook dispatcher: no DB pool — skipping delivery %s", delivery_id)
        return

    async with _pool.acquire() as conn:
        async with conn.transaction():
            await _attempt_delivery_locked(conn, delivery_id)


async def _attempt_delivery_locked(conn: asyncpg.Connection, delivery_id: str) -> None:
    """Attempt one delivery while holding its row lock for send + finalize."""
    delivery = await _load_delivery_for_attempt(conn, delivery_id)
    if not delivery:
        return
    if delivery["status"] == "retrying" and await _has_successor_attempt(conn, delivery):
        await conn.execute(
            """
            UPDATE webhook_deliveries
            SET status=$2
            WHERE id=$1::uuid AND status='retrying'
            """,
            delivery_id, SUPERSEDED_RETRY_STATUS,
        )
        return
    if delivery["status"] == "pending":
        await conn.execute(
            """
            UPDATE webhook_deliveries
            SET status='retrying'
            WHERE id=$1::uuid AND status='pending'
            """,
            delivery_id,
        )
    if delivery["revoked"]:
        await conn.execute(
            """
            UPDATE webhook_deliveries
            SET status='abandoned',
                error='subscription revoked',
                delivered_at=NOW()
            WHERE id=$1::uuid
            """,
            delivery_id,
        )
        return

    signature = _sign(delivery["secret"], delivery["payload"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MNEMOS-Webhook/1.0",
        "X-MNEMOS-Event": delivery["event_type"],
        "X-MNEMOS-Signature": f"sha256={signature}",
        "X-MNEMOS-Delivery-ID": str(delivery["id"]),
        "X-MNEMOS-Subscription-ID": str(delivery["subscription_id"]),
        "X-MNEMOS-Attempt": str(delivery["attempt_num"]),
    }

    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None

    # Re-validate URL at dispatch time (defense-in-depth against SSRF if a
    # subscription's url field was set outside the handler validation path).
    # This narrows but does not fully close the DNS-rebinding window — see
    # validate_webhook_url's docstring.
    try:
        from api.handlers.webhooks import validate_webhook_url
        await validate_webhook_url(delivery["url"])
    except Exception as e:
        error = f"url-rejected: {type(e).__name__}: {e}"
    else:
        try:
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT, follow_redirects=False) as client:
                r = await client.post(
                    delivery["url"],
                    content=delivery["payload"].encode("utf-8"),
                    headers=headers,
                )
                response_status = r.status_code
                response_body = r.text[:2048]
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"
        except Exception as e:  # pragma: no cover
            error = f"{type(e).__name__}: {e}"

    succeeded = response_status is not None and 200 <= response_status < 300

    if succeeded:
        await conn.execute(
            """
            UPDATE webhook_deliveries
            SET status='succeeded',
                response_status=$2,
                response_body=$3,
                delivered_at=NOW()
            WHERE id=$1::uuid
            """,
            delivery_id, response_status, response_body,
        )
        return

    next_attempt = delivery["attempt_num"] + 1
    if next_attempt > MAX_ATTEMPTS:
        await conn.execute(
            """
            UPDATE webhook_deliveries
            SET status='abandoned',
                response_status=$2,
                response_body=$3,
                error=$4,
                delivered_at=NOW()
            WHERE id=$1::uuid
            """,
            delivery_id, response_status, response_body, error,
        )
        return

    # Enqueue the next attempt row, then terminalize this failed attempt.
    backoff = BACKOFF_SCHEDULE[delivery["attempt_num"] - 1]
    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
    await conn.execute(
        """
        INSERT INTO webhook_deliveries
          (subscription_id, event_type, payload, payload_hash,
           attempt_num, status, scheduled_at)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6)
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload"],
        delivery["payload_hash"],
        next_attempt,
        scheduled_at,
    )
    await conn.execute(
        """
        UPDATE webhook_deliveries
        SET status=$2,
            response_status=$3,
            response_body=$4,
            error=$5
        WHERE id=$1::uuid
        """,
        delivery_id, SUPERSEDED_RETRY_STATUS, response_status, response_body, error,
    )
    logger.info(
        "webhook delivery %s attempt %d failed (status=%s error=%s), retry in %ds",
        delivery_id, delivery["attempt_num"], response_status, error, backoff,
    )


async def _load_delivery_for_attempt(conn: asyncpg.Connection, delivery_id: str) -> Optional[asyncpg.Record]:
    """Load and lock a due live delivery row, skipping rows another worker owns."""
    return await conn.fetchrow(
        """
        SELECT d.id, d.subscription_id, d.event_type, d.payload,
               d.payload_hash, d.attempt_num, d.status,
               s.url, s.secret, s.revoked
        FROM webhook_deliveries d
        JOIN webhook_subscriptions s ON s.id = d.subscription_id
        WHERE d.id = $1::uuid
          AND d.scheduled_at <= NOW()
          AND d.attempt_num <= $2
          AND d.status IN ('pending', 'retrying')
        FOR UPDATE OF d SKIP LOCKED
        """,
        delivery_id, MAX_ATTEMPTS,
    )


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


def _sign(secret: str, body: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

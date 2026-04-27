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
  next backoff interval; the failed attempt is marked `abandoned` with
  `superseded=TRUE` so old workers skip it while audit queries can distinguish
  retry-chain advancement from final-attempt failure. Workers claim due rows
  by writing a short-lived lease, release the database connection, and only
  then perform DNS validation and the outbound POST. The lease is the
  authoritative delivery budget: DNS validation, HTTP send, and capped response
  body read run under one wall-clock deadline derived from the lease, leaving a
  buffer for the finalize transaction before recovery can reclaim the attempt.
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
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Retry schedule in seconds: 1m, 5m, 30m, 2h
BACKOFF_SCHEDULE = [60, 300, 1800, 7200]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE)  # = 4
DNS_TIMEOUT = float(os.getenv("WEBHOOK_DNS_TIMEOUT", "10.0"))
DELIVERY_TIMEOUT = float(os.getenv("WEBHOOK_HTTP_TIMEOUT", "10.0"))
WEBHOOK_LEASE_SECONDS = int(os.getenv(
    "WEBHOOK_LEASE_SECONDS",
    str(max(90, int(DNS_TIMEOUT + DELIVERY_TIMEOUT + 30))),
))
WEBHOOK_FINALIZE_BUFFER_SECONDS = float(os.getenv("WEBHOOK_FINALIZE_BUFFER_SECONDS", "5.0"))
WEBHOOK_RESPONSE_BODY_MAX_BYTES = int(os.getenv("WEBHOOK_RESPONSE_BODY_MAX_BYTES", "2048"))
WEBHOOK_MAX_CONCURRENT_SENDS = int(os.getenv("WEBHOOK_MAX_CONCURRENT_SENDS", "64"))
WEBHOOK_LEGACY_GRACE_SECONDS = int(os.getenv("WEBHOOK_LEGACY_GRACE_SECONDS", "300"))
NEW_CODE_WRITER_REVISION = 1
NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES = 256
MIN_SEND_WINDOW_SECONDS = 1.0
RECOVERY_POLL_INTERVAL = 30.0          # seconds between recovery-worker passes
REPAIR_BURST_SECONDS = float(os.getenv("WEBHOOK_REPAIR_BURST_SECONDS", "60.0"))
REPAIR_BURST_INTERVAL = float(os.getenv("WEBHOOK_REPAIR_BURST_INTERVAL", "5.0"))
REPAIR_PERIODIC_INTERVAL = float(os.getenv("WEBHOOK_REPAIR_PERIODIC_INTERVAL", "300.0"))
TERMINAL_DELIVERY_STATUSES = frozenset((
    "succeeded",
    "abandoned",
))
LIVE_DELIVERY_STATUSES = frozenset(("pending", "retrying"))
_send_semaphore: asyncio.Semaphore | None = None


def _derive_total_send_deadline_seconds(
    lease_seconds: int,
    finalize_buffer_seconds: float,
) -> float:
    """Derive the single wall-clock DNS+HTTP budget from the attempt lease."""
    if finalize_buffer_seconds <= 0:
        raise ValueError("WEBHOOK_FINALIZE_BUFFER_SECONDS must be positive")
    deadline = float(lease_seconds) - finalize_buffer_seconds
    if deadline <= 0:
        raise ValueError(
            "WEBHOOK_LEASE_SECONDS must be greater than WEBHOOK_FINALIZE_BUFFER_SECONDS "
            "so webhook sends leave time for finalization before lease expiry"
        )
    return deadline


if WEBHOOK_RESPONSE_BODY_MAX_BYTES <= 0:
    raise ValueError("WEBHOOK_RESPONSE_BODY_MAX_BYTES must be positive")
if WEBHOOK_LEGACY_GRACE_SECONDS < 0:
    raise ValueError("WEBHOOK_LEGACY_GRACE_SECONDS must be non-negative")

# Keep the lease as the only operator-facing ownership budget. This derived
# value validates startup configuration; actual sends use the DB-returned claim
# timestamp pair so an app pause after claim cannot spend a stale full budget.
TOTAL_SEND_DEADLINE_SECONDS = _derive_total_send_deadline_seconds(
    WEBHOOK_LEASE_SECONDS,
    WEBHOOK_FINALIZE_BUFFER_SECONDS,
)

WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL = """
    UPDATE webhook_deliveries d
    SET status = 'abandoned',
        superseded = TRUE,
        status_updated_at = clock_timestamp(),
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE d.status IN ('pending', 'retrying')
      AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
      AND EXISTS (
        SELECT 1
        FROM webhook_deliveries newer
        WHERE newer.subscription_id = d.subscription_id
          AND newer.event_type = d.event_type
          AND newer.payload_hash = d.payload_hash
          AND newer.attempt_num > d.attempt_num
      )
"""


@dataclass(frozen=True)
class _DeliveryResult:
    succeeded: bool
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class _ClaimedDelivery:
    delivery: asyncpg.Record
    pre_claim_monotonic: float


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
              (subscription_id, event_type, payload, payload_hash, status, writer_revision)
            VALUES ($1, $2, $3, $4, 'pending', $5)
            RETURNING id
            """,
            sub["id"], event_type, body, body_hash, NEW_CODE_WRITER_REVISION,
        )
        # Schedule the send via the lifecycle-tracked delivery registry
        # so graceful shutdown lets in-flight attempts finalize. Import lazily to avoid
        # circular imports at module load time.
        from api.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
        _schedule_delivery_attempt(_attempt_delivery(str(delivery_id)))


async def repair_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: repair superseded retry rows on its own cadence.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("webhook retry repair worker started")
    loop = asyncio.get_running_loop()
    repair_burst_deadline = loop.time() + REPAIR_BURST_SECONDS
    next_repair_at = loop.time()
    while True:
        try:
            now = loop.time()
            if now >= next_repair_at:
                in_burst = now < repair_burst_deadline
                await _repair_superseded_retrying_deliveries_safely(
                    pool,
                    phase="burst" if in_burst else "periodic",
                )
                next_repair_at = now + (
                    REPAIR_BURST_INTERVAL if in_burst else REPAIR_PERIODIC_INTERVAL
                )

            await asyncio.sleep(max(0.0, next_repair_at - loop.time()))
        except asyncio.CancelledError:
            logger.info("webhook retry repair worker cancelled")
            raise
        except Exception:  # pragma: no cover — log and keep running
            logger.exception("webhook retry repair worker iteration failed")
            await asyncio.sleep(REPAIR_BURST_INTERVAL)


async def delivery_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: picks up pending deliveries whose scheduled_at has arrived.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("webhook delivery recovery worker started")
    while True:
        try:
            await _recover_due_deliveries(pool)
            await asyncio.sleep(RECOVERY_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("webhook delivery recovery worker cancelled")
            raise
        except Exception:  # pragma: no cover — log and keep running
            logger.exception("webhook delivery recovery worker iteration failed")
            await asyncio.sleep(RECOVERY_POLL_INTERVAL)


async def recovery_worker_loop(pool: asyncpg.Pool) -> None:
    """Compatibility wrapper for the delivery recovery loop."""
    await delivery_worker_loop(pool)


# ── Internals ─────────────────────────────────────────────────────────────────


async def _repair_superseded_retrying_deliveries_safely(
    pool: asyncpg.Pool,
    *,
    phase: str,
) -> None:
    """Run one repair sweep without killing the recovery worker on failure."""
    try:
        result = await repair_superseded_retrying_deliveries(pool)
        logger.info("webhook retry repair %s sweep result: %s", phase, result)
    except Exception:  # pragma: no cover — log and keep running
        logger.exception("webhook retry repair %s sweep failed", phase)


async def repair_superseded_retrying_deliveries(pool: asyncpg.Pool) -> str:
    """Terminalize live-looking attempts that already have a newer successor row."""
    async with pool.acquire() as conn:
        return await conn.execute(WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL)


async def _recover_due_deliveries(pool: asyncpg.Pool, *, limit: int = 50) -> int:
    """Recover due deliveries by taking short leases before each send."""
    recovered = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await _recoverable_delivery_ids(conn, limit=limit)
    for row in rows:
        if await _attempt_delivery(str(row["id"]), pool=pool):
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
        WHERE d.scheduled_at <= clock_timestamp()
          AND d.attempt_num <= $1
          AND d.status NOT IN ('succeeded', 'abandoned')
          AND NOT d.superseded
          AND d.status IN ('pending', 'retrying')
          AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
          AND (
            d.lease_token IS NOT NULL
            OR d.writer_revision = $4
            OR d.status_updated_at + ($3::int * INTERVAL '1 second') <= clock_timestamp()
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
        FOR UPDATE SKIP LOCKED
        """,
        MAX_ATTEMPTS, limit, WEBHOOK_LEGACY_GRACE_SECONDS, NEW_CODE_WRITER_REVISION,
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


def _get_send_semaphore() -> asyncio.Semaphore:
    global _send_semaphore
    if _send_semaphore is None:
        _send_semaphore = asyncio.Semaphore(max(1, WEBHOOK_MAX_CONCURRENT_SENDS))
    return _send_semaphore


async def _attempt_delivery(delivery_id: str, *, pool: Optional[asyncpg.Pool] = None) -> bool:
    """Claim, send, and finalize one delivery without holding DB during I/O."""
    if pool is None:
        from api.lifecycle import _pool as lifecycle_pool  # noqa: WPS433
        pool = lifecycle_pool
    if not pool:
        logger.warning("webhook dispatcher: no DB pool — skipping delivery %s", delivery_id)
        return False

    async with _get_send_semaphore():
        lease_token = str(uuid.uuid4())
        claimed = await _claim_delivery(pool, delivery_id, lease_token=lease_token)
        if not claimed:
            return False
        result = await _send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        return await _finalize_delivery(pool, claimed.delivery, lease_token, result)


async def _claim_delivery(
    pool: asyncpg.Pool,
    delivery_id: str,
    *,
    lease_token: str,
    lease_seconds: int | None = None,
) -> Optional[_ClaimedDelivery]:
    """Persist a short lease for one due live delivery row."""
    if lease_seconds is None:
        lease_seconds = WEBHOOK_LEASE_SECONDS
    async with pool.acquire() as conn:
        async with conn.transaction():
            delivery = await _load_delivery_for_claim(conn, delivery_id)
            if not delivery:
                return None

            await _lock_delivery_chain(conn, delivery)
            if delivery["status"] == "retrying" and await _has_successor_attempt(conn, delivery):
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
                  AND (
                    d.lease_token IS NOT NULL
                    OR d.writer_revision = $6
                    OR d.status_updated_at + ($5::int * INTERVAL '1 second') <= claim_clock.claim_now
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
                RETURNING d.id, d.subscription_id, d.event_type, d.payload,
                          d.payload_hash, d.attempt_num, d.status,
                          d.lease_expires_at, claim_clock.claim_now AS claim_db_now,
                          s.url, s.secret, s.revoked
                """,
                delivery_id,
                lease_token,
                lease_seconds,
                MAX_ATTEMPTS,
                WEBHOOK_LEGACY_GRACE_SECONDS,
                NEW_CODE_WRITER_REVISION,
            )
            if claimed is None:
                return None
            return _ClaimedDelivery(
                delivery=claimed,
                pre_claim_monotonic=pre_claim_monotonic,
            )


async def _send_claimed_delivery(
    delivery: asyncpg.Record,
    *,
    pre_claim_monotonic: float,
) -> _DeliveryResult:
    """Perform DNS validation and HTTP POST for an already leased row."""
    remaining_seconds = _claim_remaining_send_window_seconds(
        delivery,
        pre_claim_monotonic=pre_claim_monotonic,
    )
    if remaining_seconds <= MIN_SEND_WINDOW_SECONDS:
        return _DeliveryResult(
            succeeded=False,
            error=(
                "lease-expired-before-send: remaining lease send window "
                f"{remaining_seconds:.3f}s <= {MIN_SEND_WINDOW_SECONDS:.1f}s minimum"
            ),
        )

    try:
        return await asyncio.wait_for(
            _send_claimed_delivery_within_deadline(delivery),
            timeout=remaining_seconds,
        )
    except asyncio.TimeoutError:
        return _DeliveryResult(
            succeeded=False,
            error=(
                "send-timeout: DNS validation, HTTP send, and response body read "
                f"exceeded {remaining_seconds:.1f}s lease-anchored wall-clock deadline"
            ),
        )


async def _send_claimed_delivery_within_deadline(delivery: asyncpg.Record) -> _DeliveryResult:
    """Run the network send path; caller supplies the wall-clock deadline."""
    if not delivery:
        return _DeliveryResult(succeeded=False, error="delivery not found")
    if delivery["revoked"]:
        return _DeliveryResult(succeeded=False, error="subscription revoked")

    signature = _sign(delivery["secret"], delivery["payload"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MNEMOS-Webhook/1.0",
        "Accept-Encoding": "identity",
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
                async with client.stream(
                    "POST",
                    delivery["url"],
                    content=delivery["payload"].encode("utf-8"),
                    headers=headers,
                ) as response:
                    response_status = response.status_code
                    response_body = await _read_capped_response_body(response)
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"
        except Exception as e:  # pragma: no cover
            error = f"{type(e).__name__}: {e}"

    succeeded = response_status is not None and 200 <= response_status < 300
    return _DeliveryResult(
        succeeded=succeeded,
        response_status=response_status,
        response_body=response_body,
        error=error,
    )


async def _read_capped_response_body(response: httpx.Response) -> str:
    """Read bounded raw response bytes; never transparently decompress."""
    headers = getattr(response, "headers", {})
    content_encoding = str(headers.get("content-encoding", "identity") or "identity").strip().lower()
    if content_encoding not in ("", "identity"):
        # Receivers may ignore Accept-Encoding: identity; retain only raw bytes
        # so a compressed response cannot inflate before the audit cap applies.
        raw = await _read_capped_raw_response_body(
            response,
            max_bytes=min(WEBHOOK_RESPONSE_BODY_MAX_BYTES, NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES),
        )
        return _decode_capped_response_body(raw, WEBHOOK_RESPONSE_BODY_MAX_BYTES)

    raw = await _read_capped_raw_response_body(response, max_bytes=WEBHOOK_RESPONSE_BODY_MAX_BYTES)
    return _decode_capped_response_body(raw, WEBHOOK_RESPONSE_BODY_MAX_BYTES)


async def _read_capped_raw_response_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read at most max_bytes raw bytes from a streamed response."""
    remaining = max_bytes
    body = bytearray()
    async for chunk in response.aiter_raw():
        if not chunk:
            continue
        body.extend(chunk[:remaining])
        remaining -= min(len(chunk), remaining)
        if remaining <= 0:
            break
    return bytes(body)


def _decode_capped_response_body(raw: bytes, max_bytes: int) -> str:
    """Decode for TEXT storage while preserving the configured UTF-8 byte cap."""
    text = raw.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    used = 0
    out: list[str] = []
    for char in text:
        char_size = len(char.encode("utf-8"))
        if used + char_size > max_bytes:
            break
        out.append(char)
        used += char_size
    return "".join(out)


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
    return db_claim_window - elapsed_since_pre_claim - WEBHOOK_FINALIZE_BUFFER_SECONDS


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize asyncpg timestamp values for arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _finalize_delivery(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Persist the send result only if this worker still owns the lease."""
    delivery_id = str(delivery["id"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_delivery_chain(conn, delivery)

            if delivery["revoked"]:
                finalized = await conn.fetchrow(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=FALSE,
                        error='subscription revoked',
                        delivered_at=clock_timestamp(),
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid
                      AND lease_token=$2::uuid
                      AND lease_expires_at >= clock_timestamp()
                    RETURNING id
                    """,
                    delivery_id, lease_token,
                )
                return finalized is not None

            if result.succeeded:
                finalized = await conn.fetchrow(
                    """
                    UPDATE webhook_deliveries
                    SET status='succeeded',
                        superseded=FALSE,
                        response_status=$3,
                        response_body=$4,
                        error=NULL,
                        delivered_at=clock_timestamp(),
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid
                      AND lease_token=$2::uuid
                      AND lease_expires_at >= clock_timestamp()
                    RETURNING id
                    """,
                    delivery_id, lease_token, result.response_status, result.response_body,
                )
                if finalized is None:
                    return False

                # A live successor after our lease-valid 2xx is a mixed-version
                # artifact. This attempt already produced the canonical external delivery,
                # so cancel a free successor instead of re-posting it.
                successor = await _find_live_unleased_successor_attempt(conn, delivery)
                if successor is not None:
                    await _abandon_live_successor_attempt(conn, str(successor["id"]))
                return True

            next_attempt = delivery["attempt_num"] + 1
            if next_attempt > MAX_ATTEMPTS:
                finalized = await conn.fetchrow(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=FALSE,
                        response_status=$3,
                        response_body=$4,
                        error=$5,
                        delivered_at=clock_timestamp(),
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid
                      AND lease_token=$2::uuid
                      AND lease_expires_at >= clock_timestamp()
                    RETURNING id
                    """,
                    delivery_id, lease_token, result.response_status, result.response_body, result.error,
                )
                return finalized is not None

            backoff = BACKOFF_SCHEDULE[delivery["attempt_num"] - 1]
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            successor_exists = await _has_successor_attempt(conn, delivery)
            finalized = await conn.fetchrow(
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
                RETURNING id
                """,
                delivery_id,
                lease_token,
                result.response_status,
                result.response_body,
                result.error,
            )
            if finalized is None:
                logger.info(
                    "webhook delivery %s finalize skipped because lease expired or moved",
                    delivery_id,
                )
                return False

            if not successor_exists:
                await _insert_successor_delivery(conn, delivery, next_attempt, scheduled_at)
            logger.info(
                "webhook delivery %s attempt %d failed (status=%s error=%s), retry in %ds",
                delivery_id, delivery["attempt_num"], result.response_status, result.error, backoff,
            )
            return True


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
        delivery_id, MAX_ATTEMPTS,
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
        NEW_CODE_WRITER_REVISION,
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


async def _find_live_unleased_successor_attempt(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
) -> Optional[asyncpg.Record]:
    """Return a free live successor that would duplicate a succeeded predecessor."""
    return await conn.fetchrow(
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
        LIMIT 1
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


def _sign(secret: str, body: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

"""Webhook delivery terminal state transitions."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import chain as webhook_chain
from . import lease as webhook_lease
from . import types as webhook_types
from .types import _DeliveryResult, _LeaseExpiredBeforeSend, _PostHeaderDeliveryResult

logger = logging.getLogger(__name__)


def _pool_uses_sqlite_backend(pool: Any) -> bool:
    backend = getattr(pool, "persistence_backend", None)
    if backend is None:
        try:
            from mnemos.core.lifecycle import get_persistence_backend

            backend = get_persistence_backend()
        except Exception:
            return False
    return bool(getattr(backend, "uses_sqlite_vec", False))


async def _guard_sqlite_succeeded_terminal(
    conn: Any,
    pool: Any,
    delivery_id: str,
    attempted_status: str,
) -> bool:
    """Return True when SQLite app-level terminal-state enforcement blocks an update."""
    if attempted_status == "succeeded" or not _pool_uses_sqlite_backend(pool):
        return False

    row = None
    for sql, args in (
        ("SELECT status FROM webhook_deliveries WHERE id=$1::uuid", (delivery_id,)),
        ("SELECT status FROM webhook_deliveries WHERE id = ?", (delivery_id,)),
    ):
        try:
            row = await conn.fetchrow(sql, *args)
            break
        except Exception:
            continue
    if row is not None and row["status"] == "succeeded":
        logger.warning(
            "webhook delivery %s SQLite terminal-state guard blocked status=%s",
            delivery_id,
            attempted_status,
        )
        return True
    return False


async def _finalize_delivery(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Finalize the delivery row before post-header body capture or cleanup."""
    finalized = False
    try:
        finalized = await _finalize_delivery_row(pool, delivery, lease_token, result)
        return finalized
    finally:
        if isinstance(result, _PostHeaderDeliveryResult):
            await _run_post_finalize_delivery_work(
                pool,
                result,
                finalized=finalized,
            )


async def _finalize_delivery_row(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Persist the send result while preserving the attempt ownership rules."""
    delivery_id = str(delivery["id"])
    if isinstance(result, _LeaseExpiredBeforeSend):
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await webhook_lease._release_owned_lease_for_reclaim(conn, delivery_id, lease_token)

    if result.succeeded and not delivery["revoked"]:
        return await _finalize_successful_delivery_row(
            pool,
            delivery,
            delivery_id,
            lease_token,
            result,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)

            if await _guard_sqlite_succeeded_terminal(conn, pool, delivery_id, "abandoned"):
                return False

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
                      AND status IN ('pending', 'retrying')
                      AND NOT superseded
                    RETURNING id
                    """,
                    delivery_id, lease_token,
                )
                return finalized is not None

            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    result,
                )
                return finalized is not None

            next_attempt = delivery["attempt_num"] + 1
            if next_attempt > webhook_types.MAX_ATTEMPTS:
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
                      AND status IN ('pending', 'retrying')
                      AND NOT superseded
                    RETURNING id
                    """,
                    delivery_id, lease_token, result.response_status, result.response_body, result.error,
                )
                return finalized is not None

            backoff = webhook_types.BACKOFF_SCHEDULE[delivery["attempt_num"] - 1]
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            successor_exists = await webhook_chain._has_successor_attempt(conn, delivery)
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
            if finalized is None:
                logger.info(
                    "webhook delivery %s finalize skipped because lease expired or moved",
                    delivery_id,
                )
                return False

            if not successor_exists:
                await webhook_chain._insert_successor_delivery(conn, delivery, next_attempt, scheduled_at)
            logger.info(
                "webhook delivery %s attempt %d failed (status=%s error=%s), retry in %ds",
                delivery_id, delivery["attempt_num"], result.response_status, result.error, backoff,
            )
            return True


async def _commit_successful_delivery_row(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Commit a 2xx result with same-transaction successor convergence."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)

            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    result,
                    require_unexpired_lease=False,
                )
                return finalized is not None

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
                  AND status IN ('pending', 'retrying')
                  AND NOT superseded
                RETURNING id
                """,
                delivery_id,
                lease_token,
                result.response_status,
                result.response_body,
            )
            if finalized is None:
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    result,
                    require_unexpired_lease=False,
                )
                if finalized is not None:
                    return True
                await webhook_lease._clear_stale_owned_lease_after_terminal_finalize(
                    conn,
                    delivery_id,
                    lease_token,
                )
                return False

            await _abandon_live_successors_before_success_commit(conn, delivery)
            return True


async def _finalize_successful_delivery_row(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Persist a 2xx ACK with atomic successor convergence."""
    try:
        finalized = await _commit_successful_delivery_row(
            pool,
            delivery,
            delivery_id,
            lease_token,
            result,
        )
    except asyncpg.exceptions.UniqueViolationError:
        return await _abandon_success_duplicate_after_unique_violation(
            pool,
            delivery,
            delivery_id,
            lease_token,
            result,
        )
    except asyncio.CancelledError:
        logger.warning(
            "webhook delivery %s success finalization cancelled before commit after 2xx ACK; "
            "success transaction rolled back and retry may resend a bounded duplicate POST",
            delivery_id,
            exc_info=True,
        )
        raise
    except Exception:
        logger.warning(
            "webhook delivery %s success finalization failed before commit after 2xx ACK; "
            "success transaction rolled back and retry may resend a bounded duplicate POST",
            delivery_id,
            exc_info=True,
        )
        raise

    return finalized


async def _abandon_live_successors_before_success_commit(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
) -> None:
    """Abandon free successors inside the success transaction."""
    successors = await webhook_chain._find_live_unleased_successor_attempts(conn, delivery)
    for successor in successors:
        await webhook_chain._abandon_live_successor_attempt(conn, str(successor["id"]))


async def _abandon_success_duplicate_after_unique_violation(
    pool: asyncpg.Pool,
    delivery: asyncpg.Record,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Converge a duplicate 2xx after the succeeded-chain unique index wins."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)
            finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                conn,
                delivery_id,
                lease_token,
                result,
                require_unexpired_lease=False,
            )
            if finalized is None:
                await webhook_lease._clear_stale_owned_lease_after_terminal_finalize(
                    conn,
                    delivery_id,
                    lease_token,
                )
                return False
            return True


async def _run_post_finalize_delivery_work(
    pool: asyncpg.Pool,
    result: _PostHeaderDeliveryResult,
    *,
    finalized: bool,
) -> None:
    """Capture audit body and close HTTP resources after the DB result is durable."""
    from . import sender as webhook_sender

    try:
        if finalized:
            response_body = await webhook_sender._capture_response_body_for_audit(
                result.response,
                delivery_id=result.delivery_id,
            )
            if response_body is not None:
                await _persist_response_body_for_audit(
                    pool,
                    delivery_id=result.delivery_id,
                    response_body=response_body,
                )
    finally:
        if result.stream_cm is not None:
            await webhook_sender._run_post_header_cleanup(
                result.stream_cm.__aexit__(None, None, None),
                delivery_id=result.delivery_id,
                cleanup_name="stream",
                result=result,
            )
        if result.client_cm is not None:
            await webhook_sender._run_post_header_cleanup(
                result.client_cm.__aexit__(None, None, None),
                delivery_id=result.delivery_id,
                cleanup_name="client",
                result=result,
            )


async def _persist_response_body_for_audit(
    pool: asyncpg.Pool,
    *,
    delivery_id: Any,
    response_body: str,
) -> None:
    """Store post-finalize response audit data without lease ownership checks."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET response_body=$2
                WHERE id=$1::uuid
                """,
                str(delivery_id),
                response_body,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "webhook delivery %s response-body audit update failed after finalization",
            delivery_id,
            exc_info=True,
        )

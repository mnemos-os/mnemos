"""GDPR deletion-request workers.

Consumes ``deletion_requests.status='confirmed'`` rows, sweeps the
target user's rows, verifies that no live rows escaped the first pass,
and then marks the request ``soft_deleted`` inside one transaction.
If the transaction aborts, the request remains ``confirmed`` and the
next worker pass retries it.

The hard-delete phase consumes expired ``soft_deleted`` rows and
permanently removes rows already marked with ``deleted_at``. The
``deletion_requests`` row is never deleted; it is the audit breadcrumb
proving the wipe completed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10
DEFAULT_CHECK_INTERVAL_SECONDS = 30.0
DEFAULT_VERIFY_ATTEMPTS = 5
RESTORE_GRACE_DAYS = 30

_DEQUEUE_SQL = """
SELECT id, target_user_id, target_namespace
  FROM deletion_requests
 WHERE status = 'confirmed'
 ORDER BY confirmed_at ASC NULLS FIRST, requested_at ASC
 FOR UPDATE SKIP LOCKED
 LIMIT 1
"""

_DEQUEUE_HARD_DELETE_SQL = """
SELECT id, target_user_id, target_namespace
  FROM deletion_requests
 WHERE status = 'soft_deleted'
   AND restore_by < NOW()
 FOR UPDATE SKIP LOCKED
 LIMIT 1
"""

_MARK_SOFT_DELETED_SQL = """
UPDATE deletion_requests
   SET status = 'soft_deleted',
       soft_deleted_at = NOW(),
       restore_by = NOW() + ($2::int * INTERVAL '1 day')
 WHERE id = $1
   AND status = 'sweep_verifying'
RETURNING id, soft_deleted_at, restore_by
"""

_MARK_SWEEP_VERIFYING_SQL = """
UPDATE deletion_requests
   SET status = 'sweep_verifying'
 WHERE id = $1
   AND status = 'confirmed'
RETURNING id
"""

_MARK_HARD_DELETED_SQL = """
UPDATE deletion_requests
   SET status = 'hard_deleted',
       hard_deleted_at = NOW()
 WHERE id = $1
   AND status = 'soft_deleted'
RETURNING *
"""

_OWNER_NAMESPACE_SOFT_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
    (
        "memories",
        "memories",
        """
        UPDATE memories
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_versions",
        "memory_versions",
        """
        UPDATE memory_versions
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "kg_triples",
        "kg_triples",
        """
        UPDATE kg_triples
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "journal",
        "journal",
        """
        UPDATE journal
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "entities",
        "entities",
        """
        UPDATE entities
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "state",
        "state",
        """
        UPDATE state
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_consultations",
        "graeae_consultations",
        """
        UPDATE graeae_consultations
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
)

_SOFT_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
    (
        "memory_branches",
        "memory_branches",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        UPDATE memory_branches mb
           SET deleted_at = NOW()
          FROM target_memories tm
         WHERE mb.memory_id = tm.id
           AND mb.deleted_at IS NULL
        """,
    ),
    (
        "session_messages",
        "session_messages",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        UPDATE session_messages sm
           SET deleted_at = NOW()
          FROM target_sessions ts
         WHERE sm.session_id = ts.id
           AND sm.deleted_at IS NULL
        """,
    ),
    (
        "session_memory_injections",
        "session_memory_injections",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        UPDATE session_memory_injections smi
           SET deleted_at = NOW()
          FROM target_sessions ts
         WHERE smi.session_id = ts.id
           AND smi.deleted_at IS NULL
        """,
    ),
    (
        "sessions",
        "sessions",
        """
        UPDATE sessions
           SET deleted_at = NOW()
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_audit_log",
        "graeae_audit_log",
        """
        WITH target_consultations AS (
            SELECT id
              FROM graeae_consultations
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        UPDATE graeae_audit_log al
           SET deleted_at = NOW()
          FROM target_consultations tc
         WHERE al.consultation_id = tc.id
           AND al.deleted_at IS NULL
        """,
    ),
)

# Hard-delete order is intentional for FK safety. Child tables go first
# (memory_versions, memory_branches, session_messages,
# session_memory_injections, graeae_audit_log), then parent tables
# (memories, sessions, graeae_consultations), then the remaining
# owner/namespace-scoped tables. The worker also SET LOCALs
# mnemos.suppress_version_snapshot before the DELETEs so the memory
# versioning trigger does not synthesize a fresh delete-version row
# during GDPR erasure.
_HARD_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
    (
        "memory_versions",
        "memory_versions",
        """
        DELETE FROM memory_versions
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "memory_branches",
        "memory_branches",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM memory_branches mb
         USING target_memories tm
         WHERE mb.memory_id = tm.id
           AND mb.deleted_at IS NOT NULL
        """,
    ),
    (
        "session_messages",
        "session_messages",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM session_messages sm
         USING target_sessions ts
         WHERE sm.session_id = ts.id
           AND sm.deleted_at IS NOT NULL
        """,
    ),
    (
        "session_memory_injections",
        "session_memory_injections",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM session_memory_injections smi
         USING target_sessions ts
         WHERE smi.session_id = ts.id
           AND smi.deleted_at IS NOT NULL
        """,
    ),
    (
        "graeae_audit_log",
        "graeae_audit_log",
        """
        WITH target_consultations AS (
            SELECT id
              FROM graeae_consultations
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM graeae_audit_log al
         USING target_consultations tc
         WHERE al.consultation_id = tc.id
           AND al.deleted_at IS NOT NULL
        """,
    ),
    (
        "memories",
        "memories",
        """
        DELETE FROM memories
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "sessions",
        "sessions",
        """
        DELETE FROM sessions
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "graeae_consultations",
        "graeae_consultations",
        """
        DELETE FROM graeae_consultations
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "kg_triples",
        "kg_triples",
        """
        DELETE FROM kg_triples
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "journal",
        "journal",
        """
        DELETE FROM journal
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "entities",
        "entities",
        """
        DELETE FROM entities
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "state",
        "state",
        """
        DELETE FROM state
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
)

_RESTORE_OWNER_NAMESPACE_SQL: tuple[tuple[str, str, str], ...] = tuple(
    (
        label,
        table,
        sql.replace("SET deleted_at = NOW()", "SET deleted_at = NULL")
        .replace("AND deleted_at IS NULL", "AND deleted_at = $3::timestamptz")
        .replace("AND mb.deleted_at IS NULL", "AND mb.deleted_at = $3::timestamptz")
        .replace("AND sm.deleted_at IS NULL", "AND sm.deleted_at = $3::timestamptz")
        .replace("AND smi.deleted_at IS NULL", "AND smi.deleted_at = $3::timestamptz")
        .replace("AND al.deleted_at IS NULL", "AND al.deleted_at = $3::timestamptz"),
    )
    for label, table, sql in (*_OWNER_NAMESPACE_SOFT_DELETE_SQL, *_SOFT_DELETE_SQL)
)

_LIVE_ROW_COUNT_SQL: tuple[tuple[str, str], ...] = (
    (
        "memories",
        """
        SELECT COUNT(*)
          FROM memories
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_versions",
        """
        SELECT COUNT(*)
          FROM memory_versions
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "kg_triples",
        """
        SELECT COUNT(*)
          FROM kg_triples
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "journal",
        """
        SELECT COUNT(*)
          FROM journal
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "entities",
        """
        SELECT COUNT(*)
          FROM entities
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "state",
        """
        SELECT COUNT(*)
          FROM state
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_consultations",
        """
        SELECT COUNT(*)
          FROM graeae_consultations
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_branches",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM memory_branches mb
          JOIN target_memories tm ON tm.id = mb.memory_id
         WHERE mb.deleted_at IS NULL
        """,
    ),
    (
        "session_messages",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM session_messages sm
          JOIN target_sessions ts ON ts.id = sm.session_id
         WHERE sm.deleted_at IS NULL
        """,
    ),
    (
        "session_memory_injections",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM session_memory_injections smi
          JOIN target_sessions ts ON ts.id = smi.session_id
         WHERE smi.deleted_at IS NULL
        """,
    ),
    (
        "sessions",
        """
        SELECT COUNT(*)
          FROM sessions
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_audit_log",
        """
        WITH target_consultations AS (
            SELECT id
              FROM graeae_consultations
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM graeae_audit_log al
          JOIN target_consultations tc ON tc.id = al.consultation_id
         WHERE al.deleted_at IS NULL
        """,
    ),
)


@dataclass(frozen=True)
class DeletionRequestResult:
    request_id: str
    target_user_id: str
    target_namespace: str | None
    status: str
    row_counts: dict[str, int]
    soft_deleted_at: Any
    restore_by: Any
    hard_deleted_at: Any = None
    verification_attempts: int = 0
    remaining_counts: dict[str, int] | None = None


def _parse_update_count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


async def invalidate_deletion_scope_caches(
    target_user_id: str,
    target_namespace: str | None,
) -> None:
    """Evict cached search/stat responses that may include this target."""
    import mnemos.core.lifecycle as _lc

    if not _lc._cache:
        return
    try:
        await _lc._cache.delete("stats:global")
        await _lc._cache.delete("stats:global:v2")
        try:
            async for key in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                await _lc._cache.delete(key)
        except Exception:
            pass
    except Exception:
        logger.warning(
            "failed to invalidate deletion caches for target_user_id=%s target_namespace=%s",
            target_user_id,
            target_namespace,
            exc_info=True,
        )


async def count_live_target_rows(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, sql in _LIVE_ROW_COUNT_SQL:
        counts[label] = int(await conn.fetchval(sql, target_user_id, target_namespace) or 0)
    return counts


def _has_live_rows(counts: dict[str, int]) -> bool:
    return any(count > 0 for count in counts.values())


async def soft_delete_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    *,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, _table, sql in (*_OWNER_NAMESPACE_SOFT_DELETE_SQL, *_SOFT_DELETE_SQL):
        result = await conn.execute(sql, target_user_id, target_namespace)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts


async def restore_soft_deleted_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    soft_deleted_at: Any,
    *,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, _table, sql in _RESTORE_OWNER_NAMESPACE_SQL:
        result = await conn.execute(sql, target_user_id, target_namespace, soft_deleted_at)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts


async def hard_delete_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    *,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")
    for label, _table, sql in _HARD_DELETE_SQL:
        result = await conn.execute(sql, target_user_id, target_namespace)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts


async def process_one_deletion_request(pool: Any) -> DeletionRequestResult | None:
    """Process one confirmed request under ``FOR UPDATE SKIP LOCKED``.

    The lock, all target-table updates, and the request state
    transition share one transaction. A mid-flight exception aborts
    everything, leaving the request in ``confirmed`` for retry.
    """
    result: DeletionRequestResult | None = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(_DEQUEUE_SQL)
            if request is None:
                return None

            counts = await soft_delete_target(
                conn,
                request["target_user_id"],
                request["target_namespace"],
                invalidate_cache=False,
            )

            verifying = await conn.fetchrow(_MARK_SWEEP_VERIFYING_SQL, request["id"])
            if verifying is None:
                raise RuntimeError(
                    f"deletion request {request['id']} disappeared before verify transition"
                )

            remaining_counts: dict[str, int] = {}
            for attempt in range(1, DEFAULT_VERIFY_ATTEMPTS + 1):
                remaining_counts = await count_live_target_rows(
                    conn,
                    request["target_user_id"],
                    request["target_namespace"],
                )
                if not _has_live_rows(remaining_counts):
                    marked = await conn.fetchrow(
                        _MARK_SOFT_DELETED_SQL,
                        request["id"],
                        RESTORE_GRACE_DAYS,
                    )
                    if marked is None:
                        raise RuntimeError(
                            f"deletion request {request['id']} disappeared before soft-delete transition"
                        )
                    result = DeletionRequestResult(
                        request_id=str(request["id"]),
                        target_user_id=request["target_user_id"],
                        target_namespace=request["target_namespace"],
                        status="soft_deleted",
                        row_counts=counts,
                        soft_deleted_at=marked["soft_deleted_at"],
                        restore_by=marked["restore_by"],
                        verification_attempts=attempt,
                        remaining_counts=remaining_counts,
                    )
                    break

                logger.warning(
                    "deletion request %s verify pass %s found live rows after sweep: %s",
                    request["id"],
                    attempt,
                    remaining_counts,
                )
                retry_counts = await soft_delete_target(
                    conn,
                    request["target_user_id"],
                    request["target_namespace"],
                    invalidate_cache=False,
                )
                for label, count in retry_counts.items():
                    counts[label] = counts.get(label, 0) + count

            if result is None:
                result = DeletionRequestResult(
                    request_id=str(request["id"]),
                    target_user_id=request["target_user_id"],
                    target_namespace=request["target_namespace"],
                    status="sweep_verifying",
                    row_counts=counts,
                    soft_deleted_at=None,
                    restore_by=None,
                    verification_attempts=DEFAULT_VERIFY_ATTEMPTS,
                    remaining_counts=remaining_counts,
                )

    await invalidate_deletion_scope_caches(result.target_user_id, result.target_namespace)
    return result


async def hard_delete_soft_deleted_request(
    conn: Any,
    request: Any,
    *,
    invalidate_cache: bool = True,
) -> DeletionRequestResult:
    counts = await hard_delete_target(
        conn,
        request["target_user_id"],
        request["target_namespace"],
        invalidate_cache=False,
    )
    marked = await conn.fetchrow(_MARK_HARD_DELETED_SQL, request["id"])
    if marked is None:
        raise RuntimeError(
            f"deletion request {request['id']} disappeared before hard-delete transition"
        )
    result = DeletionRequestResult(
        request_id=str(marked["id"]),
        target_user_id=marked["target_user_id"],
        target_namespace=marked["target_namespace"],
        status=marked["status"],
        row_counts=counts,
        soft_deleted_at=marked["soft_deleted_at"],
        restore_by=marked["restore_by"],
        hard_deleted_at=marked["hard_deleted_at"],
    )
    if invalidate_cache:
        await invalidate_deletion_scope_caches(result.target_user_id, result.target_namespace)
    return result


async def process_one_hard_deletion_request(pool: Any) -> DeletionRequestResult | None:
    """Hard-delete one expired soft-deleted request under SKIP LOCKED."""
    result: DeletionRequestResult | None = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(_DEQUEUE_HARD_DELETE_SQL)
            if request is None:
                return None
            result = await hard_delete_soft_deleted_request(
                conn,
                request,
                invalidate_cache=False,
            )

    await invalidate_deletion_scope_caches(result.target_user_id, result.target_namespace)
    return result


async def process_deletion_requests(pool: Any, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    aggregate: Counter[str] = Counter()
    processed = 0
    for _ in range(batch_size):
        result = await process_one_deletion_request(pool)
        if result is None:
            break
        processed += 1
        aggregate.update(result.row_counts)
        if result.status == "soft_deleted":
            logger.info(
                "soft-deleted deletion_request=%s target_user_id=%s target_namespace=%s rows=%s "
                "restore_by=%s verify_attempts=%s",
                result.request_id,
                result.target_user_id,
                result.target_namespace,
                result.row_counts,
                result.restore_by,
                result.verification_attempts,
            )
        else:
            logger.error(
                "deletion_request=%s left in sweep_verifying after %s verify attempts; "
                "remaining live rows=%s",
                result.request_id,
                result.verification_attempts,
                result.remaining_counts,
            )
    if processed:
        aggregate["requests"] = processed
    return dict(aggregate)


async def process_hard_deletion_requests(
    pool: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    aggregate: Counter[str] = Counter()
    processed = 0
    for _ in range(batch_size):
        result = await process_one_hard_deletion_request(pool)
        if result is None:
            break
        processed += 1
        aggregate.update(result.row_counts)
        logger.info(
            "hard-deleted deletion_request=%s target_user_id=%s target_namespace=%s rows=%s "
            "hard_deleted_at=%s",
            result.request_id,
            result.target_user_id,
            result.target_namespace,
            result.row_counts,
            result.hard_deleted_at,
        )
    if processed:
        aggregate["requests"] = processed
    return dict(aggregate)


async def deletion_request_worker_loop(
    pool: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    phase: str = "soft_delete",
) -> None:
    """Perpetual lifecycle worker loop."""
    if phase not in {"soft_delete", "hard_delete"}:
        raise ValueError("phase must be 'soft_delete' or 'hard_delete'")
    process_batch = (
        process_hard_deletion_requests
        if phase == "hard_delete"
        else process_deletion_requests
    )
    while True:
        try:
            counts = await process_batch(pool, batch_size=batch_size)
            if counts:
                logger.info("deletion request worker phase=%s batch: %s", phase, counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deletion request worker phase=%s batch failed", phase)
        await asyncio.sleep(check_interval_seconds)


async def main(*, phase: str = "soft_delete") -> None:
    import asyncpg

    from mnemos.core.config import PG_CONFIG as _PG_CONFIG
    from mnemos.core.pool import wrap_pool_with_timeout

    raw_pool = await asyncpg.create_pool(
        min_size=1,
        max_size=3,
        command_timeout=60,
        user=_PG_CONFIG["user"],
        password=_PG_CONFIG["password"],
        database=_PG_CONFIG["database"],
        host=_PG_CONFIG["host"],
        port=_PG_CONFIG["port"],
    )
    pool = wrap_pool_with_timeout(raw_pool)
    try:
        await deletion_request_worker_loop(pool, phase=phase)
    finally:
        await pool.close()


def _parse_cli_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Run the GDPR deletion-request worker.")
    parser.add_argument(
        "--phase",
        choices=("soft_delete", "hard_delete"),
        default="soft_delete",
        help="Deletion-request worker phase to run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    asyncio.run(main(phase=args.phase))

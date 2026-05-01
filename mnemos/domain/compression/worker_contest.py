"""Distillation-worker helper: drains memory_compression_queue via the
v3.1 contest path.

Separate module so the queue-draining code can be unit-tested in
isolation (mock pool + mock engines) and live-tested against a
throwaway Postgres without booting the full MemoryDistillationWorker.
The existing `distillation_worker.py` keeps its direct-memory-polling
    loop; it calls `process_contest_queue()` once per loop iteration.

Queue lifecycle per row:

    pending -> running          (atomic dequeue with SKIP LOCKED)
    running -> done             (contest had a winner, persist_contest
                                  wrote winner + losers)
    running -> failed           (no winner, OR persist raised, OR too
                                  many attempts reached)
    running -> pending|failed   (stale-running sweep, see below)

Failure-recording rules:

  * attempts counter is incremented every time a row transitions from
    pending to running, regardless of outcome.
  * If a row's attempts exceeds its max on entry, we fast-fail it:
    mark failed immediately without re-running the contest. (Stops
    a persistently-broken memory from spinning forever.)
  * If the contest produces candidates but no winner (every engine
    disqualified), the queue row is marked 'failed' with a synthetic
    error summarizing the reject_reasons. The candidates are still
    persisted — operators can inspect the audit log to see why every
    engine failed.
  * If persist_contest raises, the queue row is marked 'failed' with
    the exception text. No partial state remains in the contest
    tables (persist_contest is in one transaction).

Stale-running sweep (v3.1.1):

  Belt-and-suspenders for the rare case where a worker dequeues a row
  (pending -> running), then crashes / is SIGKILLed / loses its DB
  connection before EITHER the contest-transaction commit OR the
  fresh-connection fallback mark-failed can record a terminal status.
  The row sits in 'running' forever because the dequeue only matches
  'pending'. The sweep runs at the top of every process_contest_queue
  batch: rows in 'running' older than `stale_threshold_secs` (default
  600s) are reclaimed — reset to 'pending' if attempts < max_attempts
  (retry on next dequeue), or marked 'failed' with error
  'stranded_running: ...' if already at retry ceiling.

  Race safety: the sweep is only a threshold guess — a real contest
  that exceeds `stale_threshold_secs` could still be in flight when
  the sweep reclaims its row. Two defenses against duplicate processing
  and last-writer-wins overwrites:

    1. Sweep uses FOR UPDATE SKIP LOCKED, so it never touches a row
       that the worker currently holds a row lock on (i.e. inside its
       persist transaction). Workers that are mid-contest but NOT yet
       in the persist transaction are unlocked and CAN be reclaimed.

    2. `_process_one` opens its persist transaction with a
       `SELECT ... FOR UPDATE` on the queue row and checks both
       `status == 'running'` AND `attempts == <dequeue-time value>`.
       If the sweep reclaimed the row (reset to 'pending' or marked
       'failed') OR another worker re-dequeued after the reset (attempts
       has been bumped), the precondition fails and _process_one bails
       out — no persist_contest, no mark update, no duplicate audit
       rows, no overwrite of the sweep/new-worker outcome.

  Consequence: a legitimate slow contest whose wall time exceeds the
  threshold will simply be abandoned (its work thrown away) and
  retried. That is acceptable by design — the threshold should be
  tuned above expected p99 contest latency.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, Optional, Sequence

from .base import CompressionEngine, CompressionRequest, IdentifierPolicy
from .contest import run_contest
from .contest_store import persist_contest

logger = logging.getLogger(__name__)


_MAX_ATTEMPTS = 3

# Sweep reclaims 'running' rows older than this, in case a worker crashed
# before the fresh-connection mark-failed fallback in _process_one. Real
# contest runs finish in seconds; 10 min is generous enough to cover pod
# restart + DB reconnect without fighting an in-flight row.
_DEFAULT_STALE_THRESHOLD_SECS = 600


_DEQUEUE_SQL = """
WITH next AS (
    SELECT id
    FROM memory_compression_queue
    WHERE status = 'pending'
    ORDER BY priority DESC, enqueued_at
    FOR UPDATE SKIP LOCKED
    LIMIT $1
)
UPDATE memory_compression_queue q
SET status      = 'running',
    started_at  = NOW(),
    attempts    = q.attempts + 1
FROM next
WHERE q.id = next.id
RETURNING q.id, q.memory_id, q.owner_id, q.reason,
          q.scoring_profile, q.attempts
"""

_MEMORY_CONTENT_SQL = """
SELECT id, content, category, task_type
FROM memories
WHERE id = $1
"""

_MARK_DONE_SQL = """
UPDATE memory_compression_queue
SET status      = 'done',
    finished_at = NOW(),
    error       = NULL
WHERE id = $1
"""

_MARK_FAILED_SQL = """
UPDATE memory_compression_queue
SET status      = 'failed',
    finished_at = NOW(),
    error       = $2
WHERE id = $1
"""

# $1 = stale_threshold_secs, $2 = max_attempts.
# Reclaims rows stuck in 'running' past the threshold. Attempts-below-max
# go back to 'pending' (dequeue will pick them up next); attempts-at-or-
# above-max get terminal 'failed'. SKIP LOCKED avoids touching a row
# that a worker currently holds a row lock on (i.e. inside its persist
# transaction — see `_PRECONDITION_SQL` in _process_one).
#
# `started_at IS NULL` rows with status='running' aren't produced by the
# current happy path (dequeue sets both atomically) but treat them as
# stale by default so corrupt/manual rows don't become permanent debt.
_SWEEP_STALE_SQL = """
WITH stale AS (
    SELECT id, attempts
    FROM memory_compression_queue
    WHERE status = 'running'
      AND (started_at IS NULL
           OR started_at < NOW() - ($1::int * INTERVAL '1 second'))
    FOR UPDATE SKIP LOCKED
)
UPDATE memory_compression_queue q
SET status      = CASE WHEN stale.attempts >= $2 THEN 'failed' ELSE 'pending' END,
    started_at  = CASE WHEN stale.attempts >= $2 THEN q.started_at ELSE NULL END,
    finished_at = CASE WHEN stale.attempts >= $2 THEN NOW()     ELSE NULL END,
    error       = CASE WHEN stale.attempts >= $2
                       THEN 'stranded_running: exceeded stale threshold after '
                            || stale.attempts || ' attempts'
                       ELSE NULL END
FROM stale
WHERE q.id = stale.id
RETURNING q.id, q.status, stale.attempts
"""

# Precondition check run at the start of _process_one's persist
# transaction. SELECT FOR UPDATE locks the queue row for the remainder
# of the transaction, so a concurrent sweep (which uses SKIP LOCKED)
# will skip this row until we commit. If the sweep had already
# reclaimed the row BEFORE this lock was taken, status will not be
# 'running' and/or attempts will not match the value captured at
# dequeue — in either case we abort the persist cleanly.
_PRECONDITION_SQL = """
SELECT status, attempts
FROM memory_compression_queue
WHERE id = $1
FOR UPDATE
"""


# Infrastructure-retry reset SQL.
#
# When _process_one or process_contest_queue catches a pool /
# connection-loss class error, the row is sitting at 'running' with
# attempts bumped by the dequeue. Without a reset, three things go
# wrong:
#   1. The row stays 'running' until the stale-running sweep picks
#      it up — at best the next batch's sweep, at worst stale_
#      threshold_secs later.
#   2. Each retake bumps attempts; after max_attempts cycles of
#      pure infrastructure outages the sweep terminalizes the row
#      with 'stranded_running' even though no content failure ever
#      occurred. Codex round-4 of round-30 caught this:
#      pool-pressure-as-terminal-failure, just delayed.
#   3. Operators have no breadcrumb that THIS row was infra-
#      retried (the queue's error column is empty).
#
# The reset SQL below addresses all three: status back to
# 'pending' so the next dequeue picks it up immediately,
# attempts decremented (GREATEST 0) so the dequeue's bump nets
# to zero on infra retries, started_at cleared so the sweep
# ignores it, error set to an 'infra_retry' breadcrumb for
# operator visibility.
_INFRA_RESET_SQL = """
UPDATE memory_compression_queue
SET status      = 'pending',
    attempts    = GREATEST(attempts - 1, 0),
    started_at  = NULL,
    finished_at = NULL,
    error       = $2
WHERE id = $1
  AND status = 'running'
"""


# Batch variant of the reset SQL — used when an infra error fires
# mid-batch and the un-processed tail rows must also be reset back
# to 'pending'. Without this, the tail rows sit at 'running' until
# the stale sweep catches them; under sustained pool pressure their
# attempts climb on each retake until the sweep terminalizes them
# (codex round-5 of round-28 caught this remaining surface).
_INFRA_RESET_BATCH_SQL = """
UPDATE memory_compression_queue
SET status      = 'pending',
    attempts    = GREATEST(attempts - 1, 0),
    started_at  = NULL,
    finished_at = NULL,
    error       = $2
WHERE id = ANY($1::uuid[])
  AND status = 'running'
"""


async def _reset_rows_for_infra_retry(
    pool: Any, queue_ids: Sequence[Any], error_marker: str,
) -> None:
    """Reset multiple 'running' queue rows to 'pending' in one
    UPDATE so a mid-batch infrastructure error does NOT consume
    the content-attempts budget for ANY row in the dequeue.

    Best-effort: if the reset itself raises an infra error (truly
    wedged pool), swallow + log — the stale-running sweep will
    still pick the rows up eventually. Other reset failures
    (content-class) are logged but otherwise ignored; the worker's
    primary infra-error re-raise still surfaces to the reconnect
    path.

    Empty queue_ids is a no-op (nothing to reset).
    """
    from mnemos.core.pool import is_infrastructure_error

    if not queue_ids:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INFRA_RESET_BATCH_SQL, list(queue_ids), error_marker,
            )
    except Exception as reset_exc:
        if is_infrastructure_error(reset_exc):
            logger.warning(
                "contest_queue: infra-reset acquire timed out (%s) for "
                "%d row(s); rows left at 'running' for stale-recovery",
                type(reset_exc).__name__, len(queue_ids),
            )
        else:
            logger.exception(
                "contest_queue: infra-reset failed unexpectedly for %d row(s)",
                len(queue_ids),
            )


async def _reset_row_for_infra_retry(
    pool: Any, queue_id: Any, error_marker: str,
) -> None:
    """Single-row form of ``_reset_rows_for_infra_retry``. Kept for
    backward compatibility with the round-32 single-site call shape;
    new sites should prefer the batch helper to amortise the
    acquire when more than one row needs resetting."""
    await _reset_rows_for_infra_retry(pool, [queue_id], error_marker)


async def _sweep_stale_running(
    pool: Any,
    *,
    stale_threshold_secs: int = _DEFAULT_STALE_THRESHOLD_SECS,
    max_attempts: int = _MAX_ATTEMPTS,
) -> Dict[str, int]:
    """Reclaim queue rows stranded in 'running' past the threshold.

    Internal — callers outside this module should use
    `process_contest_queue()`, which runs the sweep at the top of each
    batch. Exposed for tests.

    Handles the case where a worker crashed after dequeue but before
    any terminal status was recorded (both the contest-transaction
    commit AND the fresh-connection fallback mark-failed failed —
    e.g. pool exhausted, SIGKILL, host reboot). Without the sweep, the
    row sits 'running' forever because dequeue only selects 'pending'.

    Rows below the retry ceiling go back to 'pending' for another try
    (started_at cleared so the attempts counter bumps again on the
    next dequeue). Rows at-or-above the ceiling go terminal 'failed'
    with a 'stranded_running' error marker so operators can grep for
    them.

    Returns {'stranded_reset': N, 'stranded_failed': M}, or an empty
    dict if nothing was stale.
    """
    counts: Counter[str] = Counter()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SWEEP_STALE_SQL, int(stale_threshold_secs), int(max_attempts)
        )

    for row in rows:
        if row["status"] == "pending":
            counts["stranded_reset"] += 1
            logger.warning(
                "contest_queue sweep: row %s reset to pending "
                "(attempts=%d, exceeded %ds stale threshold)",
                row["id"], row["attempts"], stale_threshold_secs,
            )
        else:  # 'failed'
            counts["stranded_failed"] += 1
            logger.warning(
                "contest_queue sweep: row %s marked failed "
                "(attempts=%d >= max, exceeded %ds stale threshold)",
                row["id"], row["attempts"], stale_threshold_secs,
            )

    return dict(counts)


async def process_contest_queue(
    pool: Any,
    engines: Sequence[CompressionEngine],
    *,
    batch_size: int = 5,
    max_attempts: int = _MAX_ATTEMPTS,
    min_content_length: int = 0,
    stale_threshold_secs: int = _DEFAULT_STALE_THRESHOLD_SECS,
    judge_model: Optional[str] = None,
    judge: Optional[Any] = None,
) -> Dict[str, int]:
    """Drain up to `batch_size` pending queue rows via the contest path.

    `pool` is an asyncpg Pool (or anything with acquire() that yields
    a connection supporting fetch/fetchrow/execute/transaction — the
    unit tests stub this with AsyncMock). Each queue row runs its
    contest in a separate connection so one row's DB activity doesn't
    stall or transact with another's. `max_attempts` caps retries per
    row (default 3, matching the distillation worker's MAX_ATTEMPTS).

    `min_content_length` (default 0 = no gate) skips memories below
    the threshold BEFORE running the contest. Surfaced by the 2026-04-23
    CERBERUS benchmark: short templated content (git commit headers,
    GRAEAE consultation stubs) can't be meaningfully compressed by any
    engine at the balanced profile's floor. Setting this to e.g. 500
    tells the worker
    to mark those rows `failed` immediately with
    `error='too_short: N chars < threshold M'` and move on. Recommended
    for GPU-constrained installs; leave at 0 for full-contest behavior
    matching v3.1 GA default.

    `stale_threshold_secs` drives the stale-running sweep that runs at
    the top of every batch (see sweep_stale_running). Default 600s is
    safe for typical deployments where contests finish in seconds; set
    to 0 to disable the sweep entirely (tests, or operators who prefer
    an external reclaim tool).

    Returns a dict {'dequeued', 'succeeded', 'failed', 'skipped_max_attempts',
    'missing_memory', 'skipped_too_short', 'stranded_reset',
    'stranded_failed', 'race_abandoned'} for the caller to log and for
    metrics. 'race_abandoned' counts rows whose dequeue-time fingerprint
    no longer matched when the persist transaction started (sweep
    reclaimed, or another worker re-dequeued after sweep reset).
    """

    counts: Counter[str] = Counter()

    if stale_threshold_secs > 0:
        try:
            sweep_counts = await _sweep_stale_running(
                pool,
                stale_threshold_secs=stale_threshold_secs,
                max_attempts=max_attempts,
            )
            counts.update(sweep_counts)
        except Exception:
            # Sweep failure must not block the rest of the batch —
            # dequeue can still make forward progress on pending rows
            # even if reclaim is temporarily broken.
            logger.exception("contest_queue sweep failed; continuing to dequeue")

    async with pool.acquire() as conn:
        rows = await conn.fetch(_DEQUEUE_SQL, batch_size)

    if not rows:
        return dict(counts)

    counts["dequeued"] = len(rows)

    for row_idx, row in enumerate(rows):
        queue_id = row["id"]
        memory_id = row["memory_id"]
        owner_id = row["owner_id"]
        attempts = row["attempts"]
        scoring_profile = row["scoring_profile"]

        if attempts > max_attempts:
            async with pool.acquire() as conn:
                await conn.execute(
                    _MARK_FAILED_SQL,
                    queue_id,
                    f"max_attempts exceeded ({attempts} > {max_attempts})",
                )
            counts["skipped_max_attempts"] += 1
            counts["failed"] += 1
            logger.warning(
                "contest_queue[%s]: skipped, attempts=%d > max=%d",
                memory_id, attempts, max_attempts,
            )
            continue

        try:
            await _process_one(
                pool,
                queue_id=queue_id,
                memory_id=memory_id,
                owner_id=owner_id,
                scoring_profile=scoring_profile,
                engines=engines,
                counts=counts,
                judge_model=judge_model,
                judge=judge,
                min_content_length=min_content_length,
                expected_attempts=attempts,
            )
        except Exception as exc:
            # Distinguish infrastructure errors (pool exhaustion,
            # asyncpg disconnect, asyncio.TimeoutError from the
            # round-28 TimeoutPool wrap) from content / contest
            # failures. Marking a row failed on a transient pool
            # timeout would convert pool pressure into an
            # irreversible failed compression row — the worker's
            # retry/stale-recovery flow exists to handle the
            # transient case. Re-raise so the worker loop's
            # reconnect path sees the error.
            from mnemos.core.pool import is_infrastructure_error

            if is_infrastructure_error(exc):
                # Reset every unprocessed row in the dequeue batch
                # — current row + any tail. Without resetting the
                # tail, those rows sit at 'running' (already
                # attempts-bumped by the dequeue) until the
                # stale-running sweep catches them; codex round-5
                # of round-28 caught that under repeated pool
                # pressure the tail rows' attempts climb on each
                # retake until the sweep terminalizes them as
                # 'stranded_running' even though no content
                # failure occurred. Single batch UPDATE so we
                # amortise the (potentially-also-failing) acquire.
                tail_ids = [r["id"] for r in rows[row_idx:]]
                logger.warning(
                    "contest_queue[%s]: infrastructure error %s; resetting "
                    "%d unprocessed row(s) (current + %d tail) for retry "
                    "without consuming attempts budget, then re-raising",
                    memory_id, type(exc).__name__,
                    len(tail_ids), len(tail_ids) - 1,
                )
                await _reset_rows_for_infra_retry(
                    pool,
                    tail_ids,
                    f"infra_retry: {type(exc).__name__}: {str(exc)[:200]}",
                )
                counts["infra_errors"] += 1
                raise

            logger.exception(
                "contest_queue[%s]: unhandled exception", memory_id
            )
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        _MARK_FAILED_SQL,
                        queue_id,
                        f"{type(exc).__name__}: {exc}",
                    )
            except Exception as mark_exc:
                # If we can't even acquire to mark failed, that's
                # also infrastructure — bubble up rather than
                # silently leaving the row in-flight.
                if is_infrastructure_error(mark_exc):
                    logger.warning(
                        "contest_queue[%s]: failed-mark acquire timed out (%s); "
                        "re-raising original error",
                        memory_id, type(mark_exc).__name__,
                    )
                    counts["infra_errors"] += 1
                    raise mark_exc from exc
                raise
            counts["failed"] += 1

    return dict(counts)


async def _process_one(
    pool: Any,
    *,
    queue_id: Any,
    memory_id: str,
    owner_id: str,
    scoring_profile: str,
    engines: Sequence[CompressionEngine],
    counts: Counter[str],
    judge_model: Optional[str],
    judge: Optional[Any] = None,
    min_content_length: int = 0,
    expected_attempts: int,
) -> None:
    """Run the contest for a single dequeued queue row + persist + update.

    `expected_attempts` is the `attempts` value captured at dequeue time.
    Used inside the persist transaction as a fingerprint: if the sweep
    reclaimed this row to 'pending' and another worker re-dequeued it,
    attempts will have been bumped on the re-dequeue, so the fingerprint
    mismatch tells us this worker is stale. See the module docstring
    'Race safety' section.
    """

    async with pool.acquire() as conn:
        mem = await conn.fetchrow(_MEMORY_CONTENT_SQL, memory_id)

    if mem is None or not mem["content"]:
        async with pool.acquire() as conn:
            await conn.execute(
                _MARK_FAILED_SQL,
                queue_id,
                "memory not found or empty content",
            )
        counts["missing_memory"] += 1
        counts["failed"] += 1
        logger.warning(
            "contest_queue[%s]: memory not found or empty, marking failed",
            memory_id,
        )
        return

    content_len = len(mem["content"])
    if min_content_length > 0 and content_len < min_content_length:
        async with pool.acquire() as conn:
            await conn.execute(
                _MARK_FAILED_SQL,
                queue_id,
                f"too_short: {content_len} chars < threshold {min_content_length}",
            )
        counts["skipped_too_short"] += 1
        counts["failed"] += 1
        logger.info(
            "contest_queue[%s]: skipped, content %d chars < threshold %d",
            memory_id, content_len, min_content_length,
        )
        return

    request = CompressionRequest(
        memory_id=memory_id,
        content=mem["content"],
        owner_id=owner_id,
        task_type=mem.get("task_type") or mem["category"],
        scoring_profile=scoring_profile,
        identifier_policy=IdentifierPolicy.STRICT,
    )
    outcome = await run_contest(engines, request, judge=judge)

    # persist + queue-finalization must commit together so a failure
    # between them cannot leave a contest durable with the queue row
    # stuck in 'running'. Codex review surfaced this: previously the
    # queue UPDATE ran outside persist_contest's transaction, so any
    # mid-commit failure would strand the row (dequeue only selects
    # 'pending', the running row would never be retried). Fix: one
    # outer transaction wraps both.
    #
    # Before persisting we lock the queue row and verify (status,
    # attempts) still matches the dequeue-time fingerprint. If the
    # stale-running sweep has reclaimed this row in the meantime —
    # or another worker has re-dequeued after a sweep reset — we
    # abort cleanly without writing duplicate audit rows.
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(_PRECONDITION_SQL, queue_id)
                if (
                    cur is None
                    or cur["status"] != "running"
                    or cur["attempts"] != expected_attempts
                ):
                    logger.info(
                        "contest_queue[%s]: abandoning work — row no longer "
                        "owned (status=%s, attempts=%s, expected=%d); "
                        "sweep reclaimed or another worker re-dequeued",
                        memory_id,
                        None if cur is None else cur["status"],
                        None if cur is None else cur["attempts"],
                        expected_attempts,
                    )
                    counts["race_abandoned"] += 1
                    return

                await persist_contest(conn, outcome, judge_model=judge_model)

                if outcome.winner is None:
                    reasons = Counter(
                        c.reject_reason or "unknown"
                        for c in outcome.candidates
                    )
                    reason_summary = ", ".join(
                        f"{reason}={count}"
                        for reason, count in reasons.most_common()
                    )
                    await conn.execute(
                        _MARK_FAILED_SQL,
                        queue_id,
                        f"no winner: {reason_summary}",
                    )
                    mark_result = ("no_winner", reason_summary)
                else:
                    await conn.execute(_MARK_DONE_SQL, queue_id)
                    mark_result = ("winner",)
    except Exception as exc:
        # Atomic rollback: no partial contest rows, no queue update —
        # the row stays 'running'. Two cases to distinguish:
        #
        # 1. Infrastructure error (pool acquire timeout, asyncpg
        #    connection loss). The row's content didn't fail; the
        #    DB pool / connection did. Re-raise so the worker
        #    loop's reconnect path fires and the row stays
        #    retryable via the stale-running sweep. Marking it
        #    failed here would convert pool pressure into a
        #    terminal failed compression row.
        #
        # 2. Content / persist-validation failure. Mark the row
        #    failed in a FRESH transaction (separate connection,
        #    since the failed txn's connection is being released).
        #    The dequeue's next pass won't touch it because status
        #    is now 'failed' not 'pending'.
        from mnemos.core.pool import is_infrastructure_error

        if is_infrastructure_error(exc):
            logger.warning(
                "contest_queue[%s]: persist transaction hit infrastructure "
                "error %s; re-raising to worker loop (outer caller resets "
                "the row to 'pending' so this retry doesn't burn a "
                "content-attempts cycle)",
                memory_id, type(exc).__name__,
            )
            counts["infra_errors"] += 1
            raise

        logger.exception(
            "contest_queue[%s]: contest persistence failed, rolled back", memory_id
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    _MARK_FAILED_SQL,
                    queue_id,
                    f"persist failed: {type(exc).__name__}: {exc}",
                )
        except Exception as mark_exc:
            # The fallback mark-failed acquire also failed. If THAT
            # is infrastructure, surface it to the worker loop too —
            # silently swallowing it would leave the row stranded
            # at 'running' AND prevent reconnect.
            if is_infrastructure_error(mark_exc):
                logger.warning(
                    "contest_queue[%s]: persist failed AND fallback "
                    "mark-failed acquire timed out (%s); re-raising for "
                    "worker reconnect; original error: %s",
                    memory_id, type(mark_exc).__name__,
                    type(exc).__name__,
                )
                counts["infra_errors"] += 1
                raise mark_exc from exc
            logger.exception(
                "contest_queue[%s]: mark-failed also failed; row stranded at 'running' — "
                "recovery on next worker start via stale-running sweep (v3.1.1)",
                memory_id,
            )
        counts["failed"] += 1
        return

    # Transaction committed cleanly. Log + count outside the txn.
    if mark_result[0] == "winner":
        counts["succeeded"] += 1
        logger.info(
            "contest_queue[%s]: winner=%s score=%.4f",
            memory_id,
            outcome.winner.result.engine_id,
            outcome.winner.composite_score,
        )
    else:
        counts["failed"] += 1
        logger.info(
            "contest_queue[%s]: no winner (%s)", memory_id, mark_result[1]
        )


__all__ = ["process_contest_queue"]

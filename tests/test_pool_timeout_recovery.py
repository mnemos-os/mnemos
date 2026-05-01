"""Tests for the round-30 follow-on to the TimeoutPool wrap.

Round-28 wrapped the asyncpg pool so legacy ``acquire()`` calls
inherit the configured acquire timeout. Round-29 extended the wrap
to the distillation worker's own pool. Codex round-2 of round-28
caught the consequence: now that bare acquires can fail with
``asyncio.TimeoutError``, two surfaces convert that infrastructure
class error into something terminal:

  1. The contest queue's broad-except in ``_process_one`` marks the
     row failed via ``MARK_FAILED_SQL``. A pool timeout would
     therefore turn pool pressure into an irreversible failed
     compression row, hiding infrastructure failure as content
     failure.

  2. The distillation worker's ``process_contest_queue_batch`` and
     ``log_stats`` both swallow ``Exception``. A timeout here is
     logged but the wedged pool is reused next iteration —
     reconnect never fires.

The fix in mnemos.core.pool.is_infrastructure_error +
mnemos.domain.compression.worker_contest +
mnemos.workers.distillation is what these tests pin.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import asyncpg
import pytest

from mnemos.core.pool import (
    INFRASTRUCTURE_ERRORS,
    is_infrastructure_error,
)


# ── Predicate ───────────────────────────────────────────────────────────────


def test_is_infrastructure_error_admits_timeout():
    assert is_infrastructure_error(asyncio.TimeoutError())


def test_is_infrastructure_error_admits_asyncpg_connection_loss():
    err = asyncpg.PostgresConnectionError("simulated")
    assert is_infrastructure_error(err)


def test_is_infrastructure_error_admits_connection_reset():
    assert is_infrastructure_error(ConnectionResetError())


def test_is_infrastructure_error_rejects_value_error():
    """Content / processing failures must NOT pass the predicate —
    those are still terminal MARK_FAILED candidates."""
    assert not is_infrastructure_error(ValueError("bad input"))


def test_is_infrastructure_error_rejects_runtime_error():
    assert not is_infrastructure_error(RuntimeError("logic bug"))


def test_infrastructure_errors_tuple_contents():
    """Pin the exact set so a refactor doesn't accidentally narrow
    the predicate."""
    assert asyncio.TimeoutError in INFRASTRUCTURE_ERRORS
    assert ConnectionResetError in INFRASTRUCTURE_ERRORS
    assert asyncpg.PostgresConnectionError in INFRASTRUCTURE_ERRORS


# ── _process_one re-raises infra; marks failed for content errors ──────────


def _build_fake_pool_for_contest(*, dequeue_rows: list, on_execute):
    """Build an asyncpg-shaped pool whose acquire() returns a conn
    that responds to .fetch (dequeue) with ``dequeue_rows`` and
    routes .execute to ``on_execute(sql, *args)``.

    Each acquire returns a fresh async-context. The conn supports
    .transaction() as a no-op async-context for the inner per-row
    flow.
    """

    class _Tx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *args):
            return None

    async def _fetch(sql, *args):
        return dequeue_rows

    conn = MagicMock()
    conn.fetch = _fetch
    conn.execute = on_execute
    conn.transaction = lambda *a, **kw: _Tx()

    class _AsyncCtx:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *args):
            return None

    pool = MagicMock()
    # Track every acquire() invocation. Each call returns a fresh
    # async-context so multiple sequential acquires inside the
    # function under test still work.
    pool.acquire_calls = 0

    def _acquire():
        pool.acquire_calls += 1
        return _AsyncCtx()

    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_process_contest_queue_does_not_mark_failed_on_pool_timeout(
    monkeypatch,
):
    """Round-28's TimeoutPool wrap can raise asyncio.TimeoutError
    on the post-dequeue acquire inside _process_one. The broad
    except in process_contest_queue MUST NOT then write
    _MARK_FAILED_SQL via a fresh acquire — that converts transient
    pool pressure into terminal content failure. Re-raise instead.
    """
    from mnemos.domain.compression import worker_contest

    async def _fake_process_one(*args, **kwargs):
        raise asyncio.TimeoutError("simulated acquire timeout")

    monkeypatch.setattr(
        worker_contest, "_process_one", _fake_process_one,
    )

    fake_row = {
        "id": "queue-1",
        "memory_id": "mem_1",
        "owner_id": "alice",
        "scoring_profile": "default",
        "attempts": 0,
    }

    executes: list[tuple] = []

    async def _record_execute(sql, *args):
        executes.append((sql, args))
        return None

    pool = _build_fake_pool_for_contest(
        dequeue_rows=[fake_row], on_execute=_record_execute,
    )

    with pytest.raises(asyncio.TimeoutError):
        await worker_contest.process_contest_queue(
            pool,
            engines=[],
            batch_size=1,
            max_attempts=3,
            stale_threshold_secs=0,  # skip sweep
        )

    # Two acquires: dequeue (1) + infra-reset (2).
    # The infra-reset sets status='pending' so the row stays
    # retryable AND decrements attempts so this infra cycle
    # doesn't consume the content-attempts budget — codex round-4
    # of round-30 caught that without the reset, repeated infra
    # errors would still terminalize the row via the stale sweep.
    assert pool.acquire_calls == 2
    # The reset SQL must NOT mark failed — pin the breadcrumb
    # shape: status='pending', error LIKE 'infra_retry%'.
    reset_sqls = [(sql, args) for sql, args in executes]
    assert any(
        "status" in sql and "pending" in sql.lower()
        for sql, _ in reset_sqls
    ), (
        f"expected infra-reset to set status='pending'; got {reset_sqls!r}"
    )
    assert any(
        any("infra_retry" in str(arg) for arg in args)
        for _, args in reset_sqls
    ), (
        f"expected infra-reset to write 'infra_retry' breadcrumb; got "
        f"{reset_sqls!r}"
    )
    # And no MARK_FAILED execute (the persist-error SQL with
    # status='failed') should be issued on a pure infra path.
    assert not any(
        "status" in sql and "= 'failed'" in sql for sql, _ in reset_sqls
    ), f"unexpected MARK_FAILED SQL on infra error: {reset_sqls!r}"


@pytest.mark.asyncio
async def test_process_contest_queue_marks_failed_on_content_error(
    monkeypatch,
):
    """The OTHER side of the contract: a deterministic content-level
    failure (ValueError / KeyError / etc.) MUST still mark the row
    failed via MARK_FAILED_SQL — round-30's infra-only re-raise
    must not bypass terminal content errors."""
    from mnemos.domain.compression import worker_contest

    async def _fake_process_one(*args, **kwargs):
        raise ValueError("malformed compressed_content payload")

    monkeypatch.setattr(
        worker_contest, "_process_one", _fake_process_one,
    )

    fake_row = {
        "id": "queue-1",
        "memory_id": "mem_1",
        "owner_id": "alice",
        "scoring_profile": "default",
        "attempts": 0,
    }

    executes: list[tuple] = []

    async def _record_execute(sql, *args):
        executes.append((sql, args))
        return None

    pool = _build_fake_pool_for_contest(
        dequeue_rows=[fake_row], on_execute=_record_execute,
    )

    counts = await worker_contest.process_contest_queue(
        pool,
        engines=[],
        batch_size=1,
        max_attempts=3,
        stale_threshold_secs=0,  # skip sweep
    )

    assert counts.get("failed", 0) == 1
    # Two acquires: dequeue + MARK_FAILED.
    assert pool.acquire_calls == 2
    # MARK_FAILED execute landed; the SQL touches
    # memory_compression_queue with a status update.
    assert any(
        "memory_compression_queue" in sql for sql, _ in executes
    ), f"expected MARK_FAILED_SQL execute; got {executes!r}"


# ── Worker loop: infra errors reach the reconnect path ────────────────────


@pytest.mark.asyncio
async def test_worker_process_contest_batch_reraises_infrastructure_error(
    monkeypatch,
):
    """The distillation worker's process_contest_queue_batch
    swallows ordinary exceptions but MUST re-raise infrastructure
    errors so the main loop's reconnect path replaces the wedged
    pool. Without this, a timeout from the round-28 wrap is logged
    and the worker keeps reusing the broken pool indefinitely."""
    from mnemos.workers.distillation import MemoryDistillationWorker

    worker = MemoryDistillationWorker()
    # Make _contest_engines truthy so the early return doesn't
    # short-circuit the test path.
    worker._contest_engines = [object()]
    worker.db_pool = MagicMock()
    worker._judge = None

    async def _raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError("acquire timed out")

    monkeypatch.setattr(
        "mnemos.workers.distillation.process_contest_queue",
        _raise_timeout,
    )

    with pytest.raises(asyncio.TimeoutError):
        await worker.process_contest_queue_batch()


@pytest.mark.asyncio
async def test_worker_process_contest_batch_swallows_content_error(
    monkeypatch,
):
    """The complement: a non-infra error continues to be swallowed
    so a single bad row doesn't take the worker offline."""
    from mnemos.workers.distillation import MemoryDistillationWorker

    worker = MemoryDistillationWorker()
    worker._contest_engines = [object()]
    worker.db_pool = MagicMock()
    worker._judge = None

    async def _raise_content(*args, **kwargs):
        raise ValueError("bad content shape")

    monkeypatch.setattr(
        "mnemos.workers.distillation.process_contest_queue",
        _raise_content,
    )

    # Should NOT raise.
    await worker.process_contest_queue_batch()


@pytest.mark.asyncio
async def test_worker_log_stats_reraises_infrastructure_error(monkeypatch):
    """log_stats's broad except must also propagate infra errors
    so the reconnect path can fire. Stats failures are otherwise
    debug-logged."""
    from mnemos.workers.distillation import MemoryDistillationWorker

    worker = MemoryDistillationWorker()

    class _FakeAcquire:
        async def __aenter__(self_):
            raise asyncio.TimeoutError("wedged pool")

        async def __aexit__(self_, *args):
            return None

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_FakeAcquire())
    worker.db_pool = fake_pool

    with pytest.raises(asyncio.TimeoutError):
        await worker.log_stats()


@pytest.mark.asyncio
async def test_repeated_infra_errors_do_not_consume_attempts_budget(monkeypatch):
    """Codex round-4 of round-30 caught: even with the round-30
    fix, repeated infra errors would let attempts climb on each
    dequeue cycle until the stale sweep terminalized the row.
    Round-32 fixes this by RESETTING the row before re-raising —
    status back to 'pending', attempts decremented (GREATEST 0),
    so the dequeue's bump nets to zero on infra cycles. This test
    pins that contract: the reset SQL is issued and decrements
    attempts."""
    from mnemos.domain.compression import worker_contest

    async def _fake_process_one(*args, **kwargs):
        raise asyncio.TimeoutError("simulated acquire timeout")

    monkeypatch.setattr(
        worker_contest, "_process_one", _fake_process_one,
    )

    fake_row = {
        "id": "queue-1",
        "memory_id": "mem_1",
        "owner_id": "alice",
        "scoring_profile": "default",
        "attempts": 1,  # already had one cycle
    }

    executes: list[tuple] = []

    async def _record_execute(sql, *args):
        executes.append((sql, args))
        return None

    pool = _build_fake_pool_for_contest(
        dequeue_rows=[fake_row], on_execute=_record_execute,
    )

    with pytest.raises(asyncio.TimeoutError):
        await worker_contest.process_contest_queue(
            pool,
            engines=[],
            batch_size=1,
            max_attempts=3,
            stale_threshold_secs=0,
        )

    # The reset SQL is the only UPDATE on memory_compression_queue
    # in this path. It must:
    #   1. Set status='pending'  (so dequeue picks it up next)
    #   2. Decrement attempts via GREATEST(attempts - 1, 0)
    #   3. Use 'infra_retry:' as the error prefix
    reset_calls = [
        (sql, args) for sql, args in executes
        if "memory_compression_queue" in sql and "pending" in sql.lower()
    ]
    assert len(reset_calls) == 1, f"expected one reset call; got {reset_calls!r}"
    reset_sql, reset_args = reset_calls[0]
    assert "GREATEST(attempts - 1, 0)" in reset_sql
    assert any("infra_retry" in str(a) for a in reset_args)


@pytest.mark.asyncio
async def test_process_one_does_not_mark_failed_on_infra_error_in_persist(
    monkeypatch,
):
    """Codex round-3 of round-29 caught that the round-30 fix only
    handled infra errors that ESCAPED _process_one. _process_one
    has its own broad except around the persist transaction +
    fallback mark-failed acquire that ran MARK_FAILED on any
    Exception. An asyncio.TimeoutError from acquire() / persist /
    done-update would still be converted to a terminal failed row.

    Pin: when persist_contest raises TimeoutError, the row is NOT
    marked failed and the error re-raises to the worker loop.
    """
    from mnemos.domain.compression import worker_contest

    # Stub the persist call to raise a fresh TimeoutError —
    # simulates the post-dequeue acquire / persist transaction
    # hitting a pool timeout.
    async def _fake_persist(*args, **kwargs):
        raise asyncio.TimeoutError("simulated persist-tx timeout")

    monkeypatch.setattr(
        worker_contest, "persist_contest", _fake_persist,
    )

    # Stub run_contest to produce a benign outcome so persist
    # gets called with a normal payload.
    async def _fake_run_contest(*args, **kwargs):
        # The actual outcome shape is irrelevant — persist_contest
        # raises before reading it.
        return MagicMock(winner=MagicMock(), candidates=[])

    monkeypatch.setattr(
        worker_contest, "run_contest", _fake_run_contest,
    )

    # Build a pool whose acquire() yields a conn that returns a
    # non-empty memory row, satisfies the precondition fingerprint,
    # but persist_contest raises before the queue update lands.
    fake_memory = {
        "content": "lorem ipsum dolor sit amet",
        "category": "facts",
        "task_type": "facts",
    }
    fake_precondition = {
        "status": "running",
        "attempts": 1,
    }

    executes: list[tuple] = []

    async def _record_execute(sql, *args):
        executes.append((sql, args))
        return None

    async def _fetchrow(sql, *args):
        # _MEMORY_CONTENT_SQL → return memory row
        # _PRECONDITION_SQL → return matching fingerprint
        # the test only needs the dispatch by SQL prefix to be sane.
        if "FROM memories" in sql:
            return fake_memory
        return fake_precondition

    class _Tx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *args):
            return None

    conn = MagicMock()
    conn.fetch = MagicMock()
    conn.fetchrow = _fetchrow
    conn.execute = _record_execute
    conn.transaction = lambda *a, **kw: _Tx()

    class _AsyncCtx:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *args):
            return None

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx())

    counts: dict = {}
    from collections import Counter as _Counter
    counts = _Counter()

    with pytest.raises(asyncio.TimeoutError):
        await worker_contest._process_one(
            pool,
            queue_id="queue-1",
            memory_id="mem_1",
            owner_id="alice",
            scoring_profile="default",
            engines=[object()],
            counts=counts,
            judge_model=None,
            judge=None,
            min_content_length=0,
            expected_attempts=1,
        )

    # No MARK_FAILED was written.
    assert not any(
        "memory_compression_queue" in sql and "failed" in sql.lower()
        for sql, _ in executes
    ), f"unexpected MARK_FAILED on infra error inside _process_one: {executes!r}"
    # Counts reflect the infrastructure-error bucket, not 'failed'.
    assert counts.get("infra_errors", 0) == 1
    assert counts.get("failed", 0) == 0


@pytest.mark.asyncio
async def test_worker_log_stats_swallows_content_error(monkeypatch):
    """Stats failures from a malformed row, missing column, etc.
    stay debug-logged."""
    from mnemos.workers.distillation import MemoryDistillationWorker

    worker = MemoryDistillationWorker()

    class _FakeAcquire:
        async def __aenter__(self_):
            raise ValueError("bad row shape")

        async def __aexit__(self_, *args):
            return None

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_FakeAcquire())
    worker.db_pool = fake_pool

    # Should NOT raise.
    await worker.log_stats()

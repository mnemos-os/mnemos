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

    # The dequeue acquire ran (1). The fixed code's re-raise
    # prevented any subsequent MARK_FAILED acquire. Without the
    # fix, a second acquire would have happened to write the
    # MARK_FAILED row.
    assert pool.acquire_calls == 1, (
        f"expected exactly one acquire (dequeue only); got "
        f"{pool.acquire_calls} — broad-except may be marking failed"
    )
    # And no MARK_FAILED execute should have been issued.
    assert all(
        "memory_compression_queue" not in sql or "UPDATE" not in sql.upper()
        for sql, _ in executes
    ), f"unexpected MARK_FAILED SQL on infra error: {executes!r}"


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

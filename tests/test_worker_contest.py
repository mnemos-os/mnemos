"""process_contest_queue — distillation-worker queue drain tests.

Mock-based checks for compression/worker_contest.py: queue dequeue
ordering, status transitions, max-attempts fast-fail, missing-memory
handling, no-winner failure shape, persist-raises handling, and the
happy path where all dequeued rows succeed.

Mocks an asyncpg-shaped Pool: `pool.acquire()` returns an async ctx
yielding a Connection; Connection has async fetch/fetchrow/execute
and a `.transaction()` async ctx (used by persist_contest).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from mnemos.domain.compression.base import (
    CompressionEngine,
    CompressionResult,
    GPUIntent,
)
from mnemos.domain.compression.worker_contest import process_contest_queue

# ---- test doubles ----------------------------------------------------------


class _StubEngine(CompressionEngine):
    """Minimal engine returning a pre-baked result."""

    gpu_intent = GPUIntent.CPU_ONLY

    def __init__(self, id_: str, *, result_factory) -> None:
        self.id = id_
        self.label = id_
        self.version = "1"
        self._factory = result_factory
        super().__init__()

    async def compress(self, request):
        return self._factory(request)


def _good_result(engine_id: str, *, quality: float = 0.9, ratio: float = 0.4):
    def factory(request):
        return CompressionResult(
            engine_id=engine_id,
            engine_version="1",
            original_tokens=100,
            compressed_tokens=int(100 * ratio),
            compressed_content="x" * int(100 * ratio),
            compression_ratio=ratio,
            quality_score=quality,
            elapsed_ms=50,
        )
    return factory


def _bad_result(engine_id: str):
    def factory(request):
        return CompressionResult(
            engine_id=engine_id,
            engine_version="1",
            original_tokens=100,
            elapsed_ms=10,
            error="boom",
        )
    return factory


def _queue_row(*, attempts: int = 0, scoring: str = "balanced") -> dict:
    return {
        "id": uuid.uuid4(),
        "memory_id": f"mem-{uuid.uuid4().hex[:8]}",
        "owner_id": "alice",
        "reason": "on_write",
        "scoring_profile": scoring,
        "attempts": attempts,
    }


def _memory_row(memory_id: str, *, content: str = "hello world " * 50) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "category": "solutions",
        "task_type": None,
    }


def _mock_pool(
    *,
    queue_rows: list[dict],
    memory_content_by_id: dict[str, dict],
    sweep_rows: list[dict] | None = None,
) -> MagicMock:
    """Build a mock Pool whose acquire() yields a mock Connection.

    The connection dispatches on SQL text: dequeue returns queue_rows
    then [] on subsequent calls; sweep returns sweep_rows (default []);
    fetchrow returns memory rows; execute records MARK_DONE / MARK_FAILED
    calls; transaction() is a no-op async ctx (persist_contest uses it).
    """

    dequeue_calls = [queue_rows, []]  # first call returns rows, second returns []
    sweep_calls = [list(sweep_rows) if sweep_rows else []]
    execute_log: list[tuple[str, tuple]] = []
    fetchrow_calls: list[tuple[str, tuple]] = []
    compression_versions: list[dict] = []
    source_heads: dict[str, dict] = {}
    persist_fetchrow_calls = 0

    def _source_head(memory_id: str) -> dict:
        if memory_id not in source_heads:
            source_heads[memory_id] = {
                "parent_version_id": uuid.uuid4(),
                "parent_commit_hash": "d" * 64,
            }
        return source_heads[memory_id]

    async def fetch(sql, *args):
        # sweep_stale_running: SELECT ... WHERE status = 'running'
        if "status = 'running'" in sql:
            return sweep_calls.pop(0) if sweep_calls else []
        # process_contest_queue dequeue: SELECT ... WHERE status = 'pending'
        if "status = 'pending'" in sql and "FOR UPDATE SKIP LOCKED" in sql:
            return dequeue_calls.pop(0) if dequeue_calls else []
        raise AssertionError(f"unexpected fetch: {sql!r}")

    async def fetchrow(sql, *args):
        nonlocal persist_fetchrow_calls
        if sql.strip().startswith("SELECT id, content, category, task_type"):
            fetchrow_calls.append((sql, args))
            return memory_content_by_id.get(args[0])
        # _process_one precondition: SELECT status, attempts ... FOR UPDATE.
        # Default: row still 'running' with its dequeue-time attempts
        # value (i.e. the queue_row's attempts, which represents the
        # POST-bump value — mirroring the real dequeue's RETURNING).
        # Tests that simulate a sweep reclaim override this branch via
        # pool._conn.fetchrow = AsyncMock(...).
        if sql.strip().startswith("SELECT status, attempts"):
            match = next((r for r in queue_rows if r["id"] == args[0]), None)
            if match is None:
                return None
            return {"status": "running", "attempts": match["attempts"]}
        # persist_contest's INSERT ... RETURNING id
        if "INSERT INTO memory_compression_candidates" in sql:
            persist_fetchrow_calls += 1
            return {"id": uuid.uuid4()}
        # persist_contest's compression-DAG parent lookup.
        if "FROM memory_branches mb" in sql and "mb.name = 'main'" in sql:
            memory_id = args[0]
            memory = memory_content_by_id.get(memory_id)
            if memory is None:
                return None
            head = _source_head(memory_id)
            return {
                "memory_id": memory_id,
                "category": memory.get("category", "general"),
                "subcategory": memory.get("subcategory"),
                "metadata": memory.get("metadata", {}),
                "verbatim_content": memory.get("verbatim_content"),
                "owner_id": memory.get("owner_id", "alice"),
                "namespace": memory.get("namespace", "default"),
                "permission_mode": memory.get("permission_mode", 600),
                "source_model": memory.get("source_model"),
                "source_provider": memory.get("source_provider"),
                "source_session": memory.get("source_session"),
                "source_agent": memory.get("source_agent"),
                "parent_version_id": head["parent_version_id"],
                "parent_commit_hash": head["parent_commit_hash"],
            }
        if "INSERT INTO memory_versions" in sql:
            row = {
                "id": uuid.uuid4(),
                "version_num": 1,
                "commit_hash": args[13],
                "branch": args[14],
                "parent_version_id": args[15],
            }
            compression_versions.append(row)
            return row
        if "FROM memory_versions" in sql and "commit_hash" in sql:
            for row in compression_versions:
                if row["branch"] == args[1] and row["commit_hash"] == args[2]:
                    return row
            return None
        raise AssertionError(f"unexpected fetchrow: {sql[:80]!r}")

    async def execute(sql, *args):
        execute_log.append((sql, args))
        return "UPDATE 1"

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.execute = AsyncMock(side_effect=execute)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=ctx)

    pool._execute_log = execute_log
    pool._fetchrow_calls = fetchrow_calls
    pool._compression_versions = compression_versions
    pool._conn = conn
    return pool


def _mark_done_calls(pool) -> list[tuple]:
    return [args for sql, args in pool._execute_log if "status      = 'done'" in sql]


def _mark_failed_calls(pool) -> list[tuple]:
    return [args for sql, args in pool._execute_log if "status      = 'failed'" in sql]


# ---- empty queue -----------------------------------------------------------


def test_empty_queue_returns_zero_counts():
    pool = _mock_pool(queue_rows=[], memory_content_by_id={})
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))
    assert counts == {}


# ---- happy path ------------------------------------------------------------


def test_happy_path_marks_all_done_when_every_row_has_winner():
    q1 = _queue_row()
    q2 = _queue_row()
    pool = _mock_pool(
        queue_rows=[q1, q2],
        memory_content_by_id={
            q1["memory_id"]: _memory_row(q1["memory_id"]),
            q2["memory_id"]: _memory_row(q2["memory_id"]),
        },
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1", quality=0.9, ratio=0.3))]

    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts["dequeued"] == 2
    assert counts["succeeded"] == 2
    assert counts.get("failed", 0) == 0
    assert len(_mark_done_calls(pool)) == 2
    assert len(_mark_failed_calls(pool)) == 0


# ---- max_attempts fast-fail -----------------------------------------------


def test_max_attempts_exceeded_fast_fails_without_running_contest():
    # attempts=5 is already over max_attempts=3 AFTER the dequeue
    # (dequeue already incremented from 4 to 5). Row skips the contest.
    q = _queue_row(attempts=5)
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]

    counts = asyncio.run(process_contest_queue(pool, engines, max_attempts=3))

    assert counts["dequeued"] == 1
    assert counts["skipped_max_attempts"] == 1
    assert counts["failed"] == 1
    assert counts.get("succeeded", 0) == 0

    # fetchrow for the memory should NOT have been called — we fast-failed
    assert pool._fetchrow_calls == []
    # Exactly one mark_failed call, with the max-attempts message
    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert "max_attempts exceeded" in failed[0][1]


# ---- missing memory --------------------------------------------------------


def test_missing_memory_marks_failed():
    q = _queue_row()
    # Memory content dict is EMPTY — fetchrow returns None
    pool = _mock_pool(queue_rows=[q], memory_content_by_id={})
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]

    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts["dequeued"] == 1
    assert counts["missing_memory"] == 1
    assert counts["failed"] == 1
    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert "memory not found" in failed[0][1]


def test_empty_content_marks_failed():
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"], content="")},
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]

    counts = asyncio.run(process_contest_queue(pool, engines))
    assert counts["missing_memory"] == 1
    assert counts["failed"] == 1


# ---- too-short gate --------------------------------------------------------


def test_too_short_gate_skips_contest_when_threshold_set():
    # Threshold=500, memory content is 100 chars → should be gated
    # out before the contest runs.
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"], content="x" * 100)},
    )
    engine_called = False

    class _Watcher(CompressionEngine):
        id = "watch"
        label = "Watcher"
        version = "1"
        gpu_intent = GPUIntent.CPU_ONLY

        async def compress(self, req):
            nonlocal engine_called
            engine_called = True
            return CompressionResult(
                engine_id=self.id, engine_version=self.version, original_tokens=10,
                compressed_content="x", compression_ratio=0.5, quality_score=0.9,
            )

    counts = asyncio.run(process_contest_queue(pool, [_Watcher()], min_content_length=500))
    assert engine_called is False, "threshold gate should skip the contest entirely"
    assert counts["dequeued"] == 1
    assert counts["skipped_too_short"] == 1
    assert counts["failed"] == 1

    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert "too_short" in failed[0][1]
    assert "100 chars" in failed[0][1]
    assert "500" in failed[0][1]


def test_too_short_gate_off_by_default():
    # min_content_length=0 (default) → even very short content runs
    # through the contest normally.
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"], content="x" * 10)},
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))  # no min_content_length
    assert counts.get("skipped_too_short", 0) == 0
    assert counts["succeeded"] == 1


def test_too_short_gate_at_threshold_passes_through():
    # Content length EXACTLY at threshold should NOT be gated —
    # gate triggers strictly below.
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"], content="x" * 500)},
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines, min_content_length=500))
    assert counts.get("skipped_too_short", 0) == 0
    assert counts["succeeded"] == 1


# ---- no winner -------------------------------------------------------------


def test_no_winner_marks_failed_with_reject_reasons_summary():
    # Two engines, both error; contest yields no winner.
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    engines = [
        _StubEngine("bad1", result_factory=_bad_result("bad1")),
        _StubEngine("bad2", result_factory=_bad_result("bad2")),
    ]

    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts["dequeued"] == 1
    assert counts["failed"] == 1
    assert counts.get("succeeded", 0) == 0
    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert "no winner" in failed[0][1]
    assert "error=2" in failed[0][1]


# ---- mixed batch -----------------------------------------------------------


def test_mixed_batch_partial_success():
    q_ok = _queue_row()
    q_missing = _queue_row()
    pool = _mock_pool(
        queue_rows=[q_ok, q_missing],
        memory_content_by_id={q_ok["memory_id"]: _memory_row(q_ok["memory_id"])},
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]

    counts = asyncio.run(process_contest_queue(pool, engines))
    assert counts["dequeued"] == 2
    assert counts["succeeded"] == 1
    assert counts["failed"] == 1
    assert counts["missing_memory"] == 1


# ---- persist raises --------------------------------------------------------


def test_persist_raising_marks_failed():
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )

    # Override the connection's fetchrow so the INSERT ... RETURNING id
    # raises (simulating persist_contest failure).
    original_fetchrow = pool._conn.fetchrow.side_effect
    call_count = {"n": 0}

    async def fetchrow_raising(sql, *args):
        if "INSERT INTO memory_compression_candidates" in sql:
            call_count["n"] += 1
            raise RuntimeError("db exploded")
        return await original_fetchrow(sql, *args)

    pool._conn.fetchrow = AsyncMock(side_effect=fetchrow_raising)

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts["dequeued"] == 1
    assert counts["failed"] == 1
    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert "persist failed" in failed[0][1]
    assert "db exploded" in failed[0][1]


# ---- stale-running sweep (v3.1.1) -----------------------------------------


from mnemos.domain.compression.worker_contest import _sweep_stale_running  # noqa: E402


def _sweep_row(*, attempts: int, status: str) -> dict:
    """Simulated RETURNING row from the sweep UPDATE."""
    return {"id": uuid.uuid4(), "status": status, "attempts": attempts}


def test_sweep_reclaims_row_under_max_to_pending():
    stale = _sweep_row(attempts=1, status="pending")
    pool = _mock_pool(queue_rows=[], memory_content_by_id={}, sweep_rows=[stale])
    counts = asyncio.run(_sweep_stale_running(pool))
    assert counts == {"stranded_reset": 1}


def test_sweep_marks_row_at_max_as_failed():
    stale = _sweep_row(attempts=3, status="failed")
    pool = _mock_pool(queue_rows=[], memory_content_by_id={}, sweep_rows=[stale])
    counts = asyncio.run(_sweep_stale_running(pool))
    assert counts == {"stranded_failed": 1}


def test_sweep_empty_returns_empty_dict():
    pool = _mock_pool(queue_rows=[], memory_content_by_id={}, sweep_rows=[])
    counts = asyncio.run(_sweep_stale_running(pool))
    assert counts == {}


def test_sweep_mixed_batch_counts_both_outcomes():
    reset_row = _sweep_row(attempts=1, status="pending")
    failed_row = _sweep_row(attempts=3, status="failed")
    pool = _mock_pool(
        queue_rows=[], memory_content_by_id={},
        sweep_rows=[reset_row, failed_row],
    )
    counts = asyncio.run(_sweep_stale_running(pool))
    assert counts == {"stranded_reset": 1, "stranded_failed": 1}


def test_sweep_sql_receives_threshold_and_max_attempts():
    """Sweep must pass through stale_threshold_secs and max_attempts
    as SQL parameters so the CASE expression reclaims vs fails correctly.
    """
    captured_args = []

    async def capture_fetch(sql, *args):
        if "status = 'running'" in sql:
            captured_args.append(args)
            return []
        raise AssertionError(f"unexpected fetch: {sql!r}")

    pool = _mock_pool(queue_rows=[], memory_content_by_id={})
    pool._conn.fetch = AsyncMock(side_effect=capture_fetch)

    asyncio.run(_sweep_stale_running(
        pool, stale_threshold_secs=900, max_attempts=5,
    ))

    assert captured_args == [(900, 5)]


def test_process_contest_queue_runs_sweep_before_dequeue():
    """Default stale_threshold_secs > 0 ⇒ sweep runs, counts merged."""
    reset_row = _sweep_row(attempts=1, status="pending")
    pool = _mock_pool(
        queue_rows=[], memory_content_by_id={}, sweep_rows=[reset_row],
    )
    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]

    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts.get("stranded_reset") == 1
    assert "dequeued" not in counts


def test_process_contest_queue_sweep_disabled_when_threshold_zero():
    """stale_threshold_secs=0 must skip the sweep entirely — no fetch
    call matching the sweep SQL should happen.
    """
    fetch_sqls = []

    async def recording_fetch(sql, *args):
        fetch_sqls.append(sql)
        if "status = 'pending'" in sql:
            return []
        raise AssertionError(f"unexpected fetch: {sql!r}")

    pool = _mock_pool(queue_rows=[], memory_content_by_id={})
    pool._conn.fetch = AsyncMock(side_effect=recording_fetch)

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    asyncio.run(process_contest_queue(pool, engines, stale_threshold_secs=0))

    assert not any("status = 'running'" in s for s in fetch_sqls)


def test_sweep_failure_does_not_block_dequeue():
    """If the sweep raises (e.g. transient DB blip), process_contest_queue
    must still run the dequeue. Sweep error is logged, not propagated.
    """
    q = _queue_row()

    async def fetch_sweep_raises(sql, *args):
        if "status = 'running'" in sql:
            raise RuntimeError("sweep db blip")
        if "status = 'pending'" in sql and "FOR UPDATE SKIP LOCKED" in sql:
            return [q]
        raise AssertionError(f"unexpected fetch: {sql!r}")

    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    pool._conn.fetch = AsyncMock(side_effect=fetch_sweep_raises)

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    # Sweep errored, but dequeue still ran and the row succeeded.
    assert counts.get("dequeued") == 1
    assert counts.get("succeeded") == 1


# ---- sweep-vs-late-finisher race (v3.1.1) ---------------------------------


def _install_precondition_fetchrow(pool, *, returning):
    """Override the mock's fetchrow to return a forged precondition row.

    `returning` is the dict the SELECT status, attempts query should
    return (or None). Everything else falls back to the original
    fetchrow behavior.
    """
    original = pool._conn.fetchrow.side_effect

    async def override(sql, *args):
        if sql.strip().startswith("SELECT status, attempts"):
            return returning
        return await original(sql, *args)

    pool._conn.fetchrow = AsyncMock(side_effect=override)


def test_precondition_status_mismatch_abandons_work():
    """Sweep reclaimed the row to 'pending' before the worker's persist
    transaction opened. The worker must NOT persist the contest, NOT
    mark the queue row, and record race_abandoned.
    """
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    # Sweep reclaimed → status flipped to 'pending'.
    _install_precondition_fetchrow(
        pool, returning={"status": "pending", "attempts": q["attempts"]}
    )

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts.get("dequeued") == 1
    assert counts.get("race_abandoned") == 1
    # No persist, no MARK_DONE/MARK_FAILED fired.
    assert _mark_done_calls(pool) == []
    assert _mark_failed_calls(pool) == []


def test_precondition_attempts_mismatch_abandons_work():
    """Sweep reset row, another worker re-dequeued (bumping attempts).
    Our worker's fingerprint (expected_attempts=N) no longer matches
    the DB's N+1 — abandon work cleanly.
    """
    q = _queue_row(attempts=1)
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    # Row is 'running' again (worker-2 re-dequeued) but attempts bumped.
    _install_precondition_fetchrow(
        pool, returning={"status": "running", "attempts": q["attempts"] + 1}
    )

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts.get("race_abandoned") == 1
    assert _mark_done_calls(pool) == []
    assert _mark_failed_calls(pool) == []


def test_precondition_row_vanished_abandons_work():
    """Row was deleted (ON DELETE CASCADE from memories drop, etc.)
    between dequeue and persist. Treat as race_abandoned, not crash.
    """
    q = _queue_row()
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )
    _install_precondition_fetchrow(pool, returning=None)

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts.get("race_abandoned") == 1


def test_precondition_match_proceeds_normally():
    """Sanity: when precondition matches (status='running' + attempts
    unchanged), the contest persists and the queue row is marked done
    as before. This is the hot path regression guard.
    """
    q = _queue_row(attempts=2)
    pool = _mock_pool(
        queue_rows=[q],
        memory_content_by_id={q["memory_id"]: _memory_row(q["memory_id"])},
    )

    engines = [_StubEngine("e1", result_factory=_good_result("e1"))]
    counts = asyncio.run(process_contest_queue(pool, engines))

    assert counts.get("succeeded") == 1
    assert counts.get("race_abandoned", 0) == 0
    assert len(_mark_done_calls(pool)) == 1

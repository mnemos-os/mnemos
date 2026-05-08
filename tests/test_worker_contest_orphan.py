"""Regression coverage for engine exceptions in the contest worker."""
from __future__ import annotations

import asyncio

from mnemos.domain.compression.base import (
    CompressionEngine,
    CompressionResult,
    GPUIntent,
)
from mnemos.domain.compression.worker_contest import process_contest_queue
from tests.test_worker_contest import (
    _mark_done_calls,
    _mark_failed_calls,
    _memory_row,
    _mock_pool,
    _queue_row,
)


class _SyntheticEngine(CompressionEngine):
    id = "synthetic"
    label = "Synthetic"
    version = "1"
    gpu_intent = GPUIntent.CPU_ONLY

    def __init__(self, *, fail_memory_id: str) -> None:
        self._fail_memory_id = fail_memory_id
        super().__init__()

    async def compress(self, request):
        if request.memory_id == self._fail_memory_id:
            raise RuntimeError("synthetic engine failure")
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=100,
            compressed_tokens=40,
            compressed_content="compressed",
            compression_ratio=0.4,
            quality_score=0.9,
            elapsed_ms=10,
        )


def test_engine_exception_marks_row_failed_and_continues_to_next_row():
    bad = _queue_row()
    good = _queue_row()
    pool = _mock_pool(
        queue_rows=[bad, good],
        memory_content_by_id={
            bad["memory_id"]: _memory_row(bad["memory_id"]),
            good["memory_id"]: _memory_row(good["memory_id"]),
        },
    )

    counts = asyncio.run(process_contest_queue(
        pool,
        [_SyntheticEngine(fail_memory_id=bad["memory_id"])],
    ))

    assert counts["dequeued"] == 2
    assert counts["failed"] == 1
    assert counts["succeeded"] == 1

    failed = _mark_failed_calls(pool)
    assert len(failed) == 1
    assert failed[0][0] == bad["id"]
    assert "RuntimeError" in failed[0][1]
    assert "synthetic engine failure" in failed[0][1]
    assert len(_mark_done_calls(pool)) == 1

"""Regression coverage for MORPHEUS extract run counters."""
from __future__ import annotations

import pytest

from mnemos.core import config as core_config
from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import ExtractedTriple, phase_extract
from tests.test_morpheus_extract import RUN_ID, _Conn, _Pool, _memory


@pytest.fixture(autouse=True)
def reset_morpheus_extract_settings(monkeypatch):
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFY", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CHARS", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MUSE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFIER", raising=False)
    core_config._reset_settings_for_tests()
    yield
    core_config._reset_settings_for_tests()


@pytest.mark.asyncio
async def test_phase_extract_increments_run_counter_mid_phase(monkeypatch):
    conn = _Conn(memories=[
        _memory("mem_0", created_offset=0),
        _memory("mem_1", created_offset=1),
    ])
    calls = 0

    async def one_triple(content: str) -> list[ExtractedTriple]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert conn.run_row["triples_extracted"] > 0
            assert conn.run_row["memories_processed_for_extraction"] > 0
        memory_id = content.split()[0]
        return [
            ExtractedTriple(
                f"{memory_id}:subject",
                "relates_to",
                f"{memory_id}:object",
                0.9,
            )
        ]

    monkeypatch.setattr(runner, "_extract_triples_from_prose", one_triple)

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert calls == 2
    assert n == 2
    assert conn.run_row["triples_extracted"] == 2
    assert conn.run_row["memories_processed_for_extraction"] == 2

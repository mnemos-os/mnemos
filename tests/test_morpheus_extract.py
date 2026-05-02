"""Tests for MORPHEUS slice 4: EXTRACT."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from mnemos.core import config as core_config
from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import ExtractedTriple, phase_extract, rollback_run


RUN_ID = "00000000-0000-0000-0000-0000000000e4"
OTHER_RUN_ID = "00000000-0000-0000-0000-0000000000f5"
_DEFAULT_CONTENT = object()


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


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def __init__(
        self,
        *,
        run_config: dict | None = None,
        run_namespace: str | None = "A",
        memories: list[dict] | None = None,
        kg_triples: list[dict] | None = None,
    ):
        self.run_row = {"config": run_config or {"extract": True}, "namespace": run_namespace}
        self.memories = {row["id"]: row for row in memories or []}
        self.kg_triples = list(kg_triples or [])
        self.executed: list[tuple[str, tuple]] = []
        self.counter_updates: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *_args):
        if "FROM morpheus_runs" in sql:
            return self.run_row
        return None

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "SELECT id, verbatim_content, owner_id, namespace" not in compact:
            return []
        min_chars, namespace = args
        out = []
        for row in sorted(self.memories.values(), key=lambda item: item["created"]):
            content = row.get("verbatim_content")
            if row.get("deleted_at") is not None:
                continue
            if row.get("triples_extracted_at") is not None:
                continue
            if content is None or len(content) < min_chars:
                continue
            if namespace is not None and row.get("namespace") != namespace:
                continue
            out.append(row)
        return out

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("UPDATE memories SET triples_extracted_at = NOW()"):
            memory_id, namespace = args
            row = self.memories.get(memory_id)
            if row is None or row.get("deleted_at") is not None:
                return None
            if row.get("triples_extracted_at") is not None:
                return None
            if namespace is not None and row.get("namespace") != namespace:
                return None
            row["triples_extracted_at"] = "now"
            return memory_id
        return None

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO kg_triples"):
            self.kg_triples.append({
                "id": args[0],
                "subject": args[1],
                "predicate": args[2],
                "object": args[3],
                "memory_id": args[4],
                "confidence": args[5],
                "extracted_by_run_id": args[6],
                "owner_id": args[7],
                "namespace": args[8],
            })
            return "INSERT 0 1"
        if compact.startswith("WITH deleted_extract_triples AS"):
            return self._execute_extract_rollback(args[0])
        if compact.startswith("UPDATE memories SET consolidated_into = NULL"):
            return "UPDATE 0"
        if compact.startswith("DELETE FROM memories WHERE morpheus_run_id"):
            return "DELETE 0"
        if compact.startswith("UPDATE morpheus_runs"):
            self.counter_updates.append((sql, args))
            return "UPDATE 1"
        return "OK"

    def _execute_extract_rollback(self, run_id: str) -> str:
        affected_memory_ids = {
            row.get("memory_id")
            for row in self.kg_triples
            if row.get("extracted_by_run_id") == run_id and row.get("memory_id")
        }
        self.kg_triples = [
            row for row in self.kg_triples
            if row.get("extracted_by_run_id") != run_id
        ]
        reset = 0
        for memory_id in affected_memory_ids:
            row = self.memories.get(memory_id)
            if row is not None:
                row["triples_extracted_at"] = None
                reset += 1
        return f"UPDATE {reset}"


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


def _long_prose(memory_id: str) -> str:
    return (
        f"{memory_id} captures a durable product decision involving Alice, "
        "Project Helios, and the April launch window. "
        "The prose is intentionally longer than the default extraction "
        "threshold so MORPHEUS treats it as candidate text. "
        "It includes enough detail for multiple simple facts."
    )


def _memory(
    memory_id: str,
    *,
    namespace: str = "A",
    verbatim_content: object = _DEFAULT_CONTENT,
    triples_extracted_at: object | None = None,
    created_offset: int = 0,
) -> dict:
    content = _long_prose(memory_id) if verbatim_content is _DEFAULT_CONTENT else verbatim_content
    return {
        "id": memory_id,
        "verbatim_content": content,
        "owner_id": f"owner-{namespace}",
        "namespace": namespace,
        "deleted_at": None,
        "triples_extracted_at": triples_extracted_at,
        "created": datetime(2026, 5, 2, 12, 0, 0) + timedelta(minutes=created_offset),
    }


async def _three_triples(content: str) -> list[ExtractedTriple]:
    memory_id = content.split()[0]
    return [
        ExtractedTriple(f"{memory_id}:subject:{idx}", "relates_to", f"{memory_id}:object:{idx}", 0.9)
        for idx in range(3)
    ]


@pytest.mark.asyncio
async def test_phase_extract_three_memories_three_triples_each(monkeypatch):
    monkeypatch.setattr(runner, "_extract_triples_from_prose", _three_triples)
    conn = _Conn(memories=[
        _memory("mem_0", created_offset=0),
        _memory("mem_1", created_offset=1),
        _memory("mem_2", created_offset=2),
    ])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 9
    assert len(conn.kg_triples) == 9
    assert {row["memory_id"] for row in conn.kg_triples} == {"mem_0", "mem_1", "mem_2"}
    assert all(row["extracted_by_run_id"] == RUN_ID for row in conn.kg_triples)
    assert all(row["namespace"] == "A" and row["owner_id"] == "owner-A" for row in conn.kg_triples)
    assert all(row["triples_extracted_at"] == "now" for row in conn.memories.values())


@pytest.mark.asyncio
async def test_phase_extract_idempotent_on_rerun(monkeypatch):
    calls: list[str] = []

    async def fake_extract(content: str) -> list[ExtractedTriple]:
        calls.append(content)
        return await _three_triples(content)

    monkeypatch.setattr(runner, "_extract_triples_from_prose", fake_extract)
    conn = _Conn(memories=[
        _memory("mem_0", created_offset=0),
        _memory("mem_1", created_offset=1),
        _memory("mem_2", created_offset=2),
    ])
    pool = _Pool(conn)

    first = await phase_extract(pool, RUN_ID)
    second = await phase_extract(pool, RUN_ID)

    assert first == 9
    assert second == 0
    assert len(calls) == 3
    assert len(conn.kg_triples) == 9


@pytest.mark.asyncio
async def test_phase_extract_skips_null_and_short_verbatim_content(monkeypatch):
    async def one_triple(_content: str) -> list[ExtractedTriple]:
        return [ExtractedTriple("Alice", "owns", "Project Helios", 0.9)]

    monkeypatch.setattr(
        runner,
        "_extract_triples_from_prose",
        one_triple,
    )
    conn = _Conn(memories=[
        _memory("null_content", verbatim_content=None, created_offset=0),
        _memory("short_content", verbatim_content="too short", created_offset=1),
        _memory("eligible", created_offset=2),
    ])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 1
    assert [row["memory_id"] for row in conn.kg_triples] == ["eligible"]
    assert conn.memories["null_content"]["triples_extracted_at"] is None
    assert conn.memories["short_content"]["triples_extracted_at"] is None
    assert conn.memories["eligible"]["triples_extracted_at"] == "now"


@pytest.mark.asyncio
async def test_malformed_fast_muse_json_discards_triples_but_marks_memory(monkeypatch):
    async def malformed_json(*_args, **_kwargs) -> str:
        return "this is not json"

    monkeypatch.setattr(runner, "_call_morpheus_muse", malformed_json)
    conn = _Conn(memories=[_memory("mem_bad_json")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 0
    assert conn.kg_triples == []
    assert conn.memories["mem_bad_json"]["triples_extracted_at"] == "now"


@pytest.mark.asyncio
async def test_rollback_run_removes_only_triples_from_that_run():
    conn = _Conn(
        memories=[
            _memory("mem_a", triples_extracted_at="done"),
            _memory("mem_b", triples_extracted_at="done"),
            _memory("mem_c", triples_extracted_at="done"),
        ],
        kg_triples=[
            {"id": "run_a", "memory_id": "mem_a", "extracted_by_run_id": RUN_ID},
            {"id": "run_b", "memory_id": "mem_b", "extracted_by_run_id": RUN_ID},
            {"id": "other_b", "memory_id": "mem_b", "extracted_by_run_id": OTHER_RUN_ID},
            {"id": "manual_c", "memory_id": "mem_c", "extracted_by_run_id": None},
        ],
    )

    deleted, run_rows = await rollback_run(_Pool(conn), RUN_ID)

    assert deleted == 0
    assert run_rows == 1
    assert {row["id"] for row in conn.kg_triples} == {"other_b", "manual_c"}
    assert conn.memories["mem_a"]["triples_extracted_at"] is None
    assert conn.memories["mem_b"]["triples_extracted_at"] is None
    assert conn.memories["mem_c"]["triples_extracted_at"] == "done"


@pytest.mark.asyncio
async def test_phase_extract_is_namespace_scoped(monkeypatch):
    async def one_triple(_content: str) -> list[ExtractedTriple]:
        return [ExtractedTriple("Alice", "owns", "Project Helios", 0.9)]

    monkeypatch.setattr(
        runner,
        "_extract_triples_from_prose",
        one_triple,
    )
    conn = _Conn(
        run_namespace="A",
        memories=[
            _memory("mem_a", namespace="A", created_offset=0),
            _memory("mem_b", namespace="B", created_offset=1),
        ],
    )

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 1
    assert [row["memory_id"] for row in conn.kg_triples] == ["mem_a"]
    assert conn.memories["mem_a"]["triples_extracted_at"] == "now"
    assert conn.memories["mem_b"]["triples_extracted_at"] is None


@pytest.mark.asyncio
async def test_extract_verify_filters_below_min_confidence(monkeypatch):
    async def fake_muse(_prompt: str, *, task_type: str, **_kwargs) -> str:
        if task_type == "kg_extraction_verification":
            return json.dumps([
                {"index": 0, "confidence": 0.95},
                {"index": 1, "confidence": 0.59},
                {"index": 2, "confidence": 0.60},
            ])
        return json.dumps([
            {"subject": "Alice", "predicate": "owns", "object": "Project Helios", "confidence": 0.9},
            {"subject": "Bob", "predicate": "owns", "object": "Project Helios", "confidence": 0.9},
            {"subject": "Helios", "predicate": "launches_in", "object": "April", "confidence": 0.9},
        ])

    monkeypatch.setattr(runner, "_call_morpheus_muse", fake_muse)
    conn = _Conn(run_config={"extract": True, "extract_verify": True}, memories=[_memory("mem_verify")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 2
    assert [(row["subject"], row["confidence"]) for row in conn.kg_triples] == [
        ("Alice", 0.95),
        ("Helios", 0.60),
    ]


@pytest.mark.asyncio
async def test_run_dream_inserts_extract_phase_after_synthesise(monkeypatch):
    calls: list[str] = []

    async def fake_begin_run(*_args, **_kwargs):
        return RUN_ID

    async def fake_set_phase(_pool, _run_id, phase):
        calls.append(f"phase:{phase}")

    async def fake_phase(_pool, _run_id):
        calls.append("phase_fn")
        return 0

    async def fake_extract(_pool, _run_id):
        calls.append("extract_fn")
        return 0

    async def fake_finish(_pool, _run_id):
        calls.append("finish")

    monkeypatch.setattr(runner, "begin_run", fake_begin_run)
    monkeypatch.setattr(runner, "set_phase", fake_set_phase)
    monkeypatch.setattr(runner, "phase_replay", fake_phase)
    monkeypatch.setattr(runner, "phase_cluster", fake_phase)
    monkeypatch.setattr(runner, "phase_synthesise", fake_phase)
    monkeypatch.setattr(runner, "phase_extract", fake_extract)
    monkeypatch.setattr(runner, "finish_run", fake_finish)

    run_id = await runner.run_dream(object(), config={"extract": True})

    assert run_id == RUN_ID
    assert calls == [
        "phase:replay",
        "phase_fn",
        "phase:cluster",
        "phase_fn",
        "phase:synthesise",
        "phase_fn",
        "phase:extract",
        "extract_fn",
        "phase:commit",
        "finish",
    ]

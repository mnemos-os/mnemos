from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from mnemos.domain.compression.base import CompressionResult, IdentifierPolicy
from mnemos.domain.compression.contest import ContestCandidate, ContestOutcome
from mnemos.domain.compression.contest_store import (
    _compression_commit_hash,
    persist_contest,
)


class _DAGConn:
    def __init__(self) -> None:
        self.memory = {
            "memory_id": "mem-dag-1",
            "category": "architecture",
            "subcategory": "dag",
            "metadata": {"source": "unit"},
            "verbatim_content": "raw source memory",
            "owner_id": "alice",
            "namespace": "default",
            "permission_mode": 640,
            "source_model": "raw-model",
            "source_provider": "raw-provider",
            "source_session": "session-1",
            "source_agent": "agent-1",
        }
        self.create_version = {
            "id": uuid.uuid4(),
            "memory_id": self.memory["memory_id"],
            "version_num": 1,
            "content": "raw source memory",
            "category": self.memory["category"],
            "subcategory": self.memory["subcategory"],
            "commit_hash": "b" * 64,
            "parent_version_id": None,
            "branch": "main",
            "snapshot_by": "alice",
            "change_type": "create",
        }
        self.memory_versions = {self.create_version["id"]: self.create_version}
        self.branch_heads = {"main": self.create_version["id"]}
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.candidate_rows: list[dict[str, Any]] = []
        self.variant_rows: list[dict[str, Any]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((sql, args))
        if "INSERT INTO memory_compression_candidates" in sql:
            row = {"id": uuid.uuid4(), "engine_id": args[3], "is_winner": args[16]}
            self.candidate_rows.append(row)
            return row
        if "FROM memory_branches mb" in sql and "mb.name = 'main'" in sql:
            return {
                **self.memory,
                "parent_version_id": self.create_version["id"],
                "parent_commit_hash": self.create_version["commit_hash"],
            }
        if "INSERT INTO memory_versions" in sql:
            commit_hash = args[13]
            if any(v["commit_hash"] == commit_hash for v in self.memory_versions.values()):
                return None
            branch = args[14]
            same_branch_versions = [
                v for v in self.memory_versions.values()
                if v["memory_id"] == args[0] and v["branch"] == branch
            ]
            row = {
                "id": uuid.uuid4(),
                "memory_id": args[0],
                "version_num": max((v["version_num"] for v in same_branch_versions), default=0) + 1,
                "content": args[1],
                "category": args[2],
                "subcategory": args[3],
                "metadata": json.loads(args[4]),
                "verbatim_content": args[5],
                "owner_id": args[6],
                "namespace": args[7],
                "permission_mode": args[8],
                "source_model": args[9],
                "source_provider": args[10],
                "source_session": args[11],
                "source_agent": args[12],
                "snapshot_by": "system:compression",
                "change_type": "compress",
                "commit_hash": commit_hash,
                "branch": branch,
                "parent_version_id": args[15],
            }
            self.memory_versions[row["id"]] = row
            return {
                "id": row["id"],
                "version_num": row["version_num"],
                "commit_hash": row["commit_hash"],
                "parent_version_id": row["parent_version_id"],
                "branch": row["branch"],
            }
        if "FROM memory_versions" in sql and "commit_hash" in sql:
            for row in self.memory_versions.values():
                if row["memory_id"] == args[0] and row["branch"] == args[1] and row["commit_hash"] == args[2]:
                    return row
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        if sql.startswith("SET LOCAL mnemos.suppress_version_snapshot"):
            return "SET"
        if "INSERT INTO memory_compressed_variants" in sql:
            self.variant_rows.append({
                "memory_id": args[0],
                "winner_candidate_id": args[2],
                "compressed_content": args[5],
            })
            return "INSERT 0 1"
        if "INSERT INTO memory_branches" in sql:
            self.branch_heads[args[1]] = args[2]
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _outcome(
    *,
    content: str = "dense:alpha",
    representation_kind: str = "dense",
    engine_id: str = "apollo",
) -> ContestOutcome:
    result = CompressionResult(
        engine_id=engine_id,
        engine_version="1",
        original_tokens=100,
        compressed_tokens=4,
        compressed_content=content,
        compression_ratio=0.04,
        quality_score=0.93,
        elapsed_ms=5,
        judge_model="judge-1",
        gpu_used=False,
        identifier_policy=IdentifierPolicy.STRICT,
        manifest={"representation_kind": representation_kind},
    )
    candidate = ContestCandidate(
        result=result,
        speed_factor=1.0,
        composite_score=0.89,
        is_winner=True,
    )
    return ContestOutcome(
        contest_id=uuid.uuid4(),
        memory_id="mem-dag-1",
        owner_id="alice",
        scoring_profile="balanced",
        candidates=[candidate],
        winner=candidate,
    )


def _compression_children(conn: _DAGConn) -> list[dict[str, Any]]:
    return [
        row for row in conn.memory_versions.values()
        if row["change_type"] == "compress"
    ]


def test_successful_contest_creates_distilled_version_from_main_head():
    conn = _DAGConn()

    result = asyncio.run(persist_contest(conn, _outcome()))

    child = _compression_children(conn)[0]
    assert child["branch"] == "distilled"
    assert child["parent_version_id"] == conn.create_version["id"]
    assert child["category"] == conn.memory["category"]
    assert child["subcategory"] == conn.memory["subcategory"]
    assert child["snapshot_by"] == "system:compression"
    assert result["compression_version_branch"] == "distilled"


def test_prose_narration_variant_creates_narrated_branch():
    conn = _DAGConn()

    asyncio.run(persist_contest(
        conn,
        _outcome(
            content="Alice summarized the decision in prose.",
            representation_kind="prose_narration",
            engine_id="narrator",
        ),
    ))

    child = _compression_children(conn)[0]
    assert child["branch"] == "narrated"
    assert conn.branch_heads["narrated"] == child["id"]


def test_compression_commit_hash_is_deterministic_and_tamper_evident():
    parent = "c" * 64

    first = _compression_commit_hash(parent, "variant body", "distilled")
    second = _compression_commit_hash(parent, "variant body", "distilled")
    tampered = _compression_commit_hash(parent, "variant body!", "distilled")

    assert first == second
    assert first != tampered


def test_direct_memory_versions_insert_does_not_create_duplicate_trigger_row():
    conn = _DAGConn()

    asyncio.run(persist_contest(conn, _outcome()))

    version_insert_calls = [
        sql for sql, _args in conn.fetchrow_calls
        if "INSERT INTO memory_versions" in sql
    ]
    assert len(version_insert_calls) == 1
    assert len(_compression_children(conn)) == 1
    assert any(
        sql.startswith("SET LOCAL mnemos.suppress_version_snapshot")
        for sql, _args in conn.execute_calls
    )
    assert not any(
        "UPDATE memories" in sql or "INSERT INTO memories" in sql
        for sql, _args in conn.execute_calls
    )


def test_distilled_child_walks_back_to_source_create_version():
    conn = _DAGConn()

    asyncio.run(persist_contest(conn, _outcome()))

    child = _compression_children(conn)[0]
    parent = conn.memory_versions[child["parent_version_id"]]
    assert parent["id"] == conn.create_version["id"]
    assert parent["branch"] == "main"
    assert parent["change_type"] == "create"
    assert parent["parent_version_id"] is None

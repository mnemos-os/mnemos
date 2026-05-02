"""Tests for MORPHEUS slice 3: CONSOLIDATE."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mnemos.core import config as core_config
from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import (
    phase_consolidate,
    phase_synthesise,
    rollback_run,
)


RUN_ID = "00000000-0000-0000-0000-0000000000c3"


@pytest.fixture(autouse=True)
def reset_morpheus_settings(monkeypatch):
    monkeypatch.delenv("MNEMOS_MORPHEUS_USE_LLM", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_CONSOLIDATE", raising=False)
    core_config._reset_settings_for_tests()
    yield
    core_config._reset_settings_for_tests()


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, *, run_row: dict | None = None, memories: list[dict] | None = None):
        self.run_row = run_row
        self.memories = {row["id"]: row for row in memories or []}
        self.executed: list[tuple[str, tuple]] = []
        self.inserts: list[tuple[str, tuple]] = []
        self.memory_versions: list[dict] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *_args):
        if "FROM morpheus_runs" in sql:
            return self.run_row
        return None

    async def fetchval(self, sql: str, *_args):
        if "SELECT config FROM morpheus_runs" in sql and self.run_row:
            return self.run_row["config"]
        return None

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "FROM memories" not in compact:
            return []

        requested = list(args[0])
        namespace = args[1] if len(args) > 1 else None
        only_unconsolidated = "consolidated_into IS NULL" in compact
        rows = []
        for memory_id in requested:
            row = self.memories.get(memory_id)
            if row is None or row.get("deleted_at") is not None:
                continue
            if namespace is not None and row.get("namespace") != namespace:
                continue
            if only_unconsolidated and row.get("consolidated_into") is not None:
                continue
            rows.append(row)
        return rows

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("UPDATE memories SET consolidated_into=$2"):
            return self._execute_consolidate_update(*args)
        if compact.startswith("UPDATE memories SET consolidated_into = NULL"):
            return self._execute_restore(*args)
        if compact.startswith("DELETE FROM memories WHERE morpheus_run_id"):
            return self._execute_delete_run_rows(args[0])
        if compact.startswith("UPDATE morpheus_runs"):
            return "UPDATE 1"
        if compact.startswith("INSERT INTO memories"):
            self.inserts.append((sql, args))
            self.memories[args[0]] = {
                "id": args[0],
                "content": args[1],
                "category": args[2],
                "subcategory": args[3],
                "metadata": json.loads(args[4]),
                "owner_id": args[5],
                "namespace": args[6],
                "permission_mode": 600,
                "morpheus_run_id": args[7],
                "source_memories": args[8],
                "provenance": "morpheus_local",
                "deleted_at": None,
                "consolidated_into": None,
            }
            return "INSERT 0 1"
        return "OK"

    def _execute_consolidate_update(
        self,
        memory_id: str,
        canonical_id: str,
        run_id: str,
        namespace: str | None,
        permission_mode: int,
    ) -> str:
        row = self.memories.get(memory_id)
        if row is None or row.get("deleted_at") is not None:
            return "UPDATE 0"
        if row.get("consolidated_into") is not None or row.get("morpheus_run_id") is not None:
            return "UPDATE 0"
        if namespace is not None and row.get("namespace") != namespace:
            return "UPDATE 0"

        metadata = dict(row.get("metadata") or {})
        metadata.setdefault("pre_consolidate_permission_mode", row["permission_mode"])
        row["metadata"] = metadata
        row["consolidated_into"] = canonical_id
        row["permission_mode"] = permission_mode
        row["morpheus_run_id"] = run_id
        self.memory_versions.append({
            "memory_id": memory_id,
            "permission_mode": permission_mode,
            "metadata": dict(metadata),
        })
        return "UPDATE 1"

    def _execute_restore(self, run_id: str, metadata_key: str) -> str:
        restored = 0
        for row in self.memories.values():
            metadata = dict(row.get("metadata") or {})
            if row.get("morpheus_run_id") != run_id or metadata_key not in metadata:
                continue
            row["consolidated_into"] = None
            row["permission_mode"] = int(metadata[metadata_key])
            metadata.pop(metadata_key, None)
            row["metadata"] = metadata
            row["morpheus_run_id"] = None
            restored += 1
        return f"UPDATE {restored}"

    def _execute_delete_run_rows(self, run_id: str) -> str:
        doomed = [
            memory_id
            for memory_id, row in self.memories.items()
            if row.get("morpheus_run_id") == run_id and row.get("deleted_at") is None
            and row.get("provenance") == "morpheus_local"
        ]
        for memory_id in doomed:
            del self.memories[memory_id]
        return f"DELETE {len(doomed)}"


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


def _run_row(member_ids: list[str], *, cluster_min_size: int = 3, namespace: str | None = "A") -> dict:
    return {
        "config": {"clusters": [{"cluster_id": 0, "member_memory_ids": member_ids}]},
        "cluster_min_size": cluster_min_size,
        "namespace": namespace,
    }


def _memory(
    memory_id: str,
    *,
    recall_count: int = 0,
    created: datetime,
    namespace: str = "A",
    permission_mode: int = 600,
    consolidated_into: str | None = None,
    morpheus_run_id: str | None = None,
    content: str | None = None,
) -> dict:
    return {
        "id": memory_id,
        "recall_count": recall_count,
        "created": created,
        "permission_mode": permission_mode,
        "consolidated_into": consolidated_into,
        "morpheus_run_id": morpheus_run_id,
        "metadata": {},
        "deleted_at": None,
        "namespace": namespace,
        "content": content or f"{memory_id} content.",
        "category": "facts",
        "owner_id": "default",
    }


@pytest.mark.asyncio
async def test_consolidate_picks_highest_recall_and_updates_noncanonical():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = [f"mem_{i}" for i in range(5)]
    memories = [
        _memory("mem_0", recall_count=1, created=base),
        _memory("mem_1", recall_count=7, created=base + timedelta(minutes=1), permission_mode=640),
        _memory("mem_2", recall_count=3, created=base + timedelta(minutes=2), permission_mode=644),
        _memory("mem_3", recall_count=0, created=base + timedelta(minutes=3), permission_mode=600),
        _memory("mem_4", recall_count=2, created=base + timedelta(minutes=4), permission_mode=604),
    ]
    conn = _Conn(run_row=_run_row(ids), memories=memories)

    n = await phase_consolidate(_Pool(conn), RUN_ID)

    assert n == 4
    assert conn.memories["mem_1"]["consolidated_into"] is None
    assert conn.memories["mem_1"]["permission_mode"] == 640
    for memory_id in {"mem_0", "mem_2", "mem_3", "mem_4"}:
        row = conn.memories[memory_id]
        assert row["consolidated_into"] == "mem_1"
        assert row["permission_mode"] == 400
        assert row["morpheus_run_id"] == RUN_ID
        assert "pre_consolidate_permission_mode" in row["metadata"]


@pytest.mark.asyncio
async def test_consolidate_tiebreaker_earliest_created_wins():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = ["newer", "oldest", "middle"]
    conn = _Conn(
        run_row=_run_row(ids),
        memories=[
            _memory("newer", recall_count=5, created=base + timedelta(minutes=2)),
            _memory("oldest", recall_count=5, created=base),
            _memory("middle", recall_count=5, created=base + timedelta(minutes=1)),
        ],
    )

    await phase_consolidate(_Pool(conn), RUN_ID)

    assert conn.memories["oldest"]["consolidated_into"] is None
    assert conn.memories["newer"]["consolidated_into"] == "oldest"
    assert conn.memories["middle"]["consolidated_into"] == "oldest"


@pytest.mark.asyncio
async def test_synthesise_treats_consolidated_members_as_invisible():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = ["canonical", "hidden_a", "hidden_b"]
    conn = _Conn(
        run_row=_run_row(ids),
        memories=[
            _memory("canonical", created=base, content="Canonical fact survives."),
            _memory("hidden_a", created=base, consolidated_into="canonical", content="Hidden A."),
            _memory("hidden_b", created=base, consolidated_into="canonical", content="Hidden B."),
        ],
    )

    n = await phase_synthesise(_Pool(conn), RUN_ID)

    assert n == 1
    assert len(conn.inserts) == 1
    _sql, args = conn.inserts[0]
    assert "Canonical fact survives" in args[1]
    assert "Hidden A" not in args[1]
    assert "Hidden B" not in args[1]
    assert args[8] == ["canonical"]
    assert json.loads(args[4])["member_count"] == 1


@pytest.mark.asyncio
async def test_rollback_restores_consolidated_rows_and_deletes_run_inserts():
    base = datetime(2026, 5, 1, 12, 0, 0)
    original = _memory(
        "duplicate",
        created=base,
        permission_mode=400,
        consolidated_into="canonical",
        morpheus_run_id=RUN_ID,
    )
    original["metadata"] = {"pre_consolidate_permission_mode": 640}
    summary = _memory("summary", created=base, morpheus_run_id=RUN_ID)
    summary["provenance"] = "morpheus_local"
    conn = _Conn(memories=[original, summary])

    deleted, run_rows = await rollback_run(_Pool(conn), RUN_ID)

    assert deleted == 1
    assert run_rows == 1
    assert "summary" not in conn.memories
    restored = conn.memories["duplicate"]
    assert restored["consolidated_into"] is None
    assert restored["permission_mode"] == 640
    assert restored["morpheus_run_id"] is None
    assert "pre_consolidate_permission_mode" not in restored["metadata"]


@pytest.mark.asyncio
async def test_consolidate_idempotent_for_same_run():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = ["canonical", "dupe_a", "dupe_b"]
    conn = _Conn(
        run_row=_run_row(ids),
        memories=[
            _memory("canonical", recall_count=9, created=base),
            _memory("dupe_a", recall_count=1, created=base),
            _memory("dupe_b", recall_count=2, created=base),
        ],
    )
    pool = _Pool(conn)

    first = await phase_consolidate(pool, RUN_ID)
    second = await phase_consolidate(pool, RUN_ID)

    assert first == 2
    assert second == 2
    assert len(conn.memory_versions) == 2
    assert conn.memories["dupe_a"]["consolidated_into"] == "canonical"
    assert conn.memories["dupe_b"]["consolidated_into"] == "canonical"


@pytest.mark.asyncio
async def test_consolidate_is_namespace_scoped():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = ["a_canonical", "a_dupe_1", "a_dupe_2", "b_hot", "b_dupe"]
    conn = _Conn(
        run_row=_run_row(ids, namespace="A"),
        memories=[
            _memory("a_canonical", recall_count=4, created=base, namespace="A"),
            _memory("a_dupe_1", recall_count=1, created=base, namespace="A"),
            _memory("a_dupe_2", recall_count=2, created=base, namespace="A"),
            _memory("b_hot", recall_count=99, created=base, namespace="B"),
            _memory("b_dupe", recall_count=1, created=base, namespace="B"),
        ],
    )

    n = await phase_consolidate(_Pool(conn), RUN_ID)

    assert n == 2
    assert conn.memories["a_dupe_1"]["consolidated_into"] == "a_canonical"
    assert conn.memories["a_dupe_2"]["consolidated_into"] == "a_canonical"
    assert conn.memories["b_hot"]["consolidated_into"] is None
    assert conn.memories["b_dupe"]["consolidated_into"] is None


@pytest.mark.asyncio
async def test_consolidate_update_produces_version_snapshots_for_federation():
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = ["canonical", "dupe_a", "dupe_b"]
    conn = _Conn(
        run_row=_run_row(ids),
        memories=[
            _memory("canonical", recall_count=9, created=base),
            _memory("dupe_a", recall_count=1, created=base, permission_mode=640),
            _memory("dupe_b", recall_count=2, created=base, permission_mode=644),
        ],
    )

    await phase_consolidate(_Pool(conn), RUN_ID)

    assert [row["memory_id"] for row in conn.memory_versions] == ["dupe_a", "dupe_b"]
    assert all(row["permission_mode"] == 400 for row in conn.memory_versions)
    assert all("pre_consolidate_permission_mode" in row["metadata"] for row in conn.memory_versions)


def test_version_trigger_covers_consolidate_update_columns():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (repo_root / "db" / "migrations_v3_5_trigger_same_memory_parent.sql").read_text()
    compact = " ".join(sql.split())

    assert "OLD.metadata IS DISTINCT FROM NEW.metadata" in compact
    assert "OLD.permission_mode IS DISTINCT FROM NEW.permission_mode" in compact


@pytest.mark.asyncio
async def test_run_dream_inserts_consolidate_phase_when_enabled(monkeypatch):
    calls: list[str] = []

    async def fake_begin_run(*_args, **_kwargs):
        return RUN_ID

    async def fake_set_phase(_pool, _run_id, phase):
        calls.append(f"phase:{phase}")

    async def fake_phase(_pool, _run_id):
        calls.append("phase_fn")
        return 0

    async def fake_consolidate(_pool, _run_id):
        calls.append("consolidate_fn")
        return 0

    async def fake_finish(_pool, _run_id):
        calls.append("finish")

    monkeypatch.setattr(runner, "begin_run", fake_begin_run)
    monkeypatch.setattr(runner, "set_phase", fake_set_phase)
    monkeypatch.setattr(runner, "phase_replay", fake_phase)
    monkeypatch.setattr(runner, "phase_cluster", fake_phase)
    monkeypatch.setattr(runner, "phase_consolidate", fake_consolidate)
    monkeypatch.setattr(runner, "phase_synthesise", fake_phase)
    monkeypatch.setattr(runner, "finish_run", fake_finish)

    run_id = await runner.run_dream(object(), config={"consolidate": True})

    assert run_id == RUN_ID
    assert calls == [
        "phase:replay",
        "phase_fn",
        "phase:cluster",
        "phase_fn",
        "phase:consolidate",
        "consolidate_fn",
        "phase:synthesise",
        "phase_fn",
        "phase:commit",
        "finish",
    ]

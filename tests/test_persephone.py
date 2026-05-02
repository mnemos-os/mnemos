"""PERSEPHONE archival subsystem tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.domain.persephone import runner
from mnemos.domain.persephone.runner import (
    archive_memory,
    restore_memory,
    sweep_for_archival,
)
from tests._fake_backend import FakePoolBackedBackend, install_fake_backend


NOW = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, memories: list[dict]):
        self.memories = {row["id"]: row for row in memories}
        self.archive: dict[str, dict] = {}
        self.memory_versions: list[dict] = []
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        memory_id = args[0]
        if "JOIN memory_archive" in compact:
            row = self.memories.get(memory_id)
            archived = self.archive.get(memory_id)
            if row is None or archived is None:
                return None
            return {
                "id": memory_id,
                "archived_at": row.get("archived_at"),
                "compressed_content": archived["compressed_content"],
                "compression_algo": archived["compression_algo"],
            }
        if "FROM memories" in compact:
            row = self.memories.get(memory_id)
            if row is None or row.get("deleted_at") is not None:
                return None
            return row
        return None

    async def fetchval(self, sql: str, *args):
        row = self.memories.get(args[0])
        return None if row is None else row.get("archived_at")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "FOR UPDATE SKIP LOCKED" not in compact:
            return []
        namespace, days, batch_size = args
        cutoff = NOW - timedelta(days=days)
        rows = []
        for row in self.memories.values():
            if row.get("deleted_at") is not None:
                continue
            if row.get("archived_at") is not None:
                continue
            if row.get("consolidated_into") is not None:
                continue
            if row.get("namespace") != namespace:
                continue
            last_recalled_at = row.get("last_recalled_at")
            if last_recalled_at is not None and last_recalled_at >= cutoff:
                continue
            if row.get("created") >= cutoff:
                continue
            rows.append({"id": row["id"]})
        return rows[:batch_size]

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("SELECT set_config"):
            return "SELECT 1"
        if compact.startswith("INSERT INTO memory_archive"):
            memory_id, archived_by, compressed, original_size, compressed_size, schema_version = args
            self.archive[memory_id] = {
                "archived_by": archived_by,
                "compressed_content": compressed,
                "compression_algo": "zstd",
                "original_size_bytes": original_size,
                "compressed_size_bytes": compressed_size,
                "schema_version": schema_version,
                "archived_at": NOW,
            }
            return "INSERT 0 1"
        if compact.startswith("UPDATE memories") and "archived_at = NOW()" in compact:
            memory_id, marker = args
            row = self.memories[memory_id]
            row["content"] = marker
            row["verbatim_content"] = None
            row["archived_at"] = NOW
            row["updated"] = NOW
            self.memory_versions.append({"memory_id": memory_id, "content": marker})
            return "UPDATE 1"
        if compact.startswith("UPDATE memories") and "archived_at = NULL" in compact:
            memory_id, content, verbatim_content, metadata_json = args
            row = self.memories[memory_id]
            row["content"] = content
            row["verbatim_content"] = verbatim_content
            row["metadata"] = json.loads(metadata_json)
            row["archived_at"] = None
            row["updated"] = NOW
            self.memory_versions.append({"memory_id": memory_id, "content": content})
            return "UPDATE 1"
        if compact.startswith("DELETE FROM memory_archive"):
            self.archive.pop(args[0], None)
            return "DELETE 1"
        return "OK"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _memory(
    memory_id: str,
    *,
    content: str = "cold memory",
    created_days_ago: int = 90,
    recalled_days_ago: int | None = None,
    namespace: str = "default",
    consolidated_into: str | None = None,
) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "category": "facts",
        "subcategory": None,
        "metadata": {"source": "test"},
        "quality_rating": 75,
        "verbatim_content": content,
        "owner_id": "alice",
        "group_id": None,
        "namespace": namespace,
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "created": NOW - timedelta(days=created_days_ago),
        "updated": NOW - timedelta(days=created_days_ago),
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": consolidated_into,
        "recall_count": 0,
        "last_recalled_at": (
            None if recalled_days_ago is None else NOW - timedelta(days=recalled_days_ago)
        ),
    }


def _root() -> UserContext:
    return UserContext(
        user_id="root", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


def _request():
    return SimpleNamespace(headers={})


@pytest.mark.asyncio
async def test_sweep_recently_recalled_memory_is_not_archived():
    conn = _Conn([_memory("m_recent", recalled_days_ago=20)])

    archived = await sweep_for_archival(
        _Pool(conn),
        namespace="default",
        archive_after_days=30,
        batch_size=10,
    )

    assert archived == 0
    assert conn.memories["m_recent"]["archived_at"] is None
    assert conn.archive == {}


@pytest.mark.asyncio
async def test_sweep_cold_memory_is_archived():
    conn = _Conn([_memory("m_cold", recalled_days_ago=60)])

    archived = await sweep_for_archival(
        _Pool(conn),
        namespace="default",
        archive_after_days=30,
        batch_size=10,
    )

    assert archived == 1
    assert conn.memories["m_cold"]["content"] == "ARCHIVED:m_cold"
    assert conn.memories["m_cold"]["archived_at"] == NOW
    assert "m_cold" in conn.archive


@pytest.mark.asyncio
async def test_archive_then_restore_reproduces_original_content_byte_exactly():
    original = "Launch note:\n- preserve accents: cafe\n- preserve bytes: \\x00-ish text"
    conn = _Conn([_memory("m_restore", content=original, recalled_days_ago=60)])

    await archive_memory(conn, "m_restore", "root")
    await restore_memory(conn, "m_restore", "root")

    assert conn.memories["m_restore"]["content"].encode("utf-8") == original.encode("utf-8")
    assert conn.memories["m_restore"]["archived_at"] is None
    assert "m_restore" not in conn.archive


def _install_stateful_backend(monkeypatch, memories: list[dict]):
    import mnemos.core.lifecycle as lc

    pool = SimpleNamespace(
        state={"memories": {row["id"]: row for row in memories}},
        acquire=lambda: _Acquire(SimpleNamespace()),
    )
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_persistence_backend", FakePoolBackedBackend(pool))
    monkeypatch.setattr(lc, "_cache", None)
    return pool


@pytest.mark.asyncio
async def test_archived_rows_hidden_from_list_and_search_by_default(monkeypatch):
    live = _memory("m_live", content="needle live", recalled_days_ago=1)
    archived = _memory("m_arch", content="ARCHIVED:m_arch", recalled_days_ago=60)
    archived["archived_at"] = NOW
    _install_stateful_backend(monkeypatch, [live, archived])

    listed = await memories_handler.list_memories(limit=20, offset=0, user=_root())
    searched = await memories_handler.search_memories(
        MemorySearchRequest(query="ARCHIVED", limit=10),
        user=_root(),
    )

    assert [m.id for m in listed.memories] == ["m_live"]
    assert searched.memories == []


@pytest.mark.asyncio
async def test_include_archived_returns_archived_markers(monkeypatch):
    archived = _memory("m_arch", content="ARCHIVED:m_arch", recalled_days_ago=60)
    archived["archived_at"] = NOW
    _install_stateful_backend(monkeypatch, [archived])

    listed = await memories_handler.list_memories(
        include_archived=True,
        limit=20,
        offset=0,
        user=_root(),
    )
    searched = await memories_handler.search_memories(
        MemorySearchRequest(query="ARCHIVED", limit=10, include_archived=True),
        user=_root(),
    )

    assert listed.memories[0].id == "m_arch"
    assert listed.memories[0].archived is True
    assert searched.memories[0].content == "ARCHIVED:m_arch"
    assert searched.memories[0].archived is True


@pytest.mark.asyncio
async def test_get_archived_returns_410_with_restore_link(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    row = _memory("m_arch", content="ARCHIVED:m_arch", recalled_days_ago=60)
    row["archived_at"] = NOW
    backend.memories.configure_return("get_memory", row)

    response = await memories_handler.get_memory(
        "m_arch",
        request=_request(),
        user=_root(),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 410
    body = json.loads(response.body)
    assert body == {
        "archived": True,
        "archived_at": NOW.isoformat(),
        "restore_endpoint": "/admin/persephone/restore/m_arch",
    }


@pytest.mark.asyncio
async def test_consolidated_rows_are_not_archived():
    conn = _Conn([
        _memory("m_consolidated", recalled_days_ago=60, consolidated_into="m_canonical"),
    ])

    archived = await sweep_for_archival(
        _Pool(conn),
        namespace="default",
        archive_after_days=30,
        batch_size=10,
    )

    assert archived == 0
    assert conn.archive == {}


@pytest.mark.asyncio
async def test_sweep_is_namespace_isolated():
    conn = _Conn([
        _memory("m_a", recalled_days_ago=60, namespace="tenant-a"),
        _memory("m_b", recalled_days_ago=60, namespace="tenant-b"),
    ])

    archived = await sweep_for_archival(
        _Pool(conn),
        namespace="tenant-a",
        archive_after_days=30,
        batch_size=10,
    )

    assert archived == 1
    assert conn.memories["m_a"]["archived_at"] == NOW
    assert conn.memories["m_b"]["archived_at"] is None


@pytest.mark.asyncio
async def test_archive_records_version_snapshot_marker_for_federation():
    conn = _Conn([_memory("m_version", content="full content", recalled_days_ago=60)])

    await archive_memory(conn, "m_version", "root")

    assert conn.memory_versions[-1] == {
        "memory_id": "m_version",
        "content": "ARCHIVED:m_version",
    }
    assert conn.memories["m_version"]["updated"] == NOW


@pytest.mark.asyncio
async def test_decompress_then_recompress_is_idempotent():
    conn = _Conn([_memory("m_idem", content="idempotent content", recalled_days_ago=60)])

    await archive_memory(conn, "m_idem", "root")
    payload = runner._decompress_payload(conn.archive["m_idem"]["compressed_content"])
    recompressed, _size = runner._compress_payload(payload)
    payload_again = runner._decompress_payload(recompressed)

    assert payload_again == payload

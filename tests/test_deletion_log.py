from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.workers import deletion_request_worker as worker


def _user(role: str) -> UserContext:
    return UserContext(
        user_id=f"{role}-caller",
        group_ids=[],
        role=role,
        namespace="default",
        authenticated=True,
    )


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_args):
        return None


class _DeletionLogConn:
    def __init__(self):
        self.memories = {
            "mem-delete-me": {
                "id": "mem-delete-me",
                "content": "delete me permanently",
                "owner_id": "alice",
                "namespace": "tenant-a",
                "deleted_at": datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            }
        }
        self.deletion_log: list[dict] = []

    async def execute(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SET LOCAL"):
            return "SET"
        if compact.startswith("INSERT INTO deletion_log"):
            target_user_id, target_namespace = args[0], args[1]
            requested_by, requested_at = args[2], args[3]
            request_kind, reason, source = args[4], args[5], args[6]
            inserted = 0
            for row in list(self.memories.values()):
                if row["owner_id"] != target_user_id:
                    continue
                if target_namespace is not None and row["namespace"] != target_namespace:
                    continue
                if row["deleted_at"] is None:
                    continue
                self.deletion_log.append(
                    {
                        "memory_id": row["id"],
                        "content_hash": hashlib.sha256(
                            row["content"].encode("utf-8")
                        ).hexdigest(),
                        "owner_id": row["owner_id"],
                        "namespace": row["namespace"],
                        "requested_by": requested_by,
                        "requested_at": requested_at,
                        "executed_at": datetime.now(timezone.utc),
                        "request_kind": request_kind,
                        "reason": reason,
                        "source": source,
                    }
                )
                inserted += 1
            return f"INSERT 0 {inserted}"
        if "DELETE FROM memories" in compact:
            target_user_id, target_namespace = args[0], args[1]
            deleted = 0
            for memory_id, row in list(self.memories.items()):
                if row["owner_id"] != target_user_id:
                    continue
                if target_namespace is not None and row["namespace"] != target_namespace:
                    continue
                if row["deleted_at"] is None:
                    continue
                self.memories.pop(memory_id)
                deleted += 1
            return f"DELETE {deleted}"
        if compact.startswith("DELETE"):
            return "DELETE 0"
        return "OK"


@pytest.mark.asyncio
async def test_hard_delete_writes_deletion_log_before_destroying_memory(monkeypatch):
    import mnemos.core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_cache", None)
    requested_at = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc)
    conn = _DeletionLogConn()

    counts = await worker.hard_delete_target(
        conn,
        "alice",
        "tenant-a",
        requested_by="root-admin",
        requested_at=requested_at,
        request_kind="tombstone_collected",
        reason="GDPR ticket 42",
        source=["test"],
        invalidate_cache=False,
    )

    assert counts["memories"] == 1
    assert "mem-delete-me" not in conn.memories
    assert len(conn.deletion_log) == 1
    row = conn.deletion_log[0]
    assert row["memory_id"] == "mem-delete-me"
    assert row["content_hash"]
    assert len(row["content_hash"]) == 64
    assert all(ch in "0123456789abcdef" for ch in row["content_hash"])
    assert row["request_kind"] == "tombstone_collected"
    assert row["requested_by"] == "root-admin"
    assert row["requested_at"] == requested_at
    assert row["executed_at"] is not None


@pytest.mark.asyncio
async def test_admin_deletion_log_endpoint_is_root_only(monkeypatch):
    import mnemos.core.lifecycle as lifecycle
    from mnemos.api.main import app

    requested_at = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc)
    executed_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "memory_id": "mem-delete-me",
                "content_hash": "a" * 64,
                "owner_id": "alice",
                "namespace": "tenant-a",
                "requested_by": "root-admin",
                "requested_at": requested_at,
                "executed_at": executed_at,
                "request_kind": "tombstone_collected",
                "reason": "GDPR ticket 42",
                "source": ["test"],
            }
        ]
    )
    pool_manager = MagicMock()
    pool_manager.acquire = MagicMock(return_value=_AsyncContext(conn))
    monkeypatch.setattr(lifecycle, "get_pool_manager", lambda: pool_manager)
    monkeypatch.setattr(lifecycle, "_pool", MagicMock())

    params = {
        "from": "2026-05-01T00:00:00Z",
        "to": "2026-05-02T00:00:00Z",
    }
    transport = ASGITransport(app=app)

    app.dependency_overrides[get_current_user] = lambda: _user("user")
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            forbidden = await client.get("/admin/deletion-log", params=params)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert forbidden.status_code == 403

    app.dependency_overrides[get_current_user] = lambda: _user("root")
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            ok = await client.get("/admin/deletion-log", params=params)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert ok.status_code == 200
    body = ok.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["items"][0]["memory_id"] == "mem-delete-me"


@pytest.mark.asyncio
async def test_mcp_list_deletions_requires_root_context():
    from mnemos.mcp.tools.deletions import tool_list_deletions

    with pytest.raises(PermissionError):
        await tool_list_deletions(
            from_ts="2026-05-01T00:00:00Z",
            to_ts="2026-05-02T00:00:00Z",
            user=_user("user"),
        )

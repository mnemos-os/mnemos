"""DAG log visibility-gap regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import dag as dag_handler


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _version(
    *,
    version_id: str,
    commit_hash: str,
    version_num: int,
    parent_version_id: str | None,
    parent_commit_hash: str | None,
    owner_id: str,
    permission_mode: int,
):
    return {
        "id": version_id,
        "commit_hash": commit_hash,
        "parent_version_id": parent_version_id,
        "parent_commit_hash": parent_commit_hash,
        "version_num": version_num,
        "branch": "main",
        "content": f"content {version_num}",
        "category": "solutions",
        "subcategory": None,
        "snapshot_at": datetime(2026, 1, version_num, 12, 0, 0),
        "snapshot_by": owner_id,
        "change_type": "create" if parent_version_id is None else "update",
        "owner_id": owner_id,
        "namespace": "alice-ns",
        "permission_mode": permission_mode,
    }


class _Conn:
    def __init__(self):
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        if "WITH RECURSIVE commit_walk AS" not in compact:
            raise AssertionError(f"unexpected fetch SQL: {sql}")

        return [
            _version(
                version_id="v3",
                commit_hash="v3-hash",
                version_num=3,
                parent_version_id="v2",
                parent_commit_hash="v2-hash",
                owner_id="alice",
                permission_mode=600,
            ),
            _version(
                version_id="v2",
                commit_hash="v2-hash",
                version_num=2,
                parent_version_id="v1",
                parent_commit_hash="v1-hash",
                owner_id="mallory",
                permission_mode=600,
            ),
            _version(
                version_id="v1",
                commit_hash="v1-hash",
                version_num=1,
                parent_version_id=None,
                parent_commit_hash=None,
                owner_id="alice",
                permission_mode=600,
            ),
        ]


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def test_get_memory_log_does_not_bridge_invisible_parent(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    commits = asyncio.run(
        dag_handler.get_memory_log("memory-a", branch="main", user=_alice())
    )

    assert [c.commit_hash for c in commits] == ["v3-hash", "v1-hash"]
    assert commits[0].parent_hash is None
    assert commits[1].parent_hash is None
    assert "v2-hash" not in {c.commit_hash for c in commits}

    log_sql = conn.fetch_calls[0][0]
    assert "parent_version_id" in log_sql
    assert "parent_mv.commit_hash AS parent_commit_hash" in log_sql

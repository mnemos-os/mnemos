"""Memory create and webhook outbox rows share one transaction."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories
from mnemos.domain.models import MemoryCreateRequest

pytestmark = pytest.mark.asyncio


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


class _Txn:
    def __init__(self, conn: "_OutboxConn"):
        self.conn = conn

    async def __aenter__(self):
        self.conn._begin()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn._commit()
        else:
            self.conn._rollback()
        return False


class _OutboxConn:
    def __init__(self, *, fail_delivery_insert: bool = False):
        self.fail_delivery_insert = fail_delivery_insert
        self.memories: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []
        self._staged_memories: list[dict[str, Any]] | None = None
        self._staged_deliveries: list[dict[str, Any]] | None = None
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return _Txn(self)

    def _begin(self) -> None:
        if self._staged_memories is not None:
            raise AssertionError("nested transaction not expected in this test")
        self._staged_memories = []
        self._staged_deliveries = []

    def _commit(self) -> None:
        self.memories.extend(self._staged_memories or [])
        self.deliveries.extend(self._staged_deliveries or [])
        self._staged_memories = None
        self._staged_deliveries = None
        self.commits += 1

    def _rollback(self) -> None:
        self._staged_memories = None
        self._staged_deliveries = None
        self.rollbacks += 1

    async def execute(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO memories "):
            row = {
                "id": args[0],
                "content": args[1],
                "category": args[2],
                "subcategory": args[3],
                "metadata": args[4],
                "quality_rating": 75,
                "verbatim_content": args[5],
                "owner_id": args[6],
                "group_id": None,
                "namespace": args[7],
                "permission_mode": args[8],
                "source_model": args[9],
                "source_provider": args[10],
                "source_session": args[11],
                "source_agent": args[12],
                "compressed_content": None,
                "created": datetime.now(timezone.utc),
                "updated": datetime.now(timezone.utc),
            }
            target = self._staged_memories if self._staged_memories is not None else self.memories
            target.append(row)
            return "INSERT 0 1"
        return "OK"

    async def fetch(self, sql: str, *args):
        if "FROM webhook_subscriptions" in sql:
            return [
                {
                    "id": "sub_1",
                    "url": "https://example.test/hook",
                    "events": ["memory.created"],
                    "secret": "secret",
                    "owner_id": "alice",
                    "namespace": "alice-ns",
                }
            ]
        return []

    async def fetchrow(self, sql: str, *args):
        if "FROM memories WHERE id=$1" in sql:
            memory_id = args[0]
            rows = list(self.memories)
            if self._staged_memories is not None:
                rows.extend(self._staged_memories)
            return next(row for row in rows if row["id"] == memory_id)
        return None

    async def fetchval(self, sql: str, *args):
        if "INSERT INTO webhook_deliveries" in sql:
            if self.fail_delivery_insert:
                raise RuntimeError("delivery insert failed")
            delivery_id = f"delivery_{len(self.deliveries) + 1}"
            row = {
                "id": delivery_id,
                "subscription_id": args[0],
                "event_type": args[1],
                "payload": args[2],
                "payload_hash": args[3],
                "writer_revision": args[4],
            }
            target = (
                self._staged_deliveries
                if self._staged_deliveries is not None
                else self.deliveries
            )
            target.append(row)
            return delivery_id
        return None


class _Acquire:
    def __init__(self, conn: _OutboxConn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn: _OutboxConn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _install(monkeypatch: pytest.MonkeyPatch, conn: _OutboxConn) -> None:
    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(memories._lc, "_pool", _Pool(conn))
    monkeypatch.setattr(memories._lc, "_cache", None)
    monkeypatch.setattr(memories._lc, "_rls_enabled", False)
    monkeypatch.setattr(lc, "_schedule_delivery_attempt", lambda coro: coro.close())


async def test_memory_create_commits_memory_and_webhook_delivery(monkeypatch):
    conn = _OutboxConn()
    _install(monkeypatch, conn)

    response = await memories.create_memory(
        MemoryCreateRequest(content="remember this", category="facts"),
        user=_user(),
    )

    assert response.id == conn.memories[0]["id"]
    assert len(conn.memories) == 1
    assert len(conn.deliveries) == 1
    assert conn.deliveries[0]["event_type"] == "memory.created"
    assert conn.commits == 1
    assert conn.rollbacks == 0


async def test_webhook_delivery_failure_rolls_back_memory_insert(monkeypatch):
    conn = _OutboxConn(fail_delivery_insert=True)
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        await memories.create_memory(
            MemoryCreateRequest(content="remember this", category="facts"),
            user=_user(),
        )

    assert exc.value.status_code == 500
    assert conn.memories == []
    assert conn.deliveries == []
    assert conn.commits == 0
    assert conn.rollbacks == 1

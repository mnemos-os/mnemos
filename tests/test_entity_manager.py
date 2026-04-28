from __future__ import annotations

import pytest

from api.auth import UserContext
from modules.memory_categorization.entities import EntityManager

pytestmark = pytest.mark.asyncio


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


class _Acquire:
    def __init__(self, conn: "_Conn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn: "_Conn"):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, returned_id: str):
        self.returned_id = returned_id
        self.inserted_id: str | None = None

    async def fetchrow(self, sql: str, *args):
        self.inserted_id = args[0]
        assert "ON CONFLICT" in sql
        assert "RETURNING id::text" in sql
        return {"id": self.returned_id}


async def test_entity_manager_create_entity_returns_existing_id_on_conflict():
    existing_id = "11111111-1111-1111-1111-111111111111"
    conn = _Conn(returned_id=existing_id)
    manager = EntityManager(_Pool(conn))

    entity_id = await manager.create_entity(
        "person",
        "Alice",
        description="updated",
        user=_user(),
    )

    assert entity_id == existing_id
    assert conn.inserted_id is not None
    assert conn.inserted_id != existing_id

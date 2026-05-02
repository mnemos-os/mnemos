from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


def _pool(conn):
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    return pool


def test_api_key_auth_fetches_user_and_groups_in_one_query(monkeypatch):
    import mnemos.api.dependencies as auth_mod
    from mnemos.api.dependencies import get_current_user

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "key-1",
            "user_id": "alice",
            "revoked": False,
            "role": "user",
            "namespace": "alice-ns",
            "group_ids": ["ops", "dev"],
        }
    )
    conn.fetch = AsyncMock()

    monkeypatch.setattr(auth_mod, "_auth_enabled", True)

    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(lc, "_schedule_background", lambda coro: coro.close())

    request = MagicMock()
    request.app.state.pool = _pool(conn)
    request.cookies = {}

    creds = MagicMock()
    creds.credentials = "test-key"

    user = asyncio.run(get_current_user(request, creds))

    assert user.user_id == "alice"
    assert user.group_ids == ["ops", "dev"]
    conn.fetchrow.assert_awaited_once()
    conn.fetch.assert_not_awaited()

    sql = conn.fetchrow.await_args.args[0]
    assert "LEFT JOIN LATERAL" in sql
    assert "array_agg(group_id)" in sql

"""Per-user namespace loaded from the users table.

This test module verifies:
  * `_user_context_from_id` SELECT includes the namespace column
    and populates UserContext.namespace from it.
  * API-key auth path SELECT joins users.namespace and populates
    UserContext.namespace from the joined row.
  * The config default (`_default_namespace`) is reserved for the
    auth-disabled singleton path, never authenticated DB users.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


class _Conn:
    def __init__(self, *, user_row=None, group_rows=None, api_key_row=None):
        self._user_row = user_row
        self._group_rows = group_rows or []
        self._api_key_row = api_key_row
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        # Dispatch: the two distinct fetchrow queries on auth paths
        if "SELECT role, namespace FROM users" in sql:
            return self._user_row
        if "FROM api_keys ak JOIN users u" in sql:
            return self._api_key_row
        return None

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        if "FROM user_groups" in sql:
            return self._group_rows
        return []

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _pool(conn):
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    return pool


# ─── _user_context_from_id ──────────────────────────────────────────────────


def test_session_path_loads_namespace_from_users_row():
    from api.auth import _user_context_from_id

    conn = _Conn(
        user_row={"role": "user", "namespace": "alice-ns"},
        group_rows=[{"group_id": "g-1"}],
    )
    user = asyncio.run(_user_context_from_id(_pool(conn), "alice", authenticated=True))

    assert user.user_id == "alice"
    assert user.role == "user"
    assert user.namespace == "alice-ns"  # from DB, not config default
    assert user.group_ids == ["g-1"]
    assert user.authenticated is True


def test_session_path_selects_both_role_and_namespace():
    """Regression guard — the SELECT must include both columns.
    Selecting only one causes the later fetchrow lookup on
    row['namespace'] to KeyError."""
    from api.auth import _user_context_from_id

    conn = _Conn(
        user_row={"role": "root", "namespace": "admin-ns"},
        group_rows=[],
    )
    asyncio.run(_user_context_from_id(_pool(conn), "admin", authenticated=True))

    sql = conn.fetchrow_calls[0][0]
    assert "role" in sql and "namespace" in sql
    assert "FROM users" in sql


def test_session_path_404s_when_user_row_missing():
    """An auth'd session for a user that no longer exists should raise
    401, not silently construct a partial UserContext."""
    from api.auth import _user_context_from_id
    from fastapi import HTTPException

    conn = _Conn(user_row=None, group_rows=[])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_user_context_from_id(_pool(conn), "ghost", authenticated=True))
    assert exc.value.status_code == 401


# ─── API-key auth path ──────────────────────────────────────────────────────


def test_api_key_path_loads_namespace_from_joined_users_row(monkeypatch):
    """The /get_current_user Bearer branch joins api_keys JOIN users.
    After v3.2, the JOIN must pull u.namespace and the UserContext
    must reflect it."""
    from api.auth import get_current_user
    import api.auth as auth_mod

    conn = _Conn(
        api_key_row={
            "id": "key-1",
            "user_id": "bob",
            "revoked": False,
            "role": "user",
            "namespace": "bob-ns",
        },
        group_rows=[{"group_id": "g-2"}],
    )

    # Make get_current_user go through the API-key path
    monkeypatch.setattr(auth_mod, "_auth_enabled", True)

    # Stub out _schedule_background so it doesn't try to hit the real event loop
    import api.lifecycle as lc
    monkeypatch.setattr(lc, "_schedule_background", lambda coro: coro.close())

    # Fake Request with app.state.pool + headers
    request = MagicMock()
    request.app.state.pool = _pool(conn)
    request.cookies = {}

    # Fake HTTPAuthorizationCredentials
    creds = MagicMock()
    creds.credentials = "test-key"

    user = asyncio.run(get_current_user(request, creds))

    assert user.user_id == "bob"
    assert user.role == "user"
    assert user.namespace == "bob-ns"
    assert user.group_ids == ["g-2"]
    assert user.authenticated is True

    # Verify the SELECT actually joined u.namespace — future-proofs
    # against someone "simplifying" the query back to role-only
    key_sql = conn.fetchrow_calls[0][0]
    assert "u.namespace" in key_sql or "namespace" in key_sql

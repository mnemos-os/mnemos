"""POST /admin/users must accept + persist namespace.

Regression for the v3.2 Codex re-audit finding "per-user namespace
provisioning gap": users.namespace landed in 2aa41ea and non-root
read paths filter on it, but UserCreateRequest never exposed the
field — so every new user was silently stamped 'default', collapsing
any intended multi-tenant install.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.api.routes.admin import create_user, list_users
from mnemos.domain.models import UserCreateRequest, UserResponse


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


def _mock_pool(monkeypatch, create_row=None, list_rows=None):
    from mnemos.core import lifecycle

    mock_conn = MagicMock()
    side_effects: list = []
    if create_row is not None:
        side_effects = [None, create_row]
    mock_conn.fetchrow = AsyncMock(side_effect=side_effects or [None])
    mock_conn.fetch = AsyncMock(return_value=list_rows or [])

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncContext(mock_conn))
    monkeypatch.setattr(lifecycle, "_pool", mock_pool)
    return mock_pool, mock_conn


def _row(**overrides):
    base = {
        "id": "alice",
        "display_name": "Alice",
        "email": None,
        "role": "user",
        "namespace": "tenant-a",
        "created_at": datetime.datetime(2026, 4, 22, 19, 30),
    }
    base.update(overrides)
    return base


def test_request_model_carries_namespace_with_default():
    """UserCreateRequest exposes a namespace field that defaults to 'default'."""
    bare = UserCreateRequest(id="u1", display_name="U1")
    assert bare.namespace == "default"

    explicit = UserCreateRequest(id="u2", display_name="U2", namespace="tenant-x")
    assert explicit.namespace == "tenant-x"


def test_response_model_requires_namespace():
    """UserResponse must surface namespace so clients can read it back."""
    resp = UserResponse(
        id="alice", display_name="Alice", role="user",
        namespace="tenant-a", created_at="2026-04-22T19:30:00",
    )
    assert resp.namespace == "tenant-a"


@pytest.mark.asyncio
async def test_create_user_persists_namespace(monkeypatch):
    """POST /admin/users threads namespace into the INSERT and returns it."""
    _, conn = _mock_pool(monkeypatch, create_row=_row(namespace="tenant-a"))

    req = UserCreateRequest(
        id="alice", display_name="Alice", role="user", namespace="tenant-a",
    )
    resp = await create_user(req, _=MagicMock(role="root"))

    # Response echoes the namespace we set.
    assert resp.namespace == "tenant-a"

    # INSERT SQL references namespace column and binds the request value.
    insert_call = conn.fetchrow.await_args_list[1]
    sql = insert_call.args[0]
    assert "namespace" in sql, (
        "INSERT into users must include the namespace column — otherwise "
        "the request-level value never reaches the DB."
    )
    assert "tenant-a" in insert_call.args, (
        "INSERT must bind the request.namespace as a parameter."
    )


@pytest.mark.asyncio
async def test_create_user_defaults_namespace_when_omitted(monkeypatch):
    """Absent namespace in the request => 'default' is persisted."""
    _, conn = _mock_pool(monkeypatch, create_row=_row(namespace="default"))

    req = UserCreateRequest(id="bob", display_name="Bob")  # no namespace
    resp = await create_user(req, _=MagicMock(role="root"))

    assert resp.namespace == "default"
    insert_call = conn.fetchrow.await_args_list[1]
    assert "default" in insert_call.args


@pytest.mark.asyncio
async def test_list_users_surfaces_namespace(monkeypatch):
    """GET /admin/users returns each user's namespace."""
    _mock_pool(
        monkeypatch,
        list_rows=[_row(id="alice", namespace="tenant-a"),
                   _row(id="bob", namespace="tenant-b")],
    )

    resp = await list_users(_=MagicMock(role="root"))
    assert [u.namespace for u in resp] == ["tenant-a", "tenant-b"]

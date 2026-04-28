from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.core.security import (
    assert_owned,
    assert_owner_match,
    is_root,
    scope_namespace,
    scope_owner,
)


def _user(
    *,
    user_id: str = "alice",
    role: str = "user",
    namespace: str = "alice-ns",
) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=namespace,
        authenticated=True,
    )


def _root() -> UserContext:
    return _user(user_id="admin", role="root", namespace="root-ns")


class _Conn:
    def __init__(self, row=None):
        self.row = row
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self.row


def test_is_root_for_root_vs_non_root():
    assert is_root(_root()) is True
    assert is_root(_user()) is False


def test_scope_owner_root_override():
    assert scope_owner(_root(), "bob") == "bob"


def test_scope_owner_non_root_self():
    assert scope_owner(_user(user_id="alice"), "alice") == "alice"


def test_scope_owner_non_root_cross_raises_403():
    with pytest.raises(HTTPException) as exc:
        scope_owner(_user(user_id="alice"), "bob")
    assert exc.value.status_code == 403


def test_scope_namespace_root_override():
    assert scope_namespace(_root(), "bob-ns") == "bob-ns"


def test_scope_namespace_non_root_self():
    assert scope_namespace(_user(namespace="alice-ns"), "alice-ns") == "alice-ns"


def test_scope_namespace_non_root_cross_raises_403():
    with pytest.raises(HTTPException) as exc:
        scope_namespace(_user(namespace="alice-ns"), "bob-ns")
    assert exc.value.status_code == 403


def test_assert_owned_missing_raises_404():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(assert_owned(_Conn(row=None), "memories", "mem_missing", _user(), id_cast="text"))
    assert exc.value.status_code == 404


def test_assert_owned_non_root_cross_raises_404():
    conn = _Conn(row={"owner_id": "bob", "namespace": "bob-ns"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(assert_owned(conn, "memories", "mem_bob", _user(), id_cast="text"))
    assert exc.value.status_code == 404


def test_assert_owned_root_cross_returns_owner_id():
    conn = _Conn(row={"owner_id": "bob", "namespace": "bob-ns"})
    assert asyncio.run(assert_owned(conn, "memories", "mem_bob", _root(), id_cast="text")) == "bob"


def test_assert_owner_match_allows_match():
    assert_owner_match("alice", _user(user_id="alice"))


def test_assert_owner_match_cross_raises_403():
    with pytest.raises(HTTPException) as exc:
        assert_owner_match("bob", _user(user_id="alice"))
    assert exc.value.status_code == 403

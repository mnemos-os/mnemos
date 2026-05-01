"""Tests for the shared API-side persistence helpers.

Specifically pins the SQL shape of ``maybe_set_pg_rls`` so it
cannot regress to the broken ``SET LOCAL ... = $1`` form codex
round-4 (review-momrxh8q-gq0b11) caught — Postgres ``SET LOCAL``
does not accept bind parameters, so the previous shape would
500 every authenticated request on RLS-enabled Postgres. The
parameterizable form is ``SELECT set_config(name, value, true)``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.persistence_helpers import maybe_set_pg_rls


def _user(authenticated: bool = True, user_id: str = "alice", role: str = "user") -> UserContext:
    return UserContext(
        user_id=user_id, group_ids=[], role=role,
        namespace="default", authenticated=authenticated,
    )


def _fake_postgres_tx():
    """A lookalike for mnemos.persistence.postgres.PostgresTransaction.

    The real class exposes ``conn`` via @property; we set the
    backing ``_conn`` slot directly on a bypass-init instance so
    the isinstance check in ``maybe_set_pg_rls`` passes and the
    helper sees an awaitable execute() that records calls.
    """
    from mnemos.persistence.postgres import PostgresTransaction

    conn = SimpleNamespace()
    conn.execute = AsyncMock()
    tx = PostgresTransaction.__new__(PostgresTransaction)
    tx._conn = conn
    tx._tx = None
    tx._closed = False
    return tx, conn


@pytest.mark.asyncio
async def test_helper_no_op_when_rls_disabled(monkeypatch):
    monkeypatch.setattr(_lc, "_rls_enabled", False)
    tx, conn = _fake_postgres_tx()
    await maybe_set_pg_rls(tx, _user())
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_helper_no_op_when_user_not_authenticated(monkeypatch):
    monkeypatch.setattr(_lc, "_rls_enabled", True)
    tx, conn = _fake_postgres_tx()
    await maybe_set_pg_rls(tx, _user(authenticated=False))
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_helper_no_op_on_non_postgres_tx(monkeypatch):
    monkeypatch.setattr(_lc, "_rls_enabled", True)
    tx = SimpleNamespace(conn=SimpleNamespace(execute=AsyncMock()))
    await maybe_set_pg_rls(tx, _user())
    assert tx.conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_helper_uses_set_config_not_set_local(monkeypatch):
    """The SQL must be parameterizable.

    Postgres rejects bind parameters in ``SET LOCAL``; the helper
    must use ``set_config`` so asyncpg can pass the user_id / role
    as $1. This test pins the exact statement shape and ordering.
    """
    monkeypatch.setattr(_lc, "_rls_enabled", True)
    tx, conn = _fake_postgres_tx()
    await maybe_set_pg_rls(tx, _user(user_id="alice", role="user"))

    assert conn.execute.await_count == 2, (
        "Expected exactly two execute() calls — one per RLS GUC"
    )
    calls = conn.execute.await_args_list

    # Each call passes (sql, value) positionally.
    sql0, val0 = calls[0].args
    sql1, val1 = calls[1].args

    # Critical: the SQL is set_config(...), not SET LOCAL ... = $1.
    # SET LOCAL with $1 raises a syntax error on real Postgres.
    assert "set_config" in sql0, f"first GUC must use set_config, got: {sql0!r}"
    assert "set_config" in sql1, f"second GUC must use set_config, got: {sql1!r}"
    assert "SET LOCAL" not in sql0
    assert "SET LOCAL" not in sql1

    # The three positional args of set_config: name (literal),
    # value ($1, parameterised), is_local (true).
    assert "mnemos.current_user_id" in sql0
    assert "mnemos.current_role" in sql1
    assert "$1" in sql0 and "true" in sql0
    assert "$1" in sql1 and "true" in sql1

    # The bound values match the user's identity.
    assert val0 == "alice"
    assert val1 == "user"


@pytest.mark.asyncio
async def test_helper_orders_user_id_before_role(monkeypatch):
    """Doesn't matter functionally but pin the order so test
    failures point at the right call."""
    monkeypatch.setattr(_lc, "_rls_enabled", True)
    tx, conn = _fake_postgres_tx()
    await maybe_set_pg_rls(tx, _user(user_id="alice", role="custom-role"))

    sql0, _ = conn.execute.await_args_list[0].args
    sql1, _ = conn.execute.await_args_list[1].args
    assert "mnemos.current_user_id" in sql0
    assert "mnemos.current_role" in sql1

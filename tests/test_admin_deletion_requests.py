"""Admin deletion-request endpoints (GDPR right-to-be-forgotten
scaffold from v4.2.0a14 round-77).

Endpoint surface tested:

  POST   /admin/deletion-requests              create
  GET    /admin/deletion-requests              list
  GET    /admin/deletion-requests/{id}         single
  POST   /admin/deletion-requests/{id}/confirm transition requested → confirmed
  POST   /admin/deletion-requests/{id}/cancel  transition requested|confirmed → cancelled
  POST   /admin/deletion-requests/{id}/restore reverse soft-delete in grace window
  POST   /admin/deletion-requests/{id}/force-purge hard-delete operator override

The actual wipe worker that consumes confirmed rows is covered in
test_deletion_request_worker.py; this file covers the admin
lifecycle and restore route.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes.admin import (
    cancel_deletion_request,
    confirm_deletion_request,
    create_deletion_request,
    force_purge_deletion_request,
    get_deletion_request,
    list_deletion_requests,
    restore_deletion_request,
)
from mnemos.domain.models import DeletionRequestCreate


def _root_user() -> UserContext:
    return UserContext(
        user_id="root-admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


class _TxCtx:
    """Minimal async-context-manager stub for
    ``conn.transaction()``. The CREATE path entered a nested
    transaction in round-78 to take the advisory lock + run the
    overlap SELECT before the INSERT atomically; tests use this
    stub so the route's ``async with conn.transaction():`` works.
    """

    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return None


def _wire_pool(monkeypatch, mock_conn):
    """Make ``_lc.get_pool_manager().acquire()`` yield ``mock_conn``
    AND set ``_lc._pool`` so the route's profile-aware 503 helper
    passes through. Also stubs ``mock_conn.transaction()`` for
    the CREATE path's advisory-lock + overlap-SELECT block.
    """
    import mnemos.core.lifecycle as lc

    pool_manager = MagicMock()
    pool_manager.acquire = MagicMock(return_value=_AsyncContext(mock_conn))
    pool_manager.transactional = MagicMock(return_value=_AsyncContext(mock_conn))
    monkeypatch.setattr(lc, "get_pool_manager", lambda: pool_manager)
    monkeypatch.setattr(lc, "_pool", MagicMock())
    # Default transaction stub (clean exit).
    if not hasattr(mock_conn, "transaction") or not callable(getattr(mock_conn, "transaction", None)):
        pass
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    return pool_manager


def _row(
    *,
    id="00000000-0000-0000-0000-000000000001",
    target_user_id="alice",
    target_namespace=None,
    requested_by="root-admin",
    status="requested",
    confirmed_at=None,
    soft_deleted_at=None,
    restore_by=None,
    restored_at=None,
    hard_deleted_at=None,
    notes=None,
):
    """Construct a dict mimicking an asyncpg Record for
    deletion_requests."""
    now = datetime(2026, 5, 1, 22, 0, 0, tzinfo=timezone.utc)
    return {
        "id": id,
        "target_user_id": target_user_id,
        "target_namespace": target_namespace,
        "requested_by": requested_by,
        "requested_at": now,
        "confirmed_at": confirmed_at,
        "soft_deleted_at": soft_deleted_at,
        "restore_by": restore_by,
        "restored_at": restored_at,
        "hard_deleted_at": hard_deleted_at,
        "status": status,
        "notes": notes,
    }


# ── POST /admin/deletion-requests ─────────────────────────────


def _create_fetchrow_chain(*, overlap=None, inserted=None):
    """Build a fetchrow side-effect for the round-78 CREATE flow.

    Order of fetchrow calls inside the transaction:

      1. Overlap SELECT — returns ``overlap`` (None for "no
         existing active request covers this scope", or a row
         dict to simulate a 409).
      2. INSERT ... RETURNING * — returns ``inserted``.

    ``execute`` (the pg_advisory_xact_lock call) is a no-op via
    AsyncMock's default behavior.
    """
    calls = []
    if overlap is None and inserted is None:
        return AsyncMock(return_value=None)

    async def _side_effect(sql, *args):
        calls.append(sql)
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("SELECT") and "FROM DELETION_REQUESTS" in sql_upper:
            return overlap
        if sql_upper.startswith("INSERT") and "INTO DELETION_REQUESTS" in sql_upper:
            return inserted
        return None

    return AsyncMock(side_effect=_side_effect)


@pytest.mark.asyncio
async def test_create_deletion_request_persists_and_returns_id(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = _create_fetchrow_chain(
        overlap=None,
        inserted=_row(notes="GDPR ticket #42"),
    )
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(
        target_user_id="alice",
        notes="GDPR ticket #42",
    )
    result = await create_deletion_request(request=body, user=_root_user())

    assert result.id == "00000000-0000-0000-0000-000000000001"
    assert result.target_user_id == "alice"
    assert result.target_namespace is None
    assert result.requested_by == "root-admin"
    assert result.status == "requested"
    assert result.notes == "GDPR ticket #42"
    # Sanity: requested_at is a parseable ISO timestamp.
    datetime.fromisoformat(result.requested_at)


@pytest.mark.asyncio
async def test_create_deletion_request_rejects_empty_target_user_id(monkeypatch):
    mock_conn = AsyncMock()
    _wire_pool(monkeypatch, mock_conn)
    body = DeletionRequestCreate(target_user_id="", notes="oops")
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 422
    assert "target_user_id" in exc.value.detail


@pytest.mark.asyncio
async def test_create_deletion_request_rejects_whitespace_only_target_user_id(monkeypatch):
    """Round-78 closure of codex review-1 finding #2: pre-fix
    a whitespace-only ``target_user_id`` (e.g., ``"   "``)
    bypassed the falsy check and persisted as an identifier
    that no row would ever match. Must 422."""
    mock_conn = AsyncMock()
    _wire_pool(monkeypatch, mock_conn)
    body = DeletionRequestCreate(target_user_id="   ", notes="ws")
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_deletion_request_normalizes_blank_namespace_to_null(monkeypatch):
    """Round-78: an empty-string ``target_namespace`` collapses
    the all-namespaces semantics with a real-but-empty
    namespace identifier. The helper must normalize to None
    before inserting."""
    captured = {}

    async def _capture(sql, *args):
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("INSERT") and "INTO DELETION_REQUESTS" in sql_upper:
            captured["target_user_id"] = args[0]
            captured["target_namespace"] = args[1]
            return _row()
        # Overlap SELECT returns no row.
        return None

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=_capture)
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(
        target_user_id="alice",
        target_namespace="   ",
    )
    await create_deletion_request(request=body, user=_root_user())
    assert captured["target_namespace"] is None, (
        f"empty/whitespace target_namespace must normalize to "
        f"None; got {captured['target_namespace']!r}"
    )


@pytest.mark.asyncio
async def test_create_deletion_request_rejects_sentinel_namespace(monkeypatch):
    """Round-78: ``target_namespace='*'`` is the COALESCE
    sentinel for the active-row unique index. An explicit '*'
    must 422 so a request can't masquerade as the all-namespaces
    scope."""
    mock_conn = AsyncMock()
    _wire_pool(monkeypatch, mock_conn)
    body = DeletionRequestCreate(
        target_user_id="alice",
        target_namespace="*",
    )
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 422
    assert "*" in exc.value.detail


@pytest.mark.asyncio
async def test_create_deletion_request_returns_409_on_overlap_select(monkeypatch):
    """Round-78 closure of codex review-1 finding #1: the
    SELECT guard inside the transaction catches NULL-vs-
    specific containment overlap that the partial-unique
    index alone misses. (alice, NULL) and (alice, 'tenant-a')
    are distinct keys per COALESCE-to-'*' encoding, but they
    cover overlapping scopes and one already-active request
    must block the other.
    """
    mock_conn = AsyncMock()
    mock_conn.fetchrow = _create_fetchrow_chain(
        overlap={
            "id": "11111111-1111-1111-1111-111111111111",
            "target_namespace": None,
            "status": "confirmed",
        },
        inserted=None,  # never reached
    )
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(
        target_user_id="alice",
        target_namespace="tenant-a",
    )
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 409
    assert "11111111-1111-1111-1111-111111111111" in exc.value.detail
    assert "confirmed" in exc.value.detail


@pytest.mark.asyncio
async def test_create_deletion_request_409_on_unique_index_race(monkeypatch):
    """Defense-in-depth: even if the SELECT guard misses a
    concurrent INSERT (race between two CREATE calls past the
    advisory lock), the partial unique index catches the
    exact-pair duplicate. Surface as 409, not 500."""
    async def _side_effect(sql, *args):
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("SELECT") and "FROM DELETION_REQUESTS" in sql_upper:
            return None  # overlap check passes
        if sql_upper.startswith("INSERT"):
            raise asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint "
                "\"deletion_requests_active_unique_idx\""
            )
        return None

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=_side_effect)
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(target_user_id="alice")
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 409
    assert "alice" in exc.value.detail


@pytest.mark.asyncio
async def test_create_deletion_request_409_on_legacy_blank_namespace_overlap(monkeypatch):
    """Round-79: a round-77 alpha row with
    ``target_namespace=''`` (legacy data, should be NULL but
    isn't because round-77 didn't validate) must STILL block a
    new request for the same user with a specific namespace.
    The round-78 guard's ``NULLIF(BTRIM(target_namespace),
    '')`` normalization treats the legacy blank as the
    all-namespaces scope so the containment-overlap check
    catches it.

    Codex review-2 of round-78 caught the version-skew gap;
    this test pins the fix.
    """
    captured_select_sqls = []

    async def _side_effect(sql, *args):
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("SELECT") and "FROM DELETION_REQUESTS" in sql_upper:
            captured_select_sqls.append(sql)
            # Simulate Postgres applying NULLIF(BTRIM(...))
            # to the legacy blank-namespace row in WHERE: the
            # row matches the all-namespaces predicate.
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "target_namespace": "",  # legacy blank
                "status": "confirmed",
            }
        if sql_upper.startswith("INSERT"):
            return _row()
        return None

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=_side_effect)
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(
        target_user_id="alice",
        target_namespace="tenant-a",
    )
    with pytest.raises(HTTPException) as exc:
        await create_deletion_request(request=body, user=_root_user())
    assert exc.value.status_code == 409
    # The 409 detail must include the existing request's id +
    # status so operators can find / progress / cancel it.
    assert "11111111-1111-1111-1111-111111111111" in exc.value.detail

    # Sanity: the SELECT must use the ``mnemos_is_blank
    # _namespace`` helper function so future schema/code
    # changes that drop the Unicode-whitespace normalization
    # are caught here. Round-81 replaced the round-79 BTRIM
    # form (codex review-3: trims ASCII whitespace only) and
    # the round-80 POSIX ``[[:space:]]`` form (codex
    # review-4: doesn't match Python's Unicode-whitespace
    # ``.strip()`` semantics) with a SQL helper function
    # that enumerates the full Python ``str.isspace()`` set.
    assert captured_select_sqls, "no SELECT query observed"
    select_sql = captured_select_sqls[0]
    assert "mnemos_is_blank_namespace" in select_sql, (
        f"overlap SELECT must use mnemos_is_blank_namespace() "
        f"helper for Unicode-aware whitespace normalization "
        f"(matches Python str.strip() Unicode semantics); "
        f"got SQL:\n{select_sql}"
    )


@pytest.mark.asyncio
async def test_create_deletion_request_takes_advisory_lock(monkeypatch):
    """Round-78: the helper must ``SELECT pg_advisory_xact_lock(...)``
    before the SELECT-guard so concurrent CREATEs for the same
    user can't both pass the overlap check.
    """
    captured_executes = []

    async def _execute(sql, *args):
        captured_executes.append(sql)
        return None

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=_execute)
    mock_conn.fetchrow = _create_fetchrow_chain(
        overlap=None, inserted=_row(),
    )
    _wire_pool(monkeypatch, mock_conn)

    body = DeletionRequestCreate(target_user_id="alice")
    await create_deletion_request(request=body, user=_root_user())
    assert any(
        "PG_ADVISORY_XACT_LOCK" in sql.upper() for sql in captured_executes
    ), (
        f"create_deletion_request must take an advisory lock; "
        f"executed SQLs: {captured_executes!r}"
    )


# ── GET /admin/deletion-requests ──────────────────────────────


@pytest.mark.asyncio
async def test_list_deletion_requests_no_filters(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        return_value=[
            _row(id="r-1", target_user_id="alice"),
            _row(id="r-2", target_user_id="bob", status="confirmed"),
        ]
    )
    _wire_pool(monkeypatch, mock_conn)

    result = await list_deletion_requests(_=_root_user())
    assert result.count == 2
    assert {r.id for r in result.requests} == {"r-1", "r-2"}

    # Verify the SQL-shape: no WHERE clause, ORDER BY DESC
    call_args = mock_conn.fetch.await_args
    sql = call_args.args[0]
    assert "WHERE" not in sql.upper()
    assert "ORDER BY REQUESTED_AT DESC" in sql.upper()


@pytest.mark.asyncio
async def test_list_deletion_requests_filters_by_status_and_user(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    _wire_pool(monkeypatch, mock_conn)

    await list_deletion_requests(
        _=_root_user(),
        status="confirmed",
        target_user_id="alice",
        limit=50,
    )
    call_args = mock_conn.fetch.await_args
    sql = call_args.args[0]
    args = call_args.args[1:]
    assert "WHERE" in sql.upper()
    assert "STATUS =" in sql.upper()
    assert "TARGET_USER_ID =" in sql.upper()
    assert "confirmed" in args
    assert "alice" in args
    assert 50 in args


@pytest.mark.asyncio
async def test_list_deletion_requests_rejects_invalid_limit(monkeypatch):
    mock_conn = AsyncMock()
    _wire_pool(monkeypatch, mock_conn)
    with pytest.raises(HTTPException) as exc:
        await list_deletion_requests(_=_root_user(), limit=0)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await list_deletion_requests(_=_root_user(), limit=10000)
    assert exc.value.status_code == 422


# ── GET /admin/deletion-requests/{id} ─────────────────────────


@pytest.mark.asyncio
async def test_get_deletion_request_returns_row(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=_row(id="r-1"))
    _wire_pool(monkeypatch, mock_conn)

    result = await get_deletion_request("r-1", _=_root_user())
    assert result.id == "r-1"


@pytest.mark.asyncio
async def test_get_deletion_request_returns_404(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    _wire_pool(monkeypatch, mock_conn)
    with pytest.raises(HTTPException) as exc:
        await get_deletion_request("nope", _=_root_user())
    assert exc.value.status_code == 404


# ── POST /admin/deletion-requests/{id}/confirm ────────────────


@pytest.mark.asyncio
async def test_confirm_deletion_request_transitions_status(monkeypatch):
    confirmed_at = datetime(2026, 5, 1, 23, 0, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value=_row(status="confirmed", confirmed_at=confirmed_at),
    )
    _wire_pool(monkeypatch, mock_conn)

    result = await confirm_deletion_request("r-1", _=_root_user())
    assert result.status == "confirmed"
    assert result.confirmed_at == confirmed_at.isoformat()


@pytest.mark.asyncio
async def test_confirm_deletion_request_404_when_missing(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # UPDATE returns nothing
    _wire_pool(monkeypatch, mock_conn)
    with pytest.raises(HTTPException) as exc:
        await confirm_deletion_request("missing-uuid", _=_root_user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_confirm_deletion_request_409_on_terminal_state(monkeypatch):
    """Already-soft-deleted rows can't be confirmed (they're
    already past confirmation). The handler returns 409 with
    the actual current state."""
    mock_conn = AsyncMock()
    # First call (UPDATE) returns None — no rows match the
    # status='requested|confirmed' filter. Second call (the
    # status SELECT for the error message) returns the actual
    # current state.
    mock_conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"status": "soft_deleted"},
    ])
    _wire_pool(monkeypatch, mock_conn)
    with pytest.raises(HTTPException) as exc:
        await confirm_deletion_request("r-1", _=_root_user())
    assert exc.value.status_code == 409
    assert "soft_deleted" in exc.value.detail


# ── POST /admin/deletion-requests/{id}/cancel ─────────────────


@pytest.mark.asyncio
async def test_cancel_deletion_request_transitions_status(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=_row(status="cancelled"))
    _wire_pool(monkeypatch, mock_conn)
    result = await cancel_deletion_request("r-1", _=_root_user())
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_deletion_request_409_on_soft_deleted(monkeypatch):
    """Soft-deleted rows can't be cancelled — data has already
    been (potentially) destroyed. Operators reverse those via
    a separate restore endpoint (round-78+), not cancel."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"status": "soft_deleted"},
    ])
    _wire_pool(monkeypatch, mock_conn)
    with pytest.raises(HTTPException) as exc:
        await cancel_deletion_request("r-1", _=_root_user())
    assert exc.value.status_code == 409
    assert "soft_deleted" in exc.value.detail


# ── POST /admin/deletion-requests/{id}/restore ───────────────


@pytest.mark.asyncio
async def test_restore_deletion_request_restores_soft_deleted_rows(monkeypatch):
    soft_deleted_at = datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc)
    restore_by = datetime(2026, 5, 31, 23, 5, 0, tzinfo=timezone.utc)
    restored_at = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            _row(
                id="00000000-0000-0000-0000-000000000001",
                target_user_id="alice",
                target_namespace="tenant-a",
                status="soft_deleted",
                soft_deleted_at=soft_deleted_at,
                restore_by=restore_by,
            ),
            _row(
                id="00000000-0000-0000-0000-000000000001",
                target_user_id="alice",
                target_namespace="tenant-a",
                status="restored",
                soft_deleted_at=soft_deleted_at,
                restore_by=restore_by,
                restored_at=restored_at,
            ),
        ]
    )
    mock_conn.fetchval = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    _wire_pool(monkeypatch, mock_conn)

    result = await restore_deletion_request(
        "00000000-0000-0000-0000-000000000001",
        _=_root_user(),
    )

    assert result.status == "restored"
    assert result.restored_at == restored_at.isoformat()
    restore_sqls = [call.args[0] for call in mock_conn.execute.await_args_list]
    assert restore_sqls
    assert all("SET deleted_at = NULL" in sql for sql in restore_sqls)
    assert all(call.args[1] == "alice" for call in mock_conn.execute.await_args_list)
    assert all(call.args[2] == "tenant-a" for call in mock_conn.execute.await_args_list)
    assert all(call.args[3] == soft_deleted_at for call in mock_conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_restore_deletion_request_409_after_grace_window(monkeypatch):
    soft_deleted_at = datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc)
    restore_by = datetime(2026, 5, 2, 23, 5, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value=_row(
            id="00000000-0000-0000-0000-000000000001",
            target_user_id="alice",
            status="soft_deleted",
            soft_deleted_at=soft_deleted_at,
            restore_by=restore_by,
        )
    )
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    _wire_pool(monkeypatch, mock_conn)

    with pytest.raises(HTTPException) as exc:
        await restore_deletion_request(
            "00000000-0000-0000-0000-000000000001",
            _=_root_user(),
        )

    assert exc.value.status_code == 409
    assert "restore window expired" in exc.value.detail
    mock_conn.execute.assert_not_awaited()


# ── POST /admin/deletion-requests/{id}/force-purge ───────────


@pytest.mark.asyncio
async def test_force_purge_deletion_request_hard_deletes_before_restore_by(monkeypatch):
    soft_deleted_at = datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc)
    restore_by = datetime(2026, 5, 31, 23, 5, 0, tzinfo=timezone.utc)
    hard_deleted_at = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            _row(
                id="00000000-0000-0000-0000-000000000001",
                target_user_id="alice",
                target_namespace="tenant-a",
                status="soft_deleted",
                soft_deleted_at=soft_deleted_at,
                restore_by=restore_by,
            ),
            _row(
                id="00000000-0000-0000-0000-000000000001",
                target_user_id="alice",
                target_namespace="tenant-a",
                status="hard_deleted",
                soft_deleted_at=soft_deleted_at,
                restore_by=restore_by,
                hard_deleted_at=hard_deleted_at,
            ),
        ]
    )
    mock_conn.execute = AsyncMock(return_value="DELETE 1")
    _wire_pool(monkeypatch, mock_conn)

    result = await force_purge_deletion_request(
        "00000000-0000-0000-0000-000000000001",
        _=_root_user(),
    )

    assert result.id == "00000000-0000-0000-0000-000000000001"
    assert result.status == "hard_deleted"
    assert result.hard_deleted_at == hard_deleted_at.isoformat()
    executed_sql = [call.args[0] for call in mock_conn.execute.await_args_list]
    assert any(sql.startswith("SET LOCAL mnemos.suppress_version_snapshot") for sql in executed_sql)
    assert any("DELETE FROM memories" in sql for sql in executed_sql)
    assert all(call.args[1] == "alice" for call in mock_conn.execute.await_args_list[1:])
    assert all(call.args[2] == "tenant-a" for call in mock_conn.execute.await_args_list[1:])
    update_sql = mock_conn.fetchrow.await_args_list[1].args[0]
    assert "SET status = 'hard_deleted'" in update_sql
    assert "hard_deleted_at = NOW()" in update_sql


@pytest.mark.asyncio
async def test_force_purge_deletion_request_409_on_non_soft_deleted(monkeypatch):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value=_row(
            id="00000000-0000-0000-0000-000000000001",
            target_user_id="alice",
            status="confirmed",
        )
    )
    mock_conn.execute = AsyncMock(return_value="DELETE 1")
    _wire_pool(monkeypatch, mock_conn)

    with pytest.raises(HTTPException) as exc:
        await force_purge_deletion_request(
            "00000000-0000-0000-0000-000000000001",
            _=_root_user(),
        )

    assert exc.value.status_code == 409
    assert "only 'soft_deleted' rows can be force-purged" in exc.value.detail
    mock_conn.execute.assert_not_awaited()

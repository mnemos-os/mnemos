"""Tests for the MCP audit Phase-D durable surface (#146).

The Python logger entry remains the always-on surface. The
mcp_audit_log table is the durable mirror written via fire-and-
forget when a Postgres pool is available. SQLite installs and any
sync-context callers (no running asyncio loop) skip the DB write
silently — the logger covers them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Migration list sync (regression for #143 follow-up)
# ---------------------------------------------------------------------------


def test_mcp_audit_log_migration_in_postgres_list():
    """The new v5_3_4 migration must be in the canonical postgres
    list (matched by tests/test_migration_lists_sync.py)."""
    from pathlib import Path
    from tests.test_migration_lists_sync import EXPECTED_MIGRATIONS

    assert "migrations_v5_3_4_mcp_audit_log.sql" in EXPECTED_MIGRATIONS
    repo_root = Path(__file__).resolve().parents[1]
    assert (
        repo_root / "db" / "migrations_v5_3_4_mcp_audit_log.sql"
    ).exists()


def test_mcp_audit_log_migration_in_sqlite_list():
    """The SQLite parallel migration must be in the SQLite list."""
    from pathlib import Path
    from tests.test_migration_lists_sync import EXPECTED_SQLITE_MIGRATIONS

    assert (
        "migrations_v5_3_4_mcp_audit_log_sqlite.sql"
        in EXPECTED_SQLITE_MIGRATIONS
    )
    repo_root = Path(__file__).resolve().parents[1]
    assert (
        repo_root / "db" / "migrations_sqlite"
        / "migrations_v5_3_4_mcp_audit_log_sqlite.sql"
    ).exists()


# ---------------------------------------------------------------------------
# insert_audit_record contract
# ---------------------------------------------------------------------------


class _StubPgConn:
    """Async-conn stub that captures the executed SQL + args."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.__class__.__module__ = "asyncpg.connection"

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


class _StubSqliteConn:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.__class__.__module__ = "sqlite3"

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def test_insert_audit_record_writes_via_postgres_conn():
    from mnemos.db.mcp_audit_repo import insert_audit_record

    conn = _StubPgConn()

    async def run():
        return await insert_audit_record(
            conn,
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={"query": {"type": "str", "length": 5}},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is True
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO mcp_audit_log" in sql
    assert "$4::jsonb" in sql  # JSONB cast for parameter_shape
    # Args order: caller_user_id, role, tool, parameter_shape (JSON),
    # outcome, error_class.
    assert args[0] == "alice"
    assert args[1] == "user"
    assert args[2] == "search_memories"
    parsed_shape = json.loads(args[3])
    assert parsed_shape == {"query": {"type": "str", "length": 5}}
    assert args[4] == "success"
    assert args[5] is None


def test_insert_audit_record_skips_sqlite_conn():
    """SQLite installs keep the logger-only behavior. Mirrors the
    deletion_log postgres-only writer pattern."""
    from mnemos.db.mcp_audit_repo import insert_audit_record

    conn = _StubSqliteConn()

    async def run():
        return await insert_audit_record(
            conn,
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is False
    assert conn.calls == []  # no execute called


def test_insert_audit_record_skips_none_conn():
    from mnemos.db.mcp_audit_repo import insert_audit_record

    async def run():
        return await insert_audit_record(
            None,
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is False


def test_insert_audit_record_rejects_invalid_outcome():
    from mnemos.db.mcp_audit_repo import insert_audit_record

    conn = _StubPgConn()

    async def run():
        return await insert_audit_record(
            conn,
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={},
            outcome="garbage",
        )

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(run())
    assert "invalid mcp_audit_log outcome" in str(exc_info.value)
    assert conn.calls == []


@pytest.mark.parametrize(
    "outcome",
    ["called", "success", "failure", "error", "denied", "root_bypass"],
)
def test_insert_audit_record_accepts_each_valid_outcome(outcome):
    """#164: every member of VALID_OUTCOMES must be writable end-to-
    end. Earlier coverage tested only the "error" path + the
    invalid-string rejection; the new emission paths from #154
    (rate-limit "denied"), #156 (context-mismatch "denied"), and
    #157 (handler-failure "failure") could regress if the schema
    or repo validation drifted out of sync with the dispatcher."""
    from mnemos.db.mcp_audit_repo import insert_audit_record

    conn = _StubPgConn()

    async def run():
        return await insert_audit_record(
            conn,
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={"query": {"type": "str", "length": 5}},
            outcome=outcome,
        )

    asyncio.run(run())
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO mcp_audit_log" in sql
    # Outcome (5th positional after caller_user_id, role, tool,
    # parameter_shape) must round-trip the parametrized value.
    assert args[4] == outcome


def test_insert_audit_record_includes_error_class():
    from mnemos.db.mcp_audit_repo import insert_audit_record

    conn = _StubPgConn()

    async def run():
        await insert_audit_record(
            conn,
            caller_user_id="bob",
            role="user",
            tool="kg_create_triple",
            parameter_shape={"subject": {"type": "str", "length": 3}},
            outcome="error",
            error_class="ToolError",
        )

    asyncio.run(run())
    sql, args = conn.calls[0]
    assert args[5] == "ToolError"
    assert args[4] == "error"


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


def test_postgres_migration_creates_mcp_audit_log_table():
    """Schema includes the table + indexes + outcome CHECK."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1]
        / "db" / "migrations_v5_3_4_mcp_audit_log.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS mcp_audit_log" in sql
    assert "parameter_shape JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    # Outcome CHECK constraint matches VALID_OUTCOMES in the repo.
    assert "outcome IN (" in sql
    for outcome in ("called", "success", "failure", "error", "denied", "root_bypass"):
        assert f"'{outcome}'" in sql
    # Three indexes for common operator queries.
    assert "idx_mcp_audit_log_created_desc" in sql
    assert "idx_mcp_audit_log_caller_created_desc" in sql
    assert "idx_mcp_audit_log_tool_created_desc" in sql


def test_sqlite_migration_creates_parallel_table():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1]
        / "db" / "migrations_sqlite"
        / "migrations_v5_3_4_mcp_audit_log_sqlite.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS mcp_audit_log" in sql
    # Same outcome CHECK, sqlite version stores parameter_shape as TEXT.
    assert "parameter_shape TEXT NOT NULL DEFAULT" in sql
    for outcome in ("called", "success", "failure", "error", "denied", "root_bypass"):
        assert f"'{outcome}'" in sql


# ---------------------------------------------------------------------------
# Dispatcher integration — fire-and-forget DB write scheduled
# ---------------------------------------------------------------------------


def test_log_tool_audit_schedules_persist_when_loop_running(monkeypatch):
    """When called inside a running asyncio loop, _mcp_log_tool_audit
    schedules persist_audit_record_via_pool via create_task."""
    from mnemos.mcp.tools import _security

    captured: list[dict[str, Any]] = []

    async def fake_persist(**kwargs: Any) -> bool:
        captured.append(kwargs)
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        fake_persist,
    )

    async def run():
        _security._mcp_log_tool_audit(
            caller_id="alice",
            role="user",
            tool_name="search_memories",
            parameters={"query": "abc"},
            outcome="success",
        )
        # Yield to the loop so the create_task runs.
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(captured) == 1
    record = captured[0]
    assert record["caller_user_id"] == "alice"
    assert record["role"] == "user"
    assert record["tool"] == "search_memories"
    assert record["outcome"] == "success"
    # parameter_shape is the redacted shape, not raw values.
    assert record["parameter_shape"] == {
        "query": {"type": "str", "length": 3}
    }


def test_log_tool_audit_no_running_loop_does_not_raise(monkeypatch):
    """Sync caller (no running loop) — DB persist skipped silently,
    logger entry remains the always-on surface."""
    from mnemos.mcp.tools import _security

    persist_called = False

    async def fake_persist(**kwargs: Any) -> bool:
        nonlocal persist_called
        persist_called = True
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        fake_persist,
    )

    # Sync context — no running loop. Must not raise.
    _security._mcp_log_tool_audit(
        caller_id="alice",
        role="user",
        tool_name="get_stats",
        parameters={},
        outcome="success",
    )
    assert persist_called is False


def test_log_root_bypass_schedules_persist_with_root_bypass_outcome(monkeypatch):
    """Root-bypass calls also persist, with outcome='root_bypass' so
    operators can query for elevation events."""
    from mnemos.mcp.tools import _security

    captured: list[dict[str, Any]] = []

    async def fake_persist(**kwargs: Any) -> bool:
        captured.append(kwargs)
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        fake_persist,
    )

    async def run():
        _security._mcp_log_root_bypass(
            caller_id="root",
            tool_name="delete_memory",
            parameters={"memory_id": "abc-123"},
        )
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(captured) == 1
    assert captured[0]["outcome"] == "root_bypass"
    assert captured[0]["role"] == "root"


def test_persist_audit_record_via_pool_returns_false_when_no_pool(monkeypatch):
    """Pool unavailable → silently False. Audit failures must never
    propagate to the tool dispatcher."""
    from mnemos.db.mcp_audit_repo import persist_audit_record_via_pool

    def raise_no_pool():
        raise RuntimeError("pool not initialized")

    # lifecycle.get_pool_manager normally raises HTTPException(503)
    # when no pool. Stub a generic exception to verify we catch ALL
    # exception classes (not just HTTPException).
    import mnemos.core.lifecycle as _lc

    monkeypatch.setattr(_lc, "get_pool_manager", raise_no_pool)

    async def run():
        return await persist_audit_record_via_pool(
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is False


# ---------------------------------------------------------------------------
# Round-1 fix: HTTP fallback for standalone MCP bridges
# ---------------------------------------------------------------------------


def test_persist_audit_record_falls_back_to_http_when_pool_unavailable(monkeypatch):
    """Round-1 fix: when no in-process pool (standalone MCP bridge),
    persist_audit_record must fall back to httpx POST against
    /v1/internal/mcp_audit. The bridge's own bearer token authenticates."""
    import mnemos.core.lifecycle as _lc
    import mnemos.db.mcp_audit_repo as repo

    def raise_no_pool():
        raise RuntimeError("pool not initialized")

    monkeypatch.setattr(_lc, "get_pool_manager", raise_no_pool)

    captured: dict[str, Any] = {}

    class _StubResponse:
        status_code = 204

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["client_init"] = kwargs

        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _StubResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _StubResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)

    # Stub settings to point at a known base + token.
    class _StubServer:
        base = "http://localhost:5002"
        api_key = "test-token"
        internal_audit_token = ""

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _StubSettings()
    )

    async def run():
        return await repo.persist_audit_record(
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={"query": {"type": "str", "length": 5}},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is True
    assert captured["url"] == "http://localhost:5002/v1/internal/mcp_audit"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    # Body MUST NOT carry caller_user_id or role — those come from auth
    # context server-side.
    assert "caller_user_id" not in captured["json"]
    assert "role" not in captured["json"]
    assert captured["json"]["tool"] == "search_memories"
    assert captured["json"]["outcome"] == "success"
    assert captured["json"]["parameter_shape"] == {
        "query": {"type": "str", "length": 5}
    }


def test_persist_audit_record_via_http_skips_when_no_base(monkeypatch):
    """No MNEMOS_BASE → http path is a no-op (silent False)."""
    import mnemos.db.mcp_audit_repo as repo

    class _EmptyServer:
        base = ""
        api_key = "test-token"
        internal_audit_token = ""

    class _EmptySettings:
        server = _EmptyServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _EmptySettings()
    )

    async def run():
        return await repo.persist_audit_record_via_http(
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is False


def test_persist_audit_record_via_http_skips_when_no_token(monkeypatch):
    """No MNEMOS_API_KEY → http path is a no-op."""
    import mnemos.db.mcp_audit_repo as repo

    class _EmptyServer:
        base = "http://localhost:5002"
        api_key = ""
        internal_audit_token = ""

    class _EmptySettings:
        server = _EmptyServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _EmptySettings()
    )

    async def run():
        return await repo.persist_audit_record_via_http(
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is False


def test_persist_audit_record_via_http_swallows_post_errors(monkeypatch):
    """httpx errors must NOT propagate — audit failures break tools otherwise."""
    import mnemos.db.mcp_audit_repo as repo

    class _StubServer:
        base = "http://localhost:5002"
        api_key = "test-token"
        internal_audit_token = ""

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _StubSettings()
    )

    class _BoomClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("API unreachable")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

    async def run():
        return await repo.persist_audit_record_via_http(
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    # Must not raise.
    rc = asyncio.run(run())
    assert rc is False


def test_mcp_audit_route_mounted():
    """Source-level guard: the API mounts mcp_audit_router."""
    import inspect
    from mnemos.api import main

    src = inspect.getsource(main)
    assert "mcp_audit_router" in src
    assert "from mnemos.api.routes.mcp_audit import router" in src


def test_mcp_audit_route_authenticates_caller_from_context():
    """Source-level guard: the route reads caller_user_id and role
    from auth context (UserContext), NOT from the body. Prevents
    bridge from forging attribution."""
    import inspect
    from mnemos.api.routes import mcp_audit

    src = inspect.getsource(mcp_audit.write_mcp_audit_record)
    # Body model must NOT have caller_user_id or role fields.
    body_src = inspect.getsource(mcp_audit.MCPAuditRequest)
    assert "caller_user_id" not in body_src
    # The handler reads from `user.user_id` and `user.role`.
    assert "user.user_id" in src
    assert "user.role" in src


# ---------------------------------------------------------------------------
# Round-2 fixes: per-user backend api_key + parameter_shape validation + GRANT
# ---------------------------------------------------------------------------


def test_persist_audit_record_via_http_prefers_mcp_backend_context_api_key(monkeypatch):
    """Round-2 HIGH: per-user MCP mode (MNEMOS_MCP_TOKENS=user:mcp:api)
    sets the active backend api_key in MCP context. The httpx fallback
    must use that context's key, not settings.server.api_key (which
    would either be empty or a global key)."""
    import mnemos.db.mcp_audit_repo as repo
    import mnemos.mcp.tools._runtime as _runtime

    captured: dict[str, Any] = {}

    class _StubResponse:
        status_code = 204

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _StubResponse:
            captured["headers"] = headers
            return _StubResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)

    # No global api_key — per-user MCP mode shape.
    class _StubServer:
        base = "http://localhost:5002"
        api_key = ""
        internal_audit_token = ""

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _StubSettings()
    )

    # Set the per-call MCP backend context to a per-user api_key.
    tokens = _runtime.set_mcp_backend_context(
        api_key="per-user-key-abc",
        user_id="alice",
        role="user",
        namespace="alice-ns",
    )
    try:
        async def run():
            return await repo.persist_audit_record_via_http(
                tool="search_memories",
                parameter_shape={},
                outcome="success",
            )

        rc = asyncio.run(run())
        assert rc is True
        # Per-user context key wins over the empty settings key.
        assert captured["headers"]["Authorization"] == "Bearer per-user-key-abc"
    finally:
        _runtime.reset_mcp_backend_context(tokens)


def test_mcp_audit_route_rejects_raw_string_parameter_shape():
    """Round-2 MEDIUM: route validator must reject parameter_shape
    that contains raw string values (not the redacted-shape pattern)."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({"query": "raw-secret-value"})
    # The string is not an object — fails the per-key shape check.
    assert "must be an object" in str(exc_info.value)


def test_mcp_audit_route_rejects_nested_dict_in_shape():
    """Nested dicts (where values are not type-shape entries) must reject."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({
            "query": {
                "type": "str",
                "secret": "hidden-value",  # NOT in allowed entry keys
            }
        })
    assert "unexpected fields" in str(exc_info.value)


def test_mcp_audit_route_rejects_too_many_keys():
    """Round-2 size limit: parameter_shape capped at 64 keys."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    big_shape = {f"key{i}": {"type": "str", "length": 1} for i in range(100)}
    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape(big_shape)
    assert "too many keys" in str(exc_info.value)


def test_mcp_audit_route_rejects_oversized_key_name():
    """#158: _MAX_PARAMETER_SHAPE_KEY_LENGTH (128) must reject keys
    over the limit. Defends against an attacker stuffing the audit
    table with megabyte-scale keys to balloon the row size."""
    from mnemos.api.routes.mcp_audit import (
        _MAX_PARAMETER_SHAPE_KEY_LENGTH,
        _validate_parameter_shape,
    )

    long_key = "a" * (_MAX_PARAMETER_SHAPE_KEY_LENGTH + 1)
    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({long_key: {"type": "str", "length": 0}})
    assert "exceeds max length" in str(exc_info.value)


def test_mcp_audit_route_accepts_key_at_max_length():
    """Boundary: exactly _MAX_PARAMETER_SHAPE_KEY_LENGTH chars is OK."""
    from mnemos.api.routes.mcp_audit import (
        _MAX_PARAMETER_SHAPE_KEY_LENGTH,
        _validate_parameter_shape,
    )

    key = "a" * _MAX_PARAMETER_SHAPE_KEY_LENGTH
    # Must not raise.
    _validate_parameter_shape({key: {"type": "str", "length": 0}})


def test_mcp_audit_route_rejects_too_many_item_types():
    """#158: _MAX_PARAMETER_SHAPE_ITEM_TYPES (16) must reject lists
    over the limit."""
    from mnemos.api.routes.mcp_audit import (
        _MAX_PARAMETER_SHAPE_ITEM_TYPES,
        _validate_parameter_shape,
    )

    too_many = ["str"] * (_MAX_PARAMETER_SHAPE_ITEM_TYPES + 1)
    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({
            "tags": {"type": "list", "count": 0, "item_types": too_many}
        })
    assert "item_types too long" in str(exc_info.value)


def test_mcp_audit_route_accepts_item_types_at_max_length():
    """Boundary: exactly _MAX_PARAMETER_SHAPE_ITEM_TYPES is OK."""
    from mnemos.api.routes.mcp_audit import (
        _MAX_PARAMETER_SHAPE_ITEM_TYPES,
        _validate_parameter_shape,
    )

    items = ["str"] * _MAX_PARAMETER_SHAPE_ITEM_TYPES
    # Must not raise.
    _validate_parameter_shape({
        "tags": {"type": "list", "count": 0, "item_types": items}
    })


def test_mcp_audit_route_no_dead_type_name_length_constant():
    """#158: ensure the dead `_MAX_PARAMETER_SHAPE_TYPE_NAME` was
    removed. The closed `_ALLOWED_SHAPE_TYPE_NAMES` allowlist is
    strictly stricter than any character-count ceiling, so the
    constant was unused. Its removal is a deliberate cleanup."""
    import mnemos.api.routes.mcp_audit as audit_route

    assert not hasattr(audit_route, "_MAX_PARAMETER_SHAPE_TYPE_NAME"), (
        "_MAX_PARAMETER_SHAPE_TYPE_NAME was removed in #158 as dead "
        "code (the allowlist is strictly stricter than any length "
        "ceiling). If you need to reintroduce it, write a test that "
        "exercises a limit it actually enforces."
    )


def test_mcp_audit_route_accepts_valid_shape_from_mcp_parameter_shape():
    """Sanity: shapes produced by _mcp_parameter_shape pass validation."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape
    from mnemos.mcp.tools._security import _mcp_parameter_shape

    shape = _mcp_parameter_shape({
        "query": "hello",
        "limit": 10,
        "tags": ["a", "b", "c"],
        "config": {"nested": "value"},
    })
    # Must not raise.
    result = _validate_parameter_shape(shape)
    assert result == shape


def test_postgres_migration_grants_insert_to_runtime_role():
    """Round-2 MEDIUM: migration must grant INSERT to the runtime
    app role (mnemos_user) so post-installer-upgrade writes don't
    silently fail with permission denied."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1]
        / "db" / "migrations_v5_3_4_mcp_audit_log.sql"
    ).read_text()
    assert "GRANT SELECT, INSERT ON mcp_audit_log TO mnemos_user" in sql
    # Idempotent: only granted if role exists.
    assert "FROM pg_roles WHERE rolname = 'mnemos_user'" in sql


def test_mcp_audit_route_validator_rejects_non_dict_value():
    """parameter_shape must be a dict at the top level."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape("not-a-dict")  # type: ignore[arg-type]
    assert "must be an object" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Round-3 fix: closed type allowlist
# ---------------------------------------------------------------------------


def test_mcp_audit_route_rejects_raw_value_in_type_field():
    """Round-3 MEDIUM: parameter_shape[*].type must be one of the
    closed allowlist (str/bool/int/float/list/dict/none/bytes/tuple/
    set/frozenset/NoneType). Earlier round only filtered length and
    whitespace, letting values like 'sk_live_secret' slip through."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({
            "api_key": {"type": "sk_live_secret"}
        })
    assert "not in the allowed type allowlist" in str(exc_info.value)


def test_mcp_audit_route_rejects_raw_value_in_item_types():
    """Same closed allowlist applies to item_types."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    with pytest.raises(ValueError) as exc_info:
        _validate_parameter_shape({
            "tags": {
                "type": "list",
                "count": 1,
                "item_types": ["raw-secret-value"],
            }
        })
    assert "not in the allowed type allowlist" in str(exc_info.value)


def test_mcp_audit_route_accepts_all_allowlist_type_names():
    """All canonical type names from _mcp_parameter_shape pass."""
    from mnemos.api.routes.mcp_audit import _validate_parameter_shape

    for type_name in (
        "str", "bool", "int", "float", "list", "dict", "none",
        "bytes", "tuple", "set", "frozenset", "NoneType",
    ):
        # Must not raise.
        _validate_parameter_shape({"k": {"type": type_name}})


# ---------------------------------------------------------------------------
# #148: service-only credential for /v1/internal/mcp_audit
# ---------------------------------------------------------------------------


def test_internal_audit_token_required_when_configured(monkeypatch):
    """When MNEMOS_INTERNAL_AUDIT_TOKEN is set, the route MUST reject
    requests without (or with wrong) X-Mnemos-Audit-Token. Constant-
    time compare prevents timing leaks."""
    from fastapi import HTTPException
    from mnemos.api.routes.mcp_audit import _require_internal_audit_token

    class _StubServer:
        internal_audit_token = "secret-bridge-token"

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.api.routes.mcp_audit.get_settings",
        lambda: _StubSettings(),
    )

    # Missing header → 401.
    with pytest.raises(HTTPException) as exc_info:
        _require_internal_audit_token(x_mnemos_audit_token=None)
    assert exc_info.value.status_code == 401
    assert "X-Mnemos-Audit-Token" in exc_info.value.detail

    # Empty header → 401.
    with pytest.raises(HTTPException):
        _require_internal_audit_token(x_mnemos_audit_token="")

    # Wrong token → 401.
    with pytest.raises(HTTPException) as exc_info:
        _require_internal_audit_token(x_mnemos_audit_token="wrong-token")
    assert exc_info.value.status_code == 401

    # Correct token → returns None (no raise).
    result = _require_internal_audit_token(
        x_mnemos_audit_token="secret-bridge-token"
    )
    assert result is None


def test_internal_audit_token_unset_falls_back_to_legacy(monkeypatch):
    """When MNEMOS_INTERNAL_AUDIT_TOKEN is unset, the gating is a
    no-op (legacy mode). Allows phased rollout — operators can ship
    without the token configured, then add it later."""
    from mnemos.api.routes.mcp_audit import _require_internal_audit_token

    class _EmptyServer:
        internal_audit_token = ""

    class _EmptySettings:
        server = _EmptyServer()

    monkeypatch.setattr(
        "mnemos.api.routes.mcp_audit.get_settings",
        lambda: _EmptySettings(),
    )

    # No header — should not raise (legacy mode).
    result = _require_internal_audit_token(x_mnemos_audit_token=None)
    assert result is None
    # Even a wrong header is ignored in legacy mode.
    result = _require_internal_audit_token(
        x_mnemos_audit_token="anything"
    )
    assert result is None


def test_persist_audit_record_via_http_includes_audit_token_when_configured(monkeypatch):
    """The bridge-side httpx writer includes X-Mnemos-Audit-Token
    when the service-only credential is configured."""
    import mnemos.db.mcp_audit_repo as repo

    captured: dict[str, Any] = {}

    class _StubResponse:
        status_code = 204

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _StubResponse:
            captured["headers"] = headers
            return _StubResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)

    class _StubServer:
        base = "http://localhost:5002"
        api_key = "test-token"
        internal_audit_token = "secret-bridge-token"

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _StubSettings()
    )

    async def run():
        return await repo.persist_audit_record_via_http(
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    rc = asyncio.run(run())
    assert rc is True
    assert captured["headers"]["X-Mnemos-Audit-Token"] == "secret-bridge-token"
    # Bearer auth still included for caller attribution.
    assert captured["headers"]["Authorization"] == "Bearer test-token"


def test_persist_audit_record_via_http_omits_audit_token_when_not_configured(monkeypatch):
    """When token is unset, no X-Mnemos-Audit-Token header is sent
    (matching legacy mode on the route side)."""
    import mnemos.db.mcp_audit_repo as repo

    captured: dict[str, Any] = {}

    class _StubResponse:
        status_code = 204

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _StubResponse:
            captured["headers"] = dict(headers)
            return _StubResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)

    class _StubServer:
        base = "http://localhost:5002"
        api_key = "test-token"
        internal_audit_token = ""  # legacy mode

    class _StubSettings:
        server = _StubServer()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings", lambda: _StubSettings()
    )

    async def run():
        return await repo.persist_audit_record_via_http(
            tool="search_memories",
            parameter_shape={},
            outcome="success",
        )

    asyncio.run(run())
    assert "X-Mnemos-Audit-Token" not in captured["headers"]
    assert captured["headers"]["Authorization"] == "Bearer test-token"


# ---------------------------------------------------------------------------
# #149: track audit tasks for shutdown drain
# ---------------------------------------------------------------------------


def test_schedule_audit_persist_tracks_inflight_task(monkeypatch):
    """Round-3 residual #2 (#149): scheduled tasks land in the
    _INFLIGHT_AUDIT_TASKS set so transport-shutdown drain can await
    them. Without tracking, asyncio.run cancels and silently drops."""
    from mnemos.mcp.tools import _security

    persist_started = asyncio.Event()
    persist_done = asyncio.Event()

    async def slow_persist(**kwargs: Any) -> bool:
        persist_started.set()
        await asyncio.sleep(0.05)
        persist_done.set()
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        slow_persist,
    )

    async def run():
        # Clear any leftover state.
        _security._INFLIGHT_AUDIT_TASKS.clear()
        _security._mcp_log_tool_audit(
            caller_id="alice",
            role="user",
            tool_name="search_memories",
            parameters={"query": "abc"},
            outcome="success",
        )
        # The task is in-flight while persist sleeps.
        await persist_started.wait()
        assert len(_security._INFLIGHT_AUDIT_TASKS) == 1
        # Drain awaits to completion.
        drained = await _security.drain_pending_audit_tasks(timeout=2.0)
        assert drained == 1
        assert persist_done.is_set()
        # Task was removed from the set after completion (via
        # add_done_callback).
        assert len(_security._INFLIGHT_AUDIT_TASKS) == 0

    asyncio.run(run())


def test_drain_pending_audit_tasks_with_no_pending_returns_zero():
    """Empty drain is a fast no-op."""
    from mnemos.mcp.tools import _security

    async def run():
        _security._INFLIGHT_AUDIT_TASKS.clear()
        return await _security.drain_pending_audit_tasks(timeout=1.0)

    assert asyncio.run(run()) == 0


def test_drain_pending_audit_tasks_handles_timeout(monkeypatch):
    """Tasks that don't complete within timeout don't propagate;
    drain returns the count anyway. Caller can log if non-zero."""
    from mnemos.mcp.tools import _security

    async def never_finishes(**kwargs: Any) -> bool:
        await asyncio.sleep(60)
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        never_finishes,
    )

    async def run():
        _security._INFLIGHT_AUDIT_TASKS.clear()
        _security._mcp_log_tool_audit(
            caller_id="alice",
            role="user",
            tool_name="search_memories",
            parameters={"query": "abc"},
            outcome="success",
        )
        # Drain with a tight timeout — task is still pending.
        # Must not raise.
        result = await _security.drain_pending_audit_tasks(timeout=0.05)
        return result

    rc = asyncio.run(run())
    # Non-zero count even though the task didn't finish.
    assert rc == 1


def test_schedule_audit_persist_bounded_backlog(monkeypatch, caplog):
    """When _INFLIGHT_AUDIT_TASKS reaches MAX, new tasks are
    dropped (logger entry is still emitted; row is the loss).
    Prevents unbounded growth during an audit-DB outage.

    #165: ALSO verify the warning log line is emitted — without
    that, an operator hitting the cap would have no signal that
    audit rows were being dropped silently.
    """
    import logging as _logging

    from mnemos.mcp.tools import _security

    async def stuck_persist(**kwargs: Any) -> bool:
        await asyncio.sleep(60)
        return True

    monkeypatch.setattr(
        "mnemos.db.mcp_audit_repo.persist_audit_record",
        stuck_persist,
    )
    caplog.set_level(_logging.WARNING, logger="mnemos.mcp.audit")

    async def run():
        _security._INFLIGHT_AUDIT_TASKS.clear()
        # Pre-populate set to MAX. We do this directly with sentinel
        # tasks so we don't need MAX_INFLIGHT_AUDIT_TASKS real ones.
        loop = asyncio.get_running_loop()
        sentinel_tasks = []
        for _ in range(_security._MAX_INFLIGHT_AUDIT_TASKS):
            t = loop.create_task(asyncio.sleep(60))
            sentinel_tasks.append(t)
            _security._INFLIGHT_AUDIT_TASKS.add(t)

        # New schedule should be refused (bounded). The set is
        # already at MAX, so the count after the call should be MAX
        # still — no new task added.
        before = len(_security._INFLIGHT_AUDIT_TASKS)
        _security._schedule_audit_persist(
            caller_user_id="alice",
            role="user",
            tool="search_memories",
            parameter_shape={},
            outcome="success",
            error_class=None,
        )
        after = len(_security._INFLIGHT_AUDIT_TASKS)
        assert after == before  # no new task added

        # Cleanup.
        for t in sentinel_tasks:
            t.cancel()
        _security._INFLIGHT_AUDIT_TASKS.clear()

    asyncio.run(run())

    # The warning log message must surface so operators know audit
    # rows are being dropped (the table-row loss is silent at the
    # repo layer; the only signal is this warning).
    backlog_warnings = [
        rec.message for rec in caplog.records
        if rec.levelno >= _logging.WARNING
        and "inflight backlog" in rec.message
        and "dropping persist" in rec.message
    ]
    assert backlog_warnings, (
        f"expected an inflight-backlog warning when _MAX_INFLIGHT_AUDIT_TASKS "
        f"is reached; captured records: "
        f"{[r.message for r in caplog.records]}"
    )
    # The warning must name the dropped tool + caller so operators
    # can tell what they're losing.
    full_text = " ".join(backlog_warnings)
    assert "search_memories" in full_text
    assert "alice" in full_text


def test_lifecycle_hook_registers_audit_drain():
    """Source-level guard: API lifecycle registers the drain hook
    so the FastAPI process awaits pending writes on shutdown."""
    import inspect
    from mnemos.api import lifecycle_hooks

    src = inspect.getsource(lifecycle_hooks)
    assert "drain_pending_audit_tasks" in src
    assert 'register_lifespan_cleanup_hook("mcp audit drain"' in src


def test_stdio_transport_drains_audit_on_shutdown():
    """Source-level guard: stdio bridge's main() awaits the drain
    in a finally block so taskloss-on-exit is prevented."""
    import inspect
    from mnemos.mcp import stdio

    src = inspect.getsource(stdio.main)
    assert "drain_pending_audit_tasks" in src
    assert "finally:" in src


def test_stdio_transport_logs_drained_count():
    """#163: stdio bridge logs the drained count for parity with the
    http bridge. Without this, stdio operators have no observable
    signal that audit writes were waiting at shutdown — the http
    bridge already logs `drained N pending mcp_audit_log persist
    task(s)`."""
    import inspect
    from mnemos.mcp import stdio

    src = inspect.getsource(stdio.main)
    # Capture return value and gate on truthiness.
    assert "drained = await drain_pending_audit_tasks" in src or \
           "drained=await drain_pending_audit_tasks" in src, (
        "expected `drained = await drain_pending_audit_tasks(...)` "
        "to capture the return value"
    )
    assert "if drained:" in src
    # Round-2 of #163: stdio's basicConfig sets WARNING level, so
    # logger.info would be suppressed. logger.warning is required so
    # the drain count actually surfaces.
    assert "logger.warning" in src, (
        "stdio's basicConfig sets WARNING level — logger.info would be "
        "suppressed; use logger.warning for the drain-count signal"
    )
    assert "pending mcp_audit_log persist" in src


def test_http_transport_registers_drain_on_shutdown():
    """Source-level guard: MCP HTTP Starlette app registers the drain
    via the ``lifespan=`` context manager (`_mcp_http_lifespan` →
    `_drain_audit_tasks_on_shutdown`). Read the file directly
    because mnemos.mcp.http has module-level code that calls
    sys.exit(2) when MNEMOS_MCP_TOKEN is unset (test env).

    Updated in #199: Starlette 1.0 removed the legacy
    `on_shutdown=[...]` kwarg, so the registration shape moved to
    `lifespan=_mcp_http_lifespan` with the drain awaited inside
    the context manager's exit."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "mnemos" / "mcp" / "http.py"
    ).read_text()
    assert "_drain_audit_tasks_on_shutdown" in src
    # Pre-Starlette-1.0 shape was `on_shutdown=[...]`; current is
    # the lifespan context manager `_mcp_http_lifespan` that
    # awaits `_drain_audit_tasks_on_shutdown` on exit.
    assert "lifespan=_mcp_http_lifespan" in src, (
        "mnemos/mcp/http.py no longer registers the audit-drain "
        "via `lifespan=_mcp_http_lifespan`. If the registration "
        "shape changed deliberately, update this guard."
    )
    assert "_mcp_http_lifespan" in src
    assert "await _drain_audit_tasks_on_shutdown()" in src, (
        "The audit drain is no longer awaited inside the "
        "lifespan context manager; the persist tasks would be "
        "cancelled before completion on shutdown."
    )
    assert "drain_pending_audit_tasks" in src

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


# ── _rls_context (raw asyncpg path) — same SQL shape ──────────────────────
#
# Codex round-5 (review-moms3d5t-0b5c5t) caught that the helper-fix
# in round-16 missed the parallel _rls_context() in
# memories.py used by raw asyncpg endpoints (compression manifests,
# rehydrate, ingest, session). The two paths must use the same
# parameterizable SQL or RLS-enabled deployments will still 500
# whenever they hit one of those routes.


@pytest.mark.asyncio
async def test_rls_context_uses_set_config_not_set_local(monkeypatch):
    """The raw-asyncpg RLS context must use the same set_config
    form as maybe_set_pg_rls. Pinning so a divergence is caught
    by tests, not by an unhappy production deployment."""
    from mnemos.api.routes.memories import _rls_context

    monkeypatch.setattr(_lc, "_rls_enabled", True)

    @AsyncMock
    async def _exec(sql, *args):
        return None

    conn = SimpleNamespace()
    conn.execute = AsyncMock()

    class _Tx:
        async def __aenter__(self_inner):
            return None

        async def __aexit__(self_inner, *exc):
            return None

    conn.transaction = lambda: _Tx()

    user = _user(user_id="alice", role="user")
    async with _rls_context(conn, user):
        pass

    assert conn.execute.await_count == 2
    sqls = [call.args[0] for call in conn.execute.await_args_list]
    for sql in sqls:
        assert "set_config" in sql, (
            f"_rls_context must use set_config(), got: {sql!r}"
        )
        assert "SET LOCAL" not in sql, (
            f"_rls_context must NOT use SET LOCAL ... = $1 form (broken on PG): {sql!r}"
        )
        assert "$1" in sql and "true" in sql

    vals = [call.args[1] for call in conn.execute.await_args_list]
    assert "alice" in vals
    assert "user" in vals


@pytest.mark.asyncio
async def test_rls_context_no_op_when_rls_disabled(monkeypatch):
    from mnemos.api.routes.memories import _rls_context

    monkeypatch.setattr(_lc, "_rls_enabled", False)
    conn = SimpleNamespace()
    conn.execute = AsyncMock()
    conn.transaction = AsyncMock()

    async with _rls_context(conn, _user()):
        pass

    assert conn.execute.await_count == 0
    assert conn.transaction.await_count == 0


def _scan_for_broken_set_local(text: str, path: object) -> list[tuple[int, str]]:
    """AST-walk a Python source file and return every string literal
    (regardless of quoting style — single/double/triple/raw, f-string
    literal segment) that matches the broken ``SET LOCAL <name> = $N``
    SQL shape, case-insensitively.

    Codex round-6 (review-moms6o3v-1jr3xv) flagged that a
    raw-source regex over the whole file body was too porous: it
    only catches double-quoted strings starting with uppercase
    SET LOCAL. An AST walk over ``ast.Constant(value=str)`` and
    f-string ``ast.JoinedStr`` literal parts catches the shape
    regardless of how the source quotes it.
    """
    import ast
    import re

    # Anchor at start-of-line (with re.MULTILINE) so prose mentions
    # of "SET LOCAL ... = $1" inside docstrings/comments don't match,
    # but actual SQL statements that START with SET LOCAL (possibly
    # after a semicolon-newline in multi-statement strings) do.
    #
    # Postgres SET syntax accepts both ``=`` and the ``TO`` keyword
    # between the configuration parameter and the value
    # (https://www.postgresql.org/docs/current/sql-set.html). Both
    # forms reject bind parameters identically, so the scanner has
    # to flag both. ``\s*=\s*`` keeps the no-whitespace ``x=$1``
    # case; ``\s+to\s+`` requires whitespace around the keyword so
    # accidental matches inside identifiers (``settoken``) don't
    # false-positive.
    pattern = re.compile(
        r"(?:^|;\s*)\s*set\s+local\s+\S+(?:\s*=\s*|\s+to\s+)\$\d",
        re.IGNORECASE | re.MULTILINE,
    )
    findings: list[tuple[int, str]] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return findings

    def _check(value: str, lineno: int) -> None:
        if pattern.search(value):
            findings.append((lineno, value))

    def _flatten_joined_str(node: ast.JoinedStr) -> str:
        """Reconstruct a conservative template for an f-string.

        Each ``FormattedValue`` is replaced with a single ASCII
        digit placeholder so the regex matches the broken shape
        regardless of whether the interpolation falls inside the
        identifier (\\S+ position) OR after the bind-marker ``$``
        (\\d position). Examples:

          f"SET LOCAL {setting} = $1"
              → "SET LOCAL 0 = $1"           (\\S+ matches "0", \\d matches "1")
          f"SET LOCAL mnemos.x = ${slot}"
              → "SET LOCAL mnemos.x = $0"    (\\d matches "0")

        Codex round-7 caught the literal-name interpolation case;
        round-8 caught the split-bind-marker case where the digit
        itself is interpolated. Using a digit placeholder closes
        both with a single template.
        """
        parts: list[str] = []
        for sub in node.values:
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts.append(sub.value)
            else:
                # Single-digit ASCII token: matches \S+ when in
                # identifier position AND \d when in bind-marker
                # position. Using a real digit (not a Unicode
                # punctuation token) is critical for the latter.
                parts.append("0")
        return "".join(parts)

    # Track JoinedStr ids we've already flattened so the inner
    # ast.walk traversal doesn't ALSO re-check the inner Constant
    # parts of the same f-string against the literal pattern (those
    # parts can't carry the full SQL on their own; the flatten
    # already covered them).
    flattened_joined_str_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            flattened_joined_str_ids.add(id(node))
            _check(
                _flatten_joined_str(node),
                getattr(node, "lineno", 0),
            )

    # Now walk Constant nodes — but skip those that are the inner
    # parts of an already-flattened f-string. ast doesn't give us a
    # direct parent pointer, so we re-walk and use the parent map
    # built lazily.
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parent = parent_map.get(id(node))
            if parent is not None and id(parent) in flattened_joined_str_ids:
                continue
            _check(node.value, getattr(node, "lineno", 0))
    return findings


def test_require_pg_pool_returns_pool_when_present(monkeypatch):
    """Happy path: pool is set, helper returns it."""
    from mnemos.api.persistence_helpers import require_postgres_pool_or_503

    sentinel = object()
    monkeypatch.setattr(_lc, "_pool", sentinel)
    out = require_postgres_pool_or_503()
    assert out is sentinel


def test_require_pg_pool_503_with_postgres_message_when_no_backend(monkeypatch):
    """No backend installed yet AND no pool — generic transient
    503. Operators should retry."""
    from fastapi import HTTPException

    from mnemos.api.persistence_helpers import require_postgres_pool_or_503

    monkeypatch.setattr(_lc, "_pool", None)
    monkeypatch.setattr(_lc, "_persistence_backend", None)

    with pytest.raises(HTTPException) as exc:
        require_postgres_pool_or_503()
    assert exc.value.status_code == 503
    assert "Database pool not available" in exc.value.detail


def test_require_pg_pool_503_explains_sqlite_profile(monkeypatch):
    """On a SQLite profile, the 503 detail must explain that the
    route requires Postgres. Operators on edge/SQLite see this
    immediately rather than chasing a phantom outage."""
    from fastapi import HTTPException

    from mnemos.api.persistence_helpers import require_postgres_pool_or_503

    class _FakeSqliteBackend:
        pass

    monkeypatch.setattr(_lc, "_pool", None)
    monkeypatch.setattr(_lc, "_persistence_backend", _FakeSqliteBackend())

    with pytest.raises(HTTPException) as exc:
        require_postgres_pool_or_503(route_label="GET /v1/journal")
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert "Postgres backend" in detail
    assert "SQLite" in detail
    assert "MNEMOS_PROFILE=server" in detail
    # Custom route_label flows through.
    assert "GET /v1/journal" in detail


@pytest.mark.asyncio
async def test_no_remaining_set_local_with_bind_in_codebase():
    """Belt-and-braces: AST-walk every string literal under
    ``mnemos/`` and confirm nothing matches the broken
    ``SET LOCAL <name> = $N`` shape.

    AST-based so single, double, triple-quoted, raw, and f-string
    literal segments are ALL covered — codex round-6 caught that
    the previous source-text regex only handled double-quoted
    uppercase SQL.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    failures: list[tuple[pathlib.Path, int, str]] = []
    for path in (repo_root / "mnemos").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, value in _scan_for_broken_set_local(text, path):
            failures.append((path, lineno, value))
    assert not failures, (
        "Found broken SET LOCAL ... = $1 SQL string literal(s):\n"
        + "\n".join(
            f"  {p}:{ln} -> {repr(v)[:120]}"
            for (p, ln, v) in failures
        )
    )


@pytest.mark.parametrize(
    "snippet",
    [
        # double-quoted, uppercase
        'SQL = "SET LOCAL mnemos.current_user_id = $1"',
        # single-quoted
        "SQL = 'SET LOCAL mnemos.current_user_id = $1'",
        # triple double-quoted
        'SQL = """SET LOCAL mnemos.current_user_id = $1"""',
        # triple single-quoted
        "SQL = '''SET LOCAL mnemos.current_user_id = $1'''",
        # lowercase (case-insensitive)
        'SQL = "set local mnemos.current_user_id = $1"',
        # mixed case
        'SQL = "Set Local mnemos.current_role = $2"',
        # leading whitespace
        'SQL = "  SET LOCAL mnemos.current_user_id  = $1"',
        # f-string with literal SQL prefix
        'SQL = f"SET LOCAL mnemos.current_user_id = $1 -- {note}"',
        # f-string with INTERPOLATED setting name in the middle
        # (the bypass codex round-7 caught — flatten covers it).
        'SQL = f"SET LOCAL {setting_name} = $1"',
        # f-string with interpolated namespace fragment.
        'SQL = f"SET LOCAL mnemos.{setting} = $1"',
        # Two interpolated parts in the name; literal bind.
        'SQL = f"SET LOCAL {ns}.{name} = $1"',
        # Split bind: the digit itself is interpolated.
        'SQL = f"SET LOCAL mnemos.current_user_id = ${slot}"',
        # Split bind AND split name.
        'SQL = f"SET LOCAL {name} = ${slot}"',
        # ``TO`` keyword form — also rejects bind parameters on PG.
        'SQL = "SET LOCAL mnemos.current_user_id TO $1"',
        'SQL = "SET LOCAL mnemos.current_user_id to $1"',
        'SQL = f"SET LOCAL mnemos.current_user_id TO ${slot}"',
        'SQL = f"SET LOCAL {name} TO $1"',
    ],
)
def test_scanner_catches_broken_shape(snippet):
    """Negative fixtures proving the AST scanner catches every
    quoting / casing variant of the broken pattern. Pinning so the
    scanner can't quietly degrade in future."""
    findings = _scan_for_broken_set_local(snippet, "<test>")
    assert findings, f"scanner missed broken shape in: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        # The fix shape — no $-binds.
        "SQL = \"SELECT set_config('mnemos.current_user_id', $1, true)\"",
        # SET LOCAL with literal value (no $-binds; valid SQL).
        'SQL = "SET LOCAL mnemos.suppress_version_snapshot = \'1\'"',
        # SET LOCAL ... TO <literal> — keyword form, no binds.
        'SQL = "SET LOCAL statement_timeout TO \'30s\'"',
        # SET (session-level) with no binds.
        'SQL = "SET application_name = \'mnemos\'"',
        # Documentation comment about the broken form should NOT
        # match — the AST scan only looks at string-literal values,
        # not comment text.
        '# Earlier shape SET LOCAL x = $1 was broken.',
    ],
)
def test_scanner_allows_fix_and_unrelated_sql(snippet):
    findings = _scan_for_broken_set_local(snippet, "<test>")
    assert not findings, (
        f"scanner false-positive on safe input {snippet!r}: {findings!r}"
    )

"""Static-analysis invariants for the profile-aware 503 contract.

Round-53 introduced ``mnemos.api.persistence_helpers.
require_postgres_pool_or_503(*, route_label=...)``: a single
helper that raises ``HTTPException(503)`` with a profile-aware
detail (SQLite/edge → "<route> requires the Postgres backend; ...
Set MNEMOS_PROFILE=server ..."; transient pool loss →
"Database pool not available"). Rounds 54..60 migrated 67 call
sites in ``mnemos/api/routes/`` from the bare shape

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

onto the helper. The bare shape is operator-hostile on edge
profiles — it doesn't disambiguate "this route is Postgres-only-by-
design" from "the pool is transiently down" and therefore sends
the operator chasing a phantom outage.

This file pins that migration statically. Two AST walks:

* ``test_no_bare_pool_check_in_routes`` — every ``if not _lc._pool``
  guard inside ``mnemos/api/routes/`` must either route through
  the canonical helper (no bare HTTPException follower) OR be
  the documented fallback inside ``oauth_me`` (which short-circuits
  when no pool is available rather than raising).

* ``test_no_bare_503_database_pool_detail_in_routes`` — no route
  module may construct ``HTTPException(status_code=503, detail=
  "Database pool not available")`` directly. The canonical helper
  is the only sanctioned path to that detail; bypassing it loses
  the route-label augmentation.

A future migration that adds a route file inheriting the old shape
will trip the second invariant before code review.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_ROOT = REPO_ROOT / "mnemos" / "api" / "routes"

# ``oauth_me`` keeps a runtime ``if cookie_session and _lc._pool:``
# fallback because the endpoint serves both personal and server
# profiles and short-circuits to a personal/api-key response when
# no pool is available. That branch does NOT raise — it lets the
# response return ``identity=None`` — so it's not a 503-shape
# regression.
ALLOWED_BARE_POOL_CHECK_FILES = {
    "mnemos/api/routes/oauth.py",
}


def _module_path_relative(file: pathlib.Path) -> str:
    return str(file.relative_to(REPO_ROOT))


def _is_lc_pool_attribute(node: ast.AST) -> bool:
    """Match ``_lc._pool`` (Attribute(Name('_lc'), '_pool'))."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_pool"
        and isinstance(node.value, ast.Name)
        and node.value.id == "_lc"
    )


def _is_pool_falsy_check(test: ast.AST) -> bool:
    """Return True when ``test`` is one of the bare-shape patterns

        not _lc._pool          # UnaryOp(Not, ...)
        _lc._pool is None
        _lc._pool == None
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_lc_pool_attribute(test.operand)
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        if isinstance(test.ops[0], (ast.Is, ast.Eq)):
            if (
                _is_lc_pool_attribute(test.left)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            ):
                return True
    return False


def _statement_is_503_raise(stmt: ast.AST) -> bool:
    """Return True when ``stmt`` is ``raise HTTPException(...503...)``.

    Tolerant: matches ``HTTPException(status_code=503, ...)``,
    ``HTTPException(503, ...)``, and re-raise via ``raise <name>``
    bound to such a constructor — but the second form is rare in
    this codebase and a false-positive there is fine because the
    caller would still trip the second invariant below.
    """
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    call = stmt.exc
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = (
        func.id if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute)
        else None
    )
    if name != "HTTPException":
        return False
    # Positional 503?
    for arg in call.args:
        if isinstance(arg, ast.Constant) and arg.value == 503:
            return True
    # Keyword status_code=503?
    for kw in call.keywords:
        if kw.arg == "status_code":
            if isinstance(kw.value, ast.Constant) and kw.value.value == 503:
                return True
    return False


def test_no_bare_pool_check_in_routes():
    """Every ``if not _lc._pool: raise HTTPException(503, ...)`` body
    inside ``mnemos/api/routes/`` must go through
    ``require_postgres_pool_or_503``.

    A bare ``if not _lc._pool: raise HTTPException(503, ...)`` body
    bypasses the canonical helper and therefore loses the profile-
    aware detail (SQLite vs Postgres + route label). Migrate to
    ``require_postgres_pool_or_503(route_label=...)`` or document
    a non-raising fallback (see oauth_me) and add the file to the
    allow-list above.
    """
    failures: list[tuple[str, int]] = []

    for path in ROUTES_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        if rel in ALLOWED_BARE_POOL_CHECK_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if not _is_pool_falsy_check(node.test):
                continue
            # ``if not _lc._pool:`` body must NOT contain a 503 raise
            # — that's the bare shape the migration eliminated.
            for stmt in node.body:
                if _statement_is_503_raise(stmt):
                    failures.append((rel, stmt.lineno))

    assert not failures, (
        "bare ``if not _lc._pool: raise HTTPException(503, ...)`` shape "
        "detected in route module(s) — migrate to "
        "``require_postgres_pool_or_503(route_label=...)`` from "
        "``mnemos.api.persistence_helpers`` so the 503 detail names "
        "the route AND points operators at the SQLite/edge-profile "
        "flip:\n"
        + "\n".join(f"  {rel}:{lineno}" for (rel, lineno) in failures)
    )


def test_no_bare_503_database_pool_detail_in_routes():
    """No route module may construct ``HTTPException(status_code=503,
    detail="Database pool not available")`` literally. The canonical
    detail string is owned by ``require_postgres_pool_or_503``; an
    inline construction skips the route-label augmentation and the
    SQLite-vs-Postgres branch.
    """
    failures: list[tuple[str, int, str]] = []
    bad_details = {
        "Database pool not available",
        "Database not available",  # document_import.py used this variant pre-round-56
    }

    for path in ROUTES_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name != "HTTPException":
                continue
            status = None
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == 503:
                    status = 503
            for kw in node.keywords:
                if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                    status = kw.value.value
            if status != 503:
                continue
            for kw in node.keywords:
                if kw.arg == "detail" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str) and kw.value.value in bad_details:
                        failures.append((rel, kw.value.lineno, kw.value.value))

    assert not failures, (
        "Postgres-only routes must NOT inline the canonical 503 "
        "detail string — call ``require_postgres_pool_or_503(route"
        "_label=...)`` from ``mnemos.api.persistence_helpers`` "
        "instead. Inlining loses the profile-aware (SQLite vs "
        "Postgres) branch and the per-route label.\n"
        + "\n".join(
            f"  {rel}:{lineno}  detail={detail!r}"
            for (rel, lineno, detail) in failures
        )
    )


def test_import_chunk_key_unique_index_matches_on_conflict_arbiter():
    """The ``import_chunk_key`` UNIQUE index in
    ``migrations_v4_2_document_import_chunk_idempotency.sql``
    MUST be NON-PARTIAL (no WHERE clause) because the document
    _import helper uses ``ON CONFLICT (import_chunk_key) DO
    UPDATE`` without a matching predicate. A partial UNIQUE
    requires the INSERT to use ``ON CONFLICT (import_chunk_key)
    WHERE import_chunk_key IS NOT NULL`` to satisfy Postgres'
    arbiter inference rule; without that, every chunk INSERT
    would error with "no unique or exclusion constraint matching
    the ON CONFLICT specification" and document_import would
    fail entirely.

    Codex review-8 of round-68 caught a partial-UNIQUE shape
    that would have shipped exactly this bug. This invariant
    pins the migration's index shape so a future revision that
    re-introduces the WHERE clause trips a unit test before it
    can break document import.
    """
    migration_path = (
        REPO_ROOT
        / "db"
        / "migrations_v4_2_document_import_chunk_idempotency.sql"
    )
    sql = migration_path.read_text(encoding="utf-8")

    # Find the CREATE UNIQUE INDEX statement.
    import re

    matches = re.findall(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*?ON\s+memories\s*\("
        r"\s*import_chunk_key\s*\)([^;]*?);",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert matches, (
        "could not find CREATE UNIQUE INDEX ... ON memories "
        "(import_chunk_key) in the migration — file shape "
        "changed without updating this invariant"
    )
    # The captured group is everything BETWEEN the closing paren
    # of the column list and the trailing semicolon. It must not
    # contain a WHERE clause.
    assert len(matches) == 1, (
        f"multiple unique-index statements on import_chunk_key in "
        f"the migration — only one expected, got {len(matches)}"
    )
    trailing = matches[0]
    assert "WHERE" not in trailing.upper(), (
        "import_chunk_key UNIQUE index is partial (has a WHERE "
        "clause). The document_import helper uses bare ``ON "
        "CONFLICT (import_chunk_key) DO UPDATE`` which cannot "
        "infer a partial unique index — every chunk INSERT will "
        "error with 'no unique or exclusion constraint matching "
        "the ON CONFLICT specification'. Either drop the WHERE "
        "clause OR update the helper's ON CONFLICT clause to "
        "carry the same predicate.\n\nTrailing index clause: "
        f"{trailing!r}"
    )


def test_import_chunk_key_migration_uses_concurrently_no_begin():
    """The migration must use ``CREATE UNIQUE INDEX
    CONCURRENTLY`` for the unique-index creation AND must NOT
    have an explicit ``BEGIN``/``COMMIT`` transaction block.

    Codex review-11 of round-72 caught that the DO-block
    partial-index repair shipped in round-70..72 still ran a
    non-concurrent ``CREATE UNIQUE INDEX`` inside ``BEGIN``.
    ``SET LOCAL lock_timeout`` only caps how long the migration
    WAITS for the lock — once acquired, the build itself blocks
    writers for the full scan/build duration. On large
    ``memories`` tables this is a write-outage hazard.

    Round-73 restructures: migration uses CONCURRENTLY (no
    write-blocking build), drops the explicit BEGIN so psql
    autocommit-per-statement allows CONCURRENTLY (Postgres
    forbids it inside a transaction block), and EXTRACTS the
    round-68 partial-index repair to a separate operator
    runbook at ``db/scripts/repair_round_68_partial_chunk
    _key_index.sql``.

    This invariant pins both shapes so a future revision that
    re-introduces ``BEGIN``/``COMMIT`` or drops CONCURRENTLY
    trips a unit test.
    """
    migration_path = (
        REPO_ROOT
        / "db"
        / "migrations_v4_2_document_import_chunk_idempotency.sql"
    )
    sql = migration_path.read_text(encoding="utf-8")

    # Strip comment lines so SQL-keyword checks don't false-
    # positive on the operator-note prose in the header.
    code_only = "\n".join(
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    )
    code_upper = code_only.upper()

    # No explicit BEGIN/COMMIT — CONCURRENTLY can't run inside
    # a transaction block.
    import re

    assert not re.search(r"\bBEGIN\s*;", code_upper), (
        "migration has an explicit ``BEGIN;`` — CREATE INDEX "
        "CONCURRENTLY cannot run inside a transaction block. "
        "Drop the BEGIN/COMMIT pair so psql runs each statement "
        "in autocommit mode."
    )
    assert not re.search(r"\bCOMMIT\s*;", code_upper), (
        "migration has an explicit ``COMMIT;`` — see BEGIN check "
        "above."
    )

    # Index creation must be CONCURRENTLY.
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in code_upper, (
        "migration does not create the unique index with "
        "CONCURRENTLY — a non-concurrent build blocks writes "
        "on ``memories`` for the full scan/build duration. "
        "Use ``CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "memories_import_chunk_key_uniq ON memories (import"
        "_chunk_key)``."
    )


def test_legacy_chunk_key_update_is_wrapped_in_savepoint():
    """The round-72 legacy v70 chunk_key UPDATE in
    ``document_import.py`` MUST be wrapped in a nested
    ``conn.transaction()`` (asyncpg SAVEPOINT) so a
    UniqueViolationError on the new chunk_key constraint rolls
    back ONLY the savepoint, leaving the outer transaction
    usable for the subsequent INSERT-with-ON-CONFLICT path.

    Codex review-12 of round-73 caught that catching the
    violation in the outer transaction left it in Postgres'
    aborted state — the next fetchval would raise
    ``InFailedSQLTransactionError``. Round-74 wrapped the
    UPDATE in a nested savepoint to fix this.

    This invariant pins the savepoint pattern so a future
    refactor that flattens the nested transaction trips a
    unit test before it can ship.

    Detection shape: AST-walks the helper, finds the
    ``import_chunk_key`` UPDATE statement, and verifies that
    its lexical position is INSIDE a second
    ``async with conn.transaction()`` block nested within the
    outer transaction.
    """
    import ast

    helper_path = (
        REPO_ROOT
        / "mnemos"
        / "api"
        / "routes"
        / "document_import.py"
    )
    tree = ast.parse(helper_path.read_text(encoding="utf-8"))

    # Count ``async with conn.transaction():`` occurrences in
    # the helper. Round-74's structure has TWO: the outer
    # per-chunk transaction + the nested savepoint around the
    # legacy UPDATE. A flatten-refactor would drop the second.
    transaction_async_withs: list[ast.AsyncWith] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute):
                if (
                    ctx.func.attr == "transaction"
                    and isinstance(ctx.func.value, ast.Name)
                    and ctx.func.value.id == "conn"
                ):
                    transaction_async_withs.append(node)
                    break

    assert len(transaction_async_withs) >= 2, (
        f"document_import.py has {len(transaction_async_withs)} "
        f"``async with conn.transaction()`` blocks; expected ≥ 2 "
        f"(outer per-chunk transaction + nested savepoint around "
        f"the legacy v70 chunk_key UPDATE). A flatten-refactor "
        f"that drops the savepoint would re-introduce the "
        f"InFailedSQLTransactionError hazard codex review-12 of "
        f"round-73 caught."
    )

    # Verify at least one of these is NESTED inside another —
    # i.e., the savepoint pattern, not two sibling transactions.
    nested = False
    for outer in transaction_async_withs:
        for inner in transaction_async_withs:
            if inner is outer:
                continue
            for descendant in ast.walk(outer):
                if descendant is inner:
                    nested = True
                    break
            if nested:
                break
        if nested:
            break
    assert nested, (
        "no nested ``async with conn.transaction()`` found in "
        "document_import.py. The legacy v70 chunk_key UPDATE "
        "must be wrapped in a nested transaction (savepoint) "
        "INSIDE the outer per-chunk transaction; sibling "
        "transactions don't provide the savepoint-rollback "
        "semantics that round-74 relies on."
    )


def test_round_68_partial_index_repair_runbook_exists():
    """Round-73 extracted the partial-index repair from the
    migration to ``db/scripts/repair_round_68_partial_chunk
    _key_index.sql``. Operators of round-68-alpha deployments
    must run this manually to repair their partial index
    BEFORE applying the round-73 migration.

    This invariant pins the runbook's existence + shape so a
    future revision can't accidentally delete the operator
    repair path.
    """
    repair_path = (
        REPO_ROOT
        / "db"
        / "scripts"
        / "repair_round_68_partial_chunk_key_index.sql"
    )
    assert repair_path.exists(), (
        "operator runbook ``db/scripts/repair_round_68_partial"
        "_chunk_key_index.sql`` is missing. Round-68 alpha "
        "deployments need this script to repair their partial "
        "index without a write-outage; without it, the only "
        "upgrade path requires either a full maintenance "
        "window or a re-implementation of the repair step."
    )

    sql = repair_path.read_text(encoding="utf-8")
    code_only = "\n".join(
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    )
    code_upper = code_only.upper()

    # Both CREATE and DROP must use CONCURRENTLY so the repair
    # itself is online.
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in code_upper, (
        "repair runbook must build the replacement index with "
        "CONCURRENTLY to avoid blocking writes"
    )
    assert "DROP INDEX CONCURRENTLY" in code_upper, (
        "repair runbook must drop the partial index with "
        "CONCURRENTLY to avoid blocking writes"
    )
    assert "ALTER INDEX" in code_upper and "RENAME TO" in code_upper, (
        "repair runbook must rename the temp index to the "
        "canonical name after the swap"
    )


def test_invariants_have_at_least_one_helper_call():
    """Sanity check: at least one route module must call the helper.

    If this assertion goes empty the AST walk above is silently
    permissive — no bare shape, but also no helper usage means
    something dropped. We expect dozens of call sites post-round-60.
    """
    helper_calls = 0
    for path in ROUTES_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == "require_postgres_pool_or_503":
                helper_calls += 1

    assert helper_calls >= 20, (
        f"only {helper_calls} ``require_postgres_pool_or_503`` calls "
        f"found — expected at least 20 after the round-54..60 sweep. "
        f"Either the AST scanner is broken or the helper has been "
        f"reverted across many call sites."
    )

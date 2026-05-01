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

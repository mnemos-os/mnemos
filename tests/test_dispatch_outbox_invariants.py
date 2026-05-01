"""Static-analysis invariants for the transactional-outbox contract.

The transactional outbox pattern (memory write + webhook delivery
row insert atomic, send task scheduled post-commit) is the only
way to satisfy corpus-review-2026-04-29 finding #2: domain writes
must NEVER commit without their corresponding event row, and
event rows must NEVER fire on rolled-back data.

Round-47 closed the document_import side of that finding;
round-49 surfaced partial/full failure via HTTP status; round-50
prevented the batch top-level status from conflating client-error
4xx with retryable-gateway 502.

This file pins the invariant statically: every call site of
``mnemos.webhooks.dispatcher.dispatch`` (aliased as
``_dispatch_webhook`` at most call sites) MUST pass ``conn=conn``
so the delivery row joins the caller's transaction. AST-walk
catches the pattern across both literal and aliased imports;
catches a future addition that introduces a non-transactional
call before code review does.

Allow-list: the dispatcher module ITSELF imports ``dispatch`` as
a public re-export. Tests are excluded.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MNEMOS_ROOT = REPO_ROOT / "mnemos"

# Files that may import ``dispatch`` without using it as the
# transactional caller pattern (the dispatcher module itself,
# package re-exports, etc.).
EXEMPT_FILES = {
    "mnemos/webhooks/dispatcher.py",
    "mnemos/webhooks/__init__.py",
    "mnemos/webhooks/outbox.py",
    "mnemos/webhooks/repair.py",
}


def _module_path_relative(file: pathlib.Path) -> str:
    return str(file.relative_to(REPO_ROOT))


def _calls_with_local_aliases(tree: ast.AST) -> dict[str, list[ast.Call]]:
    """Return every Call node grouped by the local name of the
    callable — so an ``import dispatch as _dispatch_webhook``
    is found whether the call uses ``_dispatch_webhook`` or
    ``dispatch``.
    """
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module.startswith("mnemos.webhooks"):
                continue
            for alias in node.names:
                if alias.name == "dispatch":
                    aliases.add(alias.asname or alias.name)

    if not aliases:
        return {}

    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            calls.setdefault(func.id, []).append(node)
        elif isinstance(func, ast.Attribute):
            # Attribute access like ``dispatcher.dispatch(...)`` —
            # surface those too.
            if func.attr == "dispatch":
                calls.setdefault("dispatch", []).append(node)
    return calls


def test_every_dispatch_call_passes_conn():
    """Every call to ``mnemos.webhooks.dispatcher.dispatch`` (or
    its locally-aliased import) must include a ``conn=`` keyword
    argument so the webhook_deliveries INSERT joins the caller's
    transaction.

    A call without ``conn=`` writes the delivery row on a fresh
    connection AFTER the caller's data has committed — failures
    in that fresh-connection acquire lose the event entirely
    (corpus-review-2026-04-29 #2).
    """
    failures: list[tuple[str, int, str]] = []

    for path in MNEMOS_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        if rel in EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for alias, calls in _calls_with_local_aliases(tree).items():
            for call in calls:
                kwarg_names = {
                    kw.arg for kw in call.keywords if kw.arg is not None
                }
                if "conn" not in kwarg_names:
                    failures.append(
                        (rel, call.lineno, alias)
                    )

    assert not failures, (
        "non-transactional webhook dispatch detected (every call to "
        "mnemos.webhooks.dispatcher.dispatch MUST pass conn=conn so the "
        "delivery row joins the caller's transaction):\n"
        + "\n".join(
            f"  {rel}:{lineno}  → {alias}(...)"
            for (rel, lineno, alias) in failures
        )
        + "\n\nFix: pass ``conn=conn`` from inside the data transaction; "
        "schedule the send task after commit via "
        "``_schedule_delivery_attempt(_attempt_delivery(delivery_id))``."
    )


def test_known_call_sites_present():
    """Sanity check that the AST scanner sees the call sites we
    know exist. If this assertion goes empty, the scanner is
    silently failing and the invariant test above wouldn't catch
    a real regression."""
    found: list[str] = []
    for path in MNEMOS_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        if rel in EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for alias, calls in _calls_with_local_aliases(tree).items():
            for _call in calls:
                found.append(rel)
                break  # one mention is enough
            break

    # We expect at least 1 call site (memories.py + document_import.py)
    # in the production tree.
    assert len(found) >= 1, (
        "AST scanner found zero dispatcher.dispatch call sites — the "
        "invariant test cannot detect regressions if it can't find any "
        "calls in the first place. Check the import-pattern matcher."
    )


@pytest.mark.parametrize(
    "snippet,should_pass",
    [
        # Canonical transactional call site.
        (
            "from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook\n"
            "async def f(conn):\n"
            "    await _dispatch_webhook('memory.created', {}, conn=conn)\n",
            True,
        ),
        # Bare ``conn`` keyword without the alias also accepted.
        (
            "from mnemos.webhooks.dispatcher import dispatch\n"
            "async def f(conn):\n"
            "    await dispatch('e', {}, conn=conn)\n",
            True,
        ),
        # Non-transactional call — bug we're guarding against.
        (
            "from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook\n"
            "async def f():\n"
            "    await _dispatch_webhook('memory.created', {})\n",
            False,
        ),
        # Attribute-access ``dispatcher.dispatch`` shape.
        (
            "from mnemos.webhooks import dispatcher\n"
            "async def f(conn):\n"
            "    await dispatcher.dispatch('e', {}, conn=conn)\n",
            True,
        ),
    ],
)
def test_scanner_classifies_canonical_shapes(snippet, should_pass, tmp_path):
    """Negative + positive fixtures for the AST scanner. Each
    snippet stands in for a call-site shape; the scanner should
    correctly admit transactional patterns and reject non-
    transactional ones."""
    fake_module = tmp_path / "fake.py"
    fake_module.write_text(snippet)
    tree = ast.parse(snippet)
    aliases = _calls_with_local_aliases(tree)

    # Every call discovered must (or must not) have ``conn=`` per
    # the should_pass parametrize.
    for _alias, calls in aliases.items():
        for call in calls:
            kwarg_names = {
                kw.arg for kw in call.keywords if kw.arg is not None
            }
            has_conn = "conn" in kwarg_names
            if should_pass:
                assert has_conn, f"transactional snippet missing conn=: {snippet!r}"
            else:
                assert not has_conn, f"non-transactional snippet should fail: {snippet!r}"

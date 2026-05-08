"""Slice #192: Audit-driven removal of 7 dead Python helpers + 2
orphan pytest fixtures. Surfaced by the deep cross-code codex
audit at HEAD `de13b51` (mem_1778221719390_8cb1ba in MNEMOS).

Removed helpers:

- ``ProviderResponse`` Pydantic class in ``mnemos/domain/models.py``
  (line ~438) — declared but never used as a `response_model=`.
- ``ProviderResponse`` ``@dataclass`` class in
  ``mnemos/domain/graeae/engine.py`` (line ~76) — declared but
  never instantiated. Live shape is ``ProviderQueryResponse``.
- ``ModelRecommendation`` Pydantic class in
  ``mnemos/domain/models.py`` (line ~483) — declared but never
  used. Live recommendation type is the dataclass at
  ``mnemos/persistence/types.py`` (re-exported via
  ``mnemos/persistence/__init__.py``).
- ``JournalEntry`` Pydantic class in
  ``mnemos/api/routes/journal.py`` — declared but never used as
  a route ``response_model=``. Routes return raw dict/list.
- ``_sha256_hex`` in ``mnemos/db/deletion_log.py`` — declared
  but never called. PostgreSQL `digest(..., 'sha256')` is the
  live hashing path inside the deletion-log SQL.
- ``_looks_like_sqlite_conn`` in ``mnemos/db/deletion_log.py`` —
  declared but never called inside the module nor imported.
  The duplicate in ``mnemos/db/mcp_audit_repo.py`` IS live.
- ``_row_get`` in ``mnemos/db/deletion_log.py`` — declared but
  never called inside the module.
- ``drain_routing_log_queue_for_tests`` in
  ``mnemos/domain/pantheon/routing_log.py`` — exported in
  ``__all__`` but no test/script imports or calls it. The
  ``__all__`` entry was also removed.

Removed fixtures:

- ``event_loop`` in ``tests/__init__.py`` — pytest only collects
  fixtures from ``conftest.py``, not from package ``__init__``,
  so this fixture was never exposed to any test.
- ``event_loop`` (session scope) in ``tests/test_e2e.py`` — no
  test in the file requested it as a parameter; pytest-asyncio
  (``mode=Mode.STRICT``) auto-manages the loop.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# Each row: (filename hint, name to scan).
REMOVED_HELPERS = (
    ("mnemos/domain/models.py", "ProviderResponse"),
    ("mnemos/domain/graeae/engine.py", "ProviderResponse"),
    ("mnemos/domain/models.py", "ModelRecommendation"),
    ("mnemos/api/routes/journal.py", "JournalEntry"),
    ("mnemos/db/deletion_log.py", "_sha256_hex"),
    ("mnemos/db/deletion_log.py", "_looks_like_sqlite_conn"),
    ("mnemos/db/deletion_log.py", "_row_get"),
    ("mnemos/domain/pantheon/routing_log.py",
     "drain_routing_log_queue_for_tests"),
)


@pytest.mark.parametrize("source_file,name", REMOVED_HELPERS)
def test_helper_definition_removed(source_file: str, name: str):
    """Each removed helper must stay absent from its origin
    module — `def name(`, `async def name(`, or `class name(`."""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / source_file).read_text()
    pattern = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\s*[\(:]",
        re.MULTILINE,
    )
    assert not pattern.search(src), (
        f"`{name}` was re-introduced in {source_file}. If it has "
        "a real caller now, also wire that in and remove this "
        "case from the regression list."
    )


@pytest.mark.parametrize("name", sorted({n for _, n in REMOVED_HELPERS}))
def test_no_external_callers_of_removed_helper(name: str):
    """No source file should import or call the removed name. The
    deletion-log internal helpers (`_row_get`, `_sha256_hex`,
    `_looks_like_sqlite_conn`) intentionally collide with private
    helpers in other modules (e.g. mcp_audit_repo.py also defines
    `_looks_like_sqlite_conn`). Scan only for usages — `name(`
    or `from ... import name` — across the corrected scope.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    # Some removed names have intentional duplicates that ARE live
    # in other modules — `ModelRecommendation` lives at
    # `mnemos/persistence/types.py`; `_looks_like_sqlite_conn` at
    # `mnemos/db/mcp_audit_repo.py`; `_row_get` is a generic
    # private-helper name with independent definitions across
    # several db/domain modules. The `definition_removed` test
    # already pins the specific origin-file removal; an
    # external-caller scan would false-positive on the live
    # duplicates. Skip them here.
    if name in {"ModelRecommendation", "_looks_like_sqlite_conn",
                "_row_get"}:
        return
    pattern = re.compile(
        rf"(?:from\s+\S+\s+import\s+(?:[^#\n]*\b){re.escape(name)}\b"
        rf"|\b{re.escape(name)}\s*\()",
    )
    offenders: list[str] = []
    surfaces: list[Path] = []
    for tree in ("mnemos", "tests"):
        base = repo / tree
        if base.exists():
            surfaces.extend(base.rglob("*.py"))
    extras: list[Path] = [repo / "deploy.sh", repo / "pyproject.toml"]
    if (repo / "scripts").exists():
        extras.extend((repo / "scripts").glob("*.py"))
        extras.extend((repo / "scripts").glob("*.sh"))
    if (repo / "systemd").exists():
        extras.extend((repo / "systemd").glob("*.service"))
    surfaces.extend(extras)
    for path in surfaces:
        if "__pycache__" in str(path):
            continue
        if path.resolve() == self_path:
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) reference the removed "
        f"`{name}`:\n  " + "\n  ".join(offenders)
    )


def test_routing_log_all_drops_drained_helper():
    """`__all__` in routing_log.py must not advertise the removed
    `drain_routing_log_queue_for_tests`."""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "mnemos" / "domain" / "pantheon"
           / "routing_log.py").read_text()
    assert '"drain_routing_log_queue_for_tests"' not in src, (
        "routing_log.py `__all__` re-introduced the removed "
        "`drain_routing_log_queue_for_tests` entry."
    )


def test_no_orphan_event_loop_fixture_in_tests_init():
    """`tests/__init__.py` must not re-introduce an `event_loop`
    fixture — fixtures in package __init__.py are not collected
    by pytest, so this would be silent dead code."""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "tests" / "__init__.py").read_text()
    assert "def event_loop" not in src, (
        "tests/__init__.py re-introduced an event_loop fixture. "
        "Move it to conftest.py if it's actually needed; pytest "
        "doesn't collect fixtures from __init__.py."
    )


def test_no_orphan_event_loop_fixture_in_test_e2e():
    """`tests/test_e2e.py`'s session-scope `event_loop` fixture
    was unused (no test parameterized on it). Pin its absence."""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "tests" / "test_e2e.py").read_text()
    assert "def event_loop" not in src, (
        "tests/test_e2e.py re-introduced the orphan event_loop "
        "session fixture. pytest-asyncio mode=STRICT manages the "
        "loop; if a custom one is needed, also wire a test that "
        "uses it as a parameter."
    )

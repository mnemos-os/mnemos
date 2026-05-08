"""Slice #196: pin doc module-path references to modules that
actually exist in the current package layout.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- ``docs/MEMORY_ARCHITECTURE.md:475`` named
  ``mnemos/api/lifecycle.py``. After the v4 restructure, lifecycle
  is split across ``mnemos/core/lifecycle.py`` (boot/shutdown +
  globals) and ``mnemos/api/lifecycle_hooks.py`` (FastAPI
  startup/shutdown hooks); ``add_middleware`` calls live in
  ``mnemos/api/main.py``.
- ``docs/OPERATIONS.md:841`` referenced
  ``mnemos/api/observability.py``; actual module is
  ``mnemos/core/observability.py``.
- ``docs/OBSERVABILITY.md:249`` referenced
  ``mnemos.api.lifecycle._cache``; the live cache global is
  ``mnemos.core.lifecycle._cache``.
- ``docs/OBSERVABILITY.md:251`` named
  ``mnemos.domain.graeae.providers`` (no such module). The actual
  provider-pool surface lives at
  ``mnemos.domain.graeae.provider_worker`` +
  ``mnemos.domain.graeae.provider_sync``.
- ``docs/MEMORY_EXPORT_FORMAT.md:594`` referenced
  ``mnemos.mpf``; portability code lives under
  ``mnemos.domain.portability``. Also fixed
  ``tools/mpf_dump.py`` / ``tools/mpf_load.py`` references to
  the live ``mnemos/tools/memory_export.py`` /
  ``mnemos/tools/memory_import.py`` /
  ``mnemos/tools/mpf_validate.py``.
- ``docs/V3_5_CHARTER.md:328`` + ``V3_6_CHARTER.md:141`` use
  ``python3 -m mnemos.iris.server``; that module was never
  implemented. Added a "historical" note next to each block
  pointing readers at the live MCP model tools.
- ``DOCUMENT_IMPORT_GUIDE.md:339-340`` linked to
  ``./API.md#memories`` and ``./SEMANTIC_SEARCH.md``; neither
  file exists. Replaced with ``API_DOCUMENTATION.md`` (root)
  and ``docs/SPECIFICATION.md``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# Tuples of (module-path string that MUST NOT appear in operator
# docs, the live replacement so failure messages are useful).
_FORBIDDEN_PATHS = (
    ("mnemos/api/lifecycle.py",
     "mnemos/core/lifecycle.py + mnemos/api/lifecycle_hooks.py"),
    ("mnemos.api.lifecycle._cache",
     "mnemos.core.lifecycle._cache"),
    ("mnemos/api/observability.py",
     "mnemos/core/observability.py"),
    ("mnemos.api.observability",
     "mnemos.core.observability"),
    ("mnemos.domain.graeae.providers",
     "mnemos.domain.graeae.provider_worker / provider_sync"),
)


# Path strings that should NEVER appear as PREFIX-anchored
# substrings in operator docs (i.e. without a `mnemos/` qualifier).
# Codex round-1 of #196 caught `api/observability.py` and
# `api/auth.py` outside the canonical `mnemos/api/...` form;
# those are the OLD pre-restructure paths that no longer exist.
# We match these with a leading non-word boundary so the still-
# allowed `mnemos/api/...` qualified form does NOT trip the test.
_FORBIDDEN_UNQUALIFIED_PREFIXES = (
    ("api/observability.py",
     "mnemos/core/observability.py"),
    ("api/auth.py",
     "mnemos/api/dependencies.py (get_current_user dependency)"),
)

_DOC_SCAN_ROOTS = (
    REPO / "docs",
    REPO,  # root-level *.md
)


def _scan_md_files() -> list[Path]:
    out: list[Path] = []
    out.extend((REPO / "docs").rglob("*.md")) if (REPO / "docs").exists() else None
    out.extend(REPO.glob("*.md"))
    return out


@pytest.mark.parametrize("forbidden,replacement", _FORBIDDEN_PATHS)
def test_no_forbidden_module_path_in_docs(forbidden: str,
                                          replacement: str):
    """No operator/architecture doc should name the removed/moved
    module path. CHANGELOG.md is allowlisted — it intentionally
    names modules in historical removal/restructure entries."""
    self_path = Path(__file__).resolve()
    allowlist = {(REPO / "CHANGELOG.md").resolve()}
    pattern = re.compile(r"\b" + re.escape(forbidden) + r"\b")
    bad: list[str] = []
    for md in _scan_md_files():
        if md.resolve() in allowlist or md.resolve() == self_path:
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            if pattern.search(line):
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: "
                    f"{line.strip()[:80]}"
                )
    assert not bad, (
        f"{len(bad)} doc(s) reference removed/moved path "
        f"`{forbidden}`. Use `{replacement}` instead:\n"
        + "\n".join(bad)
    )


@pytest.mark.parametrize("forbidden,replacement",
                         _FORBIDDEN_UNQUALIFIED_PREFIXES)
def test_no_unqualified_pre_restructure_path(forbidden: str,
                                             replacement: str):
    """Operator docs occasionally name `api/foo.py` (without the
    leading `mnemos/`) — that's the pre-v4-restructure path
    shape. Match the unqualified form by requiring the character
    before `api/` to NOT be alphanumeric (so `mnemos/api/` is
    NOT matched, but `\nin api/observability.py` IS). CHANGELOG
    is allowlisted for historical narrative."""
    self_path = Path(__file__).resolve()
    allowlist = {(REPO / "CHANGELOG.md").resolve()}
    pattern = re.compile(r"(?:^|[^A-Za-z0-9_/])" + re.escape(forbidden))
    bad: list[str] = []
    for md in _scan_md_files():
        if md.resolve() in allowlist or md.resolve() == self_path:
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            if pattern.search(line):
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: "
                    f"{line.strip()[:80]}"
                )
    assert not bad, (
        f"{len(bad)} doc(s) reference unqualified pre-restructure "
        f"path `{forbidden}`. Use `{replacement}` instead:\n"
        + "\n".join(bad)
    )


def test_no_mnemos_mpf_module_reference():
    """The shared `mnemos.mpf` module never existed. Live
    portability code lives under ``mnemos.domain.portability``.
    """
    self_path = Path(__file__).resolve()
    allowlist = {(REPO / "CHANGELOG.md").resolve()}
    pattern = re.compile(r"\bmnemos\.mpf\b")
    bad: list[str] = []
    for md in _scan_md_files():
        if md.resolve() in allowlist or md.resolve() == self_path:
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            if pattern.search(line):
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: "
                    f"{line.strip()[:80]}"
                )
    assert not bad, (
        f"{len(bad)} doc(s) reference non-existent `mnemos.mpf` "
        f"module. Use `mnemos.domain.portability` instead:\n"
        + "\n".join(bad)
    )


def test_referenced_modules_actually_exist():
    """The live module paths the docs now point at must exist."""
    must_exist = [
        REPO / "mnemos" / "core" / "lifecycle.py",
        REPO / "mnemos" / "api" / "lifecycle_hooks.py",
        REPO / "mnemos" / "core" / "observability.py",
        REPO / "mnemos" / "domain" / "portability" / "__init__.py",
        REPO / "mnemos" / "domain" / "graeae" / "provider_worker.py",
        REPO / "mnemos" / "domain" / "graeae" / "provider_sync.py",
        REPO / "mnemos" / "tools" / "memory_export.py",
        REPO / "mnemos" / "tools" / "memory_import.py",
        REPO / "mnemos" / "tools" / "mpf_validate.py",
        REPO / "mnemos" / "mcp" / "tools" / "models.py",
        REPO / "API_DOCUMENTATION.md",
        REPO / "docs" / "SPECIFICATION.md",
    ]
    missing = [str(p.relative_to(REPO)) for p in must_exist
               if not p.exists()]
    assert not missing, (
        f"{len(missing)} doc-referenced path(s) no longer exist:\n  "
        + "\n  ".join(missing)
    )


def test_iris_module_marked_historical_in_charters():
    """The two charter docs that show `mnemos.iris.server` in a
    config snippet must accompany it with a "historical" note —
    the module was never implemented. Pin the note's presence
    so a future copy-paste doesn't regress to silently advertising
    a non-existent module."""
    for charter in ("V3_5_CHARTER.md", "V3_6_CHARTER.md"):
        src = (REPO / "docs" / charter).read_text()
        if "mnemos.iris.server" not in src:
            continue
        assert "never implemented" in src, (
            f"docs/{charter} references `mnemos.iris.server` "
            "without the historical-note callout. Add a note "
            "saying the module was never implemented and point "
            "at mnemos/mcp/tools/models.py instead."
        )

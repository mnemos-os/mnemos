"""Slice #191: ``mnemos/domain/memory_categorization/tiers.py`` and
its entire tier API were dead — re-exported but never imported by
any caller after the #188 removal of ``JournalManager`` +
``TierSelector`` (which were the only modules that cared about the
hot/warm/cold/archive tier model).

Removed:

- ``mnemos/domain/memory_categorization/tiers.py`` (127 lines):
  ``MemoryTier`` dataclass, ``TIER_1`` / ``TIER_2`` / ``TIER_3``
  / ``TIER_4`` instances, ``TIERS`` registry, ``TIER_NAMES``,
  ``get_tier``, ``get_tier_by_name``, ``list_tiers``.

The package `__init__.py` was also slimmed: it now only exports
``EntityManager`` + ``StateManager`` — the two classes with live
callers (tests + ``mnemos/api/routes/state.py``).

The README claim at line 641-643 ("The mnemos/domain/
memory_categorization package still exposes a hot/warm/cold/
archive selector for hook-side prompt budgeting") was also dropped:
hooks were removed in #182, the selector itself had no consumers,
and the README claim painted a feature that didn't actually exist.

If a future caller needs a tier-style memory budgeting model,
build it deliberately and wire it into a live integration; the
old declarative-only definitions weren't worth preserving as a
shim.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REMOVED_NAMES = (
    "MemoryTier",
    "TIERS",
    "TIER_NAMES",
    "TIER_1",
    "TIER_2",
    "TIER_3",
    "TIER_4",
    "get_tier",
    "get_tier_by_name",
    "list_tiers",
)


def test_tiers_module_does_not_exist():
    """The tiers.py file must stay removed."""
    repo = Path(__file__).resolve().parents[1]
    target = repo / "mnemos" / "domain" / "memory_categorization" / "tiers.py"
    assert not target.exists(), (
        f"tiers.py was re-created at {target}. If a real tier "
        "API is needed, wire it into a live caller before "
        "removing this guard."
    )


def test_tiers_not_in_categorization_all():
    """Package `__init__.py` must not advertise the dead tier API."""
    repo = Path(__file__).resolve().parents[1]
    init = (repo / "mnemos" / "domain" / "memory_categorization"
            / "__init__.py").read_text()
    for name in REMOVED_NAMES:
        assert f'"{name}"' not in init, (
            f"`__all__` in memory_categorization/__init__.py still "
            f"lists `{name}`; remove the entry."
        )
    # Also pin no ``from .tiers`` import line.
    assert "from .tiers" not in init, (
        "memory_categorization/__init__.py re-introduced "
        "`from .tiers import ...`; remove the import."
    )


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_no_imports_of_removed_tier_name(name: str):
    """Full-scope scan: mnemos/, tests/, scripts/*.py, scripts/*.sh,
    systemd/*.service, deploy.sh, pyproject.toml.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    pattern = re.compile(
        rf"(?:from\s+\S+\s+import\s+(?:[^#\n]*\b){re.escape(name)}\b"
        rf"|\bimport\s+\S+\s+as\s+{re.escape(name)}\b"
        rf"|\b{re.escape(name)}\s*\()",
    )
    for tree in ("mnemos", "tests"):
        base = repo / tree
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            if path.resolve() == self_path:
                continue
            src = path.read_text()
            if pattern.search(src):
                offenders.append(str(path.relative_to(repo)))
    extras: list[Path] = [repo / "deploy.sh", repo / "pyproject.toml"]
    if (repo / "scripts").exists():
        extras.extend((repo / "scripts").glob("*.py"))
        extras.extend((repo / "scripts").glob("*.sh"))
    if (repo / "systemd").exists():
        extras.extend((repo / "systemd").glob("*.service"))
    for path in extras:
        if not path.exists():
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) reference the removed "
        f"`{name}`:\n  " + "\n  ".join(offenders)
    )


def test_no_module_import_of_dead_tiers_path():
    """Pin that no source file uses ``import
    mnemos.domain.memory_categorization.tiers`` (full module-path
    form). A ``from X.tiers import Y`` form is already covered by
    the named-import scan above, but the bare ``import X.tiers``
    form would produce a runtime ImportError now that tiers.py is
    gone — pin it explicitly so a re-introduction is caught at
    test time rather than at startup. Codex round-1 of #191
    flagged this as a regression-test scope gap.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    pattern = re.compile(
        r"\bimport\s+mnemos\.domain\.memory_categorization\.tiers\b"
    )
    offenders: list[str] = []
    surfaces: list[Path] = []
    for tree in ("mnemos", "tests"):
        base = repo / tree
        if base.exists():
            surfaces.extend(base.rglob("*.py"))
    if (repo / "scripts").exists():
        surfaces.extend((repo / "scripts").glob("*.py"))
    for path in surfaces:
        if "__pycache__" in str(path) or path.resolve() == self_path:
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) still use `import "
        f"mnemos.domain.memory_categorization.tiers`:\n  "
        + "\n  ".join(offenders)
    )


def test_readme_does_not_advertise_removed_tier_selector():
    """The README claim at line ~641-643 about "hot/warm/cold/
    archive selector for hook-side prompt budgeting" was dropped
    in this slice. Hooks are gone (#182), the selector had no
    consumers, and the claim painted a feature that didn't exist.
    """
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "README.md").read_text()
    bad_phrases = [
        "hot/warm/cold/archive selector",
        "hook-side prompt budgeting",
    ]
    for phrase in bad_phrases:
        assert phrase not in src, (
            f"README still advertises the removed tier API: "
            f"`{phrase}` was re-introduced. The selector is gone "
            "and hooks were removed in #182."
        )

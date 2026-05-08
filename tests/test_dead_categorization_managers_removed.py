"""Slice #188: ``JournalManager`` (mnemos/domain/memory_categorization/
journal.py, 224 lines) and ``TierSelector`` (.../tier_selector.py,
156 lines) were entire-class-dead since v4.0 A.1 (commit 72508a5).

Both were re-exported from
``mnemos/domain/memory_categorization/__init__.py``'s ``__all__`` but
never imported by any other module: full-scope grep across mnemos/,
tests/, docs/, scripts/, systemd/, deploy.sh, and pyproject.toml
returned zero hits.

Sibling ``EntityManager`` (entities.py) and ``StateManager``
(state.py) ARE imported (tests/test_entity_manager.py and
tests/test_state_manager_durability.py) and remain.

Pin both removals so a future merge doesn't re-introduce the dead
prototypes via __all__ resurrection.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REMOVED_FILES = (
    "mnemos/domain/memory_categorization/journal.py",
    "mnemos/domain/memory_categorization/tier_selector.py",
)

REMOVED_NAMES = ("JournalManager", "TierSelector")


@pytest.mark.parametrize("relpath", REMOVED_FILES)
def test_dead_categorization_module_does_not_exist(relpath: str):
    """Removed files must stay removed."""
    repo = Path(__file__).resolve().parents[1]
    target = repo / relpath
    assert not target.exists(), (
        f"{relpath} was re-created at {target}. If the class is "
        "genuinely needed, also wire it into a live caller before "
        "removing this guard."
    )


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_removed_class_not_in_categorization_all(name: str):
    """The package's `__all__` must not advertise the removed
    classes — would cause `from ... import *` to fail at runtime
    even though no static caller would notice."""
    repo = Path(__file__).resolve().parents[1]
    init = (repo / "mnemos" / "domain" / "memory_categorization"
            / "__init__.py").read_text()
    assert f'"{name}"' not in init, (
        f"`__all__` in memory_categorization/__init__.py still "
        f"lists `{name}`; remove the entry."
    )


def test_no_wildcard_imports_from_categorization_package():
    """Wildcard imports (`from mnemos.domain.memory_categorization
    import *`) would silently re-expose any name re-introduced into
    the package's `__all__`. Pin that no source file uses the
    wildcard form, so the named-import regex in
    `test_no_imports_of_removed_class` remains sufficient.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    pattern = re.compile(
        r"from\s+mnemos\.domain\.memory_categorization\s+import\s+\*",
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
    extras: list[Path] = [repo / "deploy.sh"]
    if (repo / "scripts").exists():
        extras.extend((repo / "scripts").glob("*.py"))
    for path in extras:
        if not path.exists():
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) use wildcard import from "
        f"the categorization package — would silently re-expose any "
        f"removed class re-added to `__all__`:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_no_imports_of_removed_class(name: str):
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

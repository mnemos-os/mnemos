"""Slice #189: ``GraeaeEngine._query_provider`` was a thin pass-
through wrapper over ``_call_provider_worker`` with no callers.

The 3 real call sites in ``mnemos/domain/graeae/engine.py``
(line ~695 in initiate_consultation, line ~1046 in async-fan-out,
line ~1132 in retry path) all invoke ``_call_provider_worker``
directly. The wrapper added a function-call layer with no
behavior of its own.

Pin the removal so a future merge doesn't accidentally bring it
back, and confirm no surface (mnemos/, tests/, docs/, scripts/,
systemd/, deploy.sh, pyproject.toml, MQ_INTEGRATION.md) names
the dead method.
"""
from __future__ import annotations

import re
from pathlib import Path

REMOVED_NAME = "_query_provider"


def test_removed_method_not_in_engine():
    """The wrapper definition must stay removed."""
    repo = Path(__file__).resolve().parents[1]
    engine = repo / "mnemos" / "domain" / "graeae" / "engine.py"
    src = engine.read_text()
    pattern = re.compile(rf"^\s*async\s+def\s+{re.escape(REMOVED_NAME)}\s*\(",
                         re.MULTILINE)
    assert not pattern.search(src), (
        f"`{REMOVED_NAME}` was re-added to {engine}. The 3 real "
        "call sites use `_call_provider_worker` directly; the "
        "wrapper added no behavior."
    )


def test_no_references_to_removed_method():
    """No source file should reference `_query_provider` (calls or
    docstring/comment mentions). Stale references rot fast — the
    method that previously had this shape is `_call_provider_worker`.

    `engine.py` itself is allowed to keep the slice-marker comment
    (`# #189: removed _query_provider — ...`) per the project's
    removal-marker convention — it's the only file where the
    method ever lived.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    allowlist = {
        repo / "mnemos" / "domain" / "graeae" / "engine.py",
        # CHANGELOG.md describes this very removal under the slice
        # heading; it would be a regression doorway only if the
        # name were re-introduced as code, which the engine.py
        # comment-only guard already prevents.
        repo / "CHANGELOG.md",
    }
    offenders: list[str] = []
    pattern = re.compile(rf"\b{re.escape(REMOVED_NAME)}\b")
    surfaces: list[Path] = []
    for tree in ("mnemos", "tests", "docs"):
        base = repo / tree
        if base.exists():
            surfaces.extend(base.rglob("*.py"))
            surfaces.extend(base.rglob("*.md"))
    # Repo-root docs (ROADMAP.md, CHANGELOG.md, etc.) are also
    # frequent stale-reference homes — codex round-1 of #189
    # caught a leftover in ROADMAP.md.
    surfaces.extend(repo.glob("*.md"))
    extras: list[Path] = [repo / "deploy.sh", repo / "pyproject.toml"]
    if (repo / "scripts").exists():
        extras.extend((repo / "scripts").glob("*.py"))
        extras.extend((repo / "scripts").glob("*.sh"))
    if (repo / "systemd").exists():
        extras.extend((repo / "systemd").glob("*.service"))
    surfaces.extend(extras)
    for path in surfaces:
        if not path.exists() or "__pycache__" in str(path):
            continue
        if path.resolve() == self_path:
            continue
        if path.resolve() in {p.resolve() for p in allowlist}:
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) still reference the "
        f"removed `{REMOVED_NAME}`:\n  " + "\n  ".join(offenders)
    )


def test_engine_py_only_references_method_in_removal_marker():
    """The allowlist exception above (engine.py keeps a slice-marker
    comment) must NOT become a regression doorway. Pin that any
    `_query_provider` mention in engine.py is inside a `#`-prefixed
    comment line — never an executable identifier (def, await self.,
    etc.).
    """
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "mnemos" / "domain" / "graeae" / "engine.py").read_text()
    bad: list[str] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if REMOVED_NAME not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        bad.append(f"  engine.py:{lineno}: {line.rstrip()}")
    assert not bad, (
        f"`{REMOVED_NAME}` must only appear in slice-marker comments "
        f"in engine.py. Found executable references:\n"
        + "\n".join(bad)
    )

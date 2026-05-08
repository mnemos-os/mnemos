"""Slice #190: Four dead compatibility / placeholder modules with
zero imports anywhere. Each was either a thin re-export over the
canonical location (mnemos.core.resilience) or an empty placeholder
for never-shipped work.

Removed:

- ``mnemos/db/repositories.py`` — 1-line empty placeholder docstring
  ("Repository placeholders for future SQL extraction work."). No
  symbols, no callers.
- ``mnemos/domain/graeae/_concurrency.py`` — 10-line shim that
  re-exported ``ConcurrencyLimiterPool`` /
  ``ProviderConcurrencyLimiter`` from ``mnemos.core.resilience``.
- ``mnemos/domain/graeae/_circuit_breaker.py`` — 11-line shim that
  re-exported ``CircuitBreaker`` / ``CircuitBreakerPool`` /
  ``CircuitState`` from ``mnemos.core.resilience``.
- ``mnemos/domain/graeae/_rate_limiter.py`` — 10-line shim that
  re-exported ``RateLimiter`` / ``RateLimiterPool`` from
  ``mnemos.core.resilience``.

Live callers (``engine.py``, ``_cache.py``, ``_quality.py``,
``tests/test_resilience.py``) import from
``mnemos.core.resilience`` directly. The sibling ``_cache.py``
and ``_quality.py`` modules ARE used (engine.py:62-63 and
test_graeae_redis_state.py); only these 3 graeae shims +
the empty repositories placeholder were dead.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REMOVED_FILES = (
    "mnemos/db/repositories.py",
    "mnemos/domain/graeae/_concurrency.py",
    "mnemos/domain/graeae/_circuit_breaker.py",
    "mnemos/domain/graeae/_rate_limiter.py",
)

REMOVED_MODULES = tuple(
    f.replace("/", ".")[:-len(".py")] for f in REMOVED_FILES
)


@pytest.mark.parametrize("relpath", REMOVED_FILES)
def test_dead_module_does_not_exist(relpath: str):
    """Removed files must stay removed."""
    repo = Path(__file__).resolve().parents[1]
    target = repo / relpath
    assert not target.exists(), (
        f"{relpath} was re-created at {target}. If a real "
        "implementation lives here, also wire it into a live "
        "caller (or import the canonical name from "
        "`mnemos.core.resilience` directly)."
    )


@pytest.mark.parametrize("dotted", REMOVED_MODULES)
def test_no_imports_of_removed_module(dotted: str):
    """No source file should import the removed module — full or
    relative form."""
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    parts = dotted.split(".")
    last = parts[-1]
    parent = ".".join(parts[:-1])
    patterns = [
        re.compile(rf"from\s+{re.escape(dotted)}\b"),
        re.compile(rf"import\s+{re.escape(dotted)}\b"),
        re.compile(rf"from\s+{re.escape(parent)}\s+import\s+[^#\n]*\b{re.escape(last)}\b"),
        re.compile(rf"from\s+\.\s+import\s+[^#\n]*\b{re.escape(last)}\b"),
        re.compile(rf"from\s+\.{re.escape(last)}\s+import"),
    ]
    offenders: list[str] = []
    surfaces: list[Path] = []
    for tree in ("mnemos", "tests"):
        base = repo / tree
        if base.exists():
            surfaces.extend(base.rglob("*.py"))
    if (repo / "scripts").exists():
        surfaces.extend((repo / "scripts").glob("*.py"))
    for path in surfaces:
        if "__pycache__" in str(path):
            continue
        if path.resolve() == self_path:
            continue
        src = path.read_text()
        if any(p.search(src) for p in patterns):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) still import the removed "
        f"`{dotted}`:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("relpath", REMOVED_FILES)
def test_no_doc_references_to_removed_module_path(relpath: str):
    """Doc/markdown surfaces frequently keep stale module-path
    references after a removal (codex round-1 of #190 caught
    `docs/GRAEAE_FEATURES.md` listing all 3 removed graeae shims
    as the "Current module" for their respective resilience
    capabilities). Pin the full POSIX path string so any future
    re-doc-drift trips the test.

    Scope: docs/**/*.md, repo-root *.md, deploy.sh, scripts/*.sh,
    systemd/*.service, pyproject.toml.
    """
    repo = Path(__file__).resolve().parents[1]
    pattern = re.compile(re.escape(relpath))
    offenders: list[str] = []
    surfaces: list[Path] = []
    docs = repo / "docs"
    if docs.exists():
        surfaces.extend(docs.rglob("*.md"))
    surfaces.extend(repo.glob("*.md"))
    for shell in (repo / "deploy.sh", repo / "pyproject.toml"):
        if shell.exists():
            surfaces.append(shell)
    if (repo / "scripts").exists():
        surfaces.extend((repo / "scripts").glob("*.sh"))
    if (repo / "systemd").exists():
        surfaces.extend((repo / "systemd").glob("*.service"))
    # CHANGELOG.md is allowlisted: the slice description names the
    # removed paths under the slice heading per project convention.
    allowlist = {(repo / "CHANGELOG.md").resolve()}
    for path in surfaces:
        if not path.exists():
            continue
        if path.resolve() in allowlist:
            continue
        src = path.read_text()
        if pattern.search(src):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} doc/config file(s) still name the "
        f"removed module path `{relpath}`:\n  "
        + "\n  ".join(offenders)
    )

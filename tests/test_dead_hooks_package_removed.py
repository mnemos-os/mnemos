"""Slice #182: ``mnemos/hooks/`` package was dead since v4.0
(no imports anywhere in mnemos/, tests/, or docs/). Removed.

Pin the removal so a future merge doesn't accidentally bring it
back without wiring it into a real registration path.

If you intentionally need a hook system in the future, build it
deliberately rather than recovering this dead variant — by the
time you re-introduce it, the patterns it used will likely be
out of date with the rest of the codebase.
"""
from __future__ import annotations

from pathlib import Path


def test_hooks_package_directory_does_not_exist():
    """The mnemos/hooks/ directory must stay removed."""
    repo = Path(__file__).resolve().parents[1]
    hooks_dir = repo / "mnemos" / "hooks"
    assert not hooks_dir.exists(), (
        f"mnemos/hooks/ directory was re-created at {hooks_dir}. "
        "If this is intentional, also wire the package into a real "
        "registration path (FastAPI lifespan, MCP transport, etc.) "
        "and remove this guard."
    )


def test_hooks_package_not_listed_in_pyproject():
    """pyproject.toml's `setuptools.packages.find` list must not
    include `mnemos.hooks`."""
    repo = Path(__file__).resolve().parents[1]
    pyproject = (repo / "pyproject.toml").read_text()
    assert '"mnemos.hooks"' not in pyproject, (
        "pyproject.toml's package list still includes "
        "`\"mnemos.hooks\"`. Remove that entry — the directory is "
        "gone and the build will fail otherwise."
    )


def test_no_imports_from_dead_hooks_package():
    """No source file in mnemos/, tests/, or top-level shell scripts
    (deploy.sh, scripts/*) should import from mnemos.hooks. The
    package is gone. Defensive — catches a merge that re-introduces
    a stale import.

    Round-2 of #182 extended this from `*.py` only to also include
    shell scripts that import via `python -c`. Codex caught
    `deploy.sh` line 242 which had `python -c "from mnemos.hooks
    import HookRegistry"` as a deployment-time smoke check; the
    Python-only scan missed it.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    # Match real Python imports, not docstring mentions. The
    # interesting patterns are line-start-ish; require leading
    # whitespace + the keyword.
    import re
    pattern = re.compile(r"(?:from|import)\s+mnemos\.hooks", re.MULTILINE)
    # .py files under mnemos/ and tests/
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
    # Top-level shell scripts that may use `python -c`.
    for shell_path in [repo / "deploy.sh"] + list(
        (repo / "scripts").glob("*.sh") if (repo / "scripts").exists() else []
    ):
        if not shell_path.exists():
            continue
        src = shell_path.read_text()
        if pattern.search(src):
            offenders.append(str(shell_path.relative_to(repo)))
    assert not offenders, (
        f"{len(offenders)} source file(s) import from the removed "
        f"mnemos.hooks package:\n  " + "\n  ".join(offenders)
    )

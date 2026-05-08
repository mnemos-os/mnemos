"""Slice #187: ``pgvector_installed`` (mnemos/installer/db.py) and
``service_status`` (mnemos/installer/service.py) were defined but
never called.

Verified across the FULL live-entrypoint surface — corrected from
the #186 blind spot:

- mnemos/, tests/, docs/
- scripts/*.py (cron + systemd + manual operator scripts)
- scripts/*.sh, deploy.sh
- systemd/*.service ExecStart= lines
- pyproject.toml [project.scripts] console_scripts

Installer ``__main__`` imports:
- ``run_migrations``, ``setup_database``, ``setup_sqlite_database``,
  ``create_api_key``, ``verify_connection`` from ``.db`` —
  but NOT ``pgvector_installed``.
- ``create_service_user``, ``enable_service``, ``install_launchd``,
  ``install_systemd``, ``start_service`` from ``.service`` —
  but NOT ``service_status``.

``_which_exists`` (db.py-side helper called only by
``service_status``) is also removed in this slice.

If a future operator workflow genuinely needs a probe variant
(install-time gating, post-install status), build it deliberately
and wire it into a live entrypoint rather than recovering this
dead code.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REMOVED_NAMES = (
    "pgvector_installed",
    "service_status",
    "_which_exists",
)


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_removed_installer_helper_not_in_module(name: str):
    """Targeted import probes — these names must not exist in the
    installer modules. A `def <name>` line would fail this test if
    re-introduced without removing this guard.
    """
    repo = Path(__file__).resolve().parents[1]
    candidates = [
        repo / "mnemos" / "installer" / "db.py",
        repo / "mnemos" / "installer" / "service.py",
    ]
    pattern = re.compile(rf"^\s*def\s+{re.escape(name)}\s*\(", re.MULTILINE)
    offenders: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"`{name}` was re-introduced in: {offenders}. If this is "
        "intentional, also wire it into a live entrypoint and "
        "remove the corresponding case from this test."
    )


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_no_imports_of_removed_helper(name: str):
    """No source file should import the removed helper. Scope
    matches the corrected #186-onwards scan: mnemos/, tests/,
    scripts/*.py, scripts/*.sh, deploy.sh, systemd/*.service,
    pyproject.toml.
    """
    repo = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    pattern = re.compile(
        rf"(?:from\s+\S+\s+import\s+(?:[^#\n]*\b){re.escape(name)}\b"
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
        f"helper `{name}`:\n  " + "\n  ".join(offenders)
    )

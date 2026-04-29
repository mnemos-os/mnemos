# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller one-file build for the platform-native mnemos binary."""

import os
import platform
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


ENTRY_POINT = "mnemos.cli.main:app"
ROOT = Path(SPECPATH).resolve() if "SPECPATH" in globals() else Path(__file__).resolve().parent
DOCS_MAX_BUNDLE_BYTES = 5 * 1024 * 1024


def _detect_platform() -> str:
    override = os.environ.get("MNEMOS_BINARY_PLATFORM", "").strip()
    if override:
        return override

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "darwin" and machine == "arm64":
        return "macos-aarch64"
    raise SystemExit(f"Unsupported PyInstaller host platform: {system}-{machine}")


def _files_matching(pattern: str, dest: str) -> list[tuple[str, str]]:
    return [(str(path), dest) for path in sorted(ROOT.glob(pattern)) if path.is_file()]


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _files_under(root: Path, dest: str) -> list[tuple[str, str]]:
    if not root.exists():
        return []
    dest_root = Path(dest)
    return [
        (str(path), str(dest_root / path.relative_to(root).parent))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _write_entry_script() -> Path:
    entry_dir = ROOT / "build" / "pyinstaller-entry"
    entry_dir.mkdir(parents=True, exist_ok=True)
    entry_script = entry_dir / "mnemos_entry.py"
    module_name, app_name = ENTRY_POINT.split(":", 1)
    entry_script.write_text(
        f"from {module_name} import {app_name}\n\n"
        "if __name__ == '__main__':\n"
        f"    {app_name}()\n",
        encoding="utf-8",
    )
    return entry_script


BINARY_PLATFORM = _detect_platform()
BINARY_NAME = f"mnemos-{BINARY_PLATFORM}"

# PyInstaller hiddenimports use importable module names. The sqlite-vec
# distribution exposes the sqlite_vec module plus a native vec0 extension.
HIDDEN_IMPORTS = [
    "sqlite_vec",
    "aiosqlite",
    "asyncpg",
    "redis",
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "pydantic_settings",
    "typer",
    "click",
    "openai",
    "anthropic",
    "google.genai",
    "httpx",
]

datas = []
datas += _files_matching("db/migrations*.sql", "db")
datas += _files_matching("db/migrations_sqlite/*.sql", "db/migrations_sqlite")
if _tree_size(ROOT / "docs") <= DOCS_MAX_BUNDLE_BYTES:
    datas += _files_under(ROOT / "docs", "docs")
datas += collect_data_files("sqlite_vec")

binaries = []
binaries += collect_dynamic_libs("sqlite_vec")

a = Analysis(
    [str(_write_entry_script())],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=BINARY_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

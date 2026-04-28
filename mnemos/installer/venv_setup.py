"""Virtual environment management for MNEMOS mnemos.installer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(
    cmd: list[str],
    timeout: int = 300,
    env: dict = None,
    cwd: str = None,
) -> tuple[int, str, str]:
    """Run a command, stream output to console, return (rc, stdout, stderr)."""
    import os

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=merged_env,
            cwd=cwd,
        )
        stdout_lines = []
        stderr_lines = []

        # Stream stdout
        for line in proc.stdout:
            print(line, end="", flush=True)
            stdout_lines.append(line)

        proc.wait(timeout=timeout)

        stderr_data = proc.stderr.read()
        if stderr_data:
            stderr_lines.append(stderr_data)

        return proc.returncode, "".join(stdout_lines), "".join(stderr_lines)
    except subprocess.TimeoutExpired:
        proc.kill()
        return 1, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def _pip(venv_path: str, args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run pip inside the venv. Falls back to python -m pip if script missing."""
    venv = Path(venv_path)
    pip_bin = venv / "bin" / "pip"
    python_bin = venv / "bin" / "python"
    if pip_bin.exists():
        return _run([str(pip_bin)] + args, timeout=timeout)
    # Fallback: python -m pip (always works when venv was created correctly)
    return _run([str(python_bin), "-m", "pip"] + args, timeout=timeout)


def create_venv(base_path: str) -> str:
    """Create a virtual environment at {base_path}/venv. Returns venv path."""
    venv_path = str(Path(base_path) / "venv")

    # Check if venv already exists and is healthy
    python_bin = Path(venv_path) / "bin" / "python"
    if python_bin.exists():
        rc, out, _ = _run([str(python_bin), "--version"])
        if rc == 0:
            print(f"[venv] Existing venv found: {out.strip()}")
            return venv_path

    print(f"[venv] Creating virtual environment at {venv_path}...")
    rc, _, err = _run([sys.executable, "-m", "venv", venv_path])
    if rc != 0:
        raise RuntimeError(f"Failed to create venv: {err}")

    print("[venv] Virtual environment created.")

    # Upgrade pip, setuptools, wheel inside the venv
    print("[venv] Upgrading pip/setuptools/wheel...")
    rc, _, err = _pip(venv_path, ["install", "--upgrade", "pip", "setuptools", "wheel"])
    if rc != 0:
        print(f"[venv] WARNING: pip upgrade failed: {err}")

    return venv_path


def pip_install(venv_path: str, packages: list[str], timeout: int = 300) -> bool:
    """Install a list of packages into the venv."""
    if not packages:
        return True
    print(f"[venv] Installing: {' '.join(packages)}")
    rc, _, err = _pip(venv_path, ["install"] + packages, timeout=timeout)
    if rc != 0:
        print(f"[venv] ERROR installing packages: {err}", file=sys.stderr)
        return False
    return True


def install_requirements(venv_path: str, extra: list[str] = None) -> bool:
    """Install requirements.txt (and optionally requirements-phi.txt)."""
    repo_path = Path(venv_path).parent
    success = True

    # Primary requirements
    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        print(f"[venv] Installing requirements from {req_file}...")
        rc, _, err = _pip(
            venv_path,
            ["install", "-r", str(req_file)],
            timeout=600,
        )
        if rc != 0:
            print(f"[venv] ERROR installing requirements.txt: {err}", file=sys.stderr)
            success = False
    else:
        print(f"[venv] WARNING: {req_file} not found", file=sys.stderr)

    # Optional phi requirements (swallow errors)
    phi_req = repo_path / "requirements-phi.txt"
    if phi_req.exists():
        print(f"[venv] Installing optional phi requirements from {phi_req}...")
        rc, _, err = _pip(
            venv_path,
            ["install", "-r", str(phi_req)],
            timeout=600,
        )
        if rc != 0:
            print(f"[venv] NOTE: phi requirements failed (optional — continuing): {err}")

    # Extra packages passed by caller
    if extra:
        ok = pip_install(venv_path, extra)
        if not ok:
            success = False

    return success


def install_docling(venv_path: str) -> bool:
    """Install docling for document import support."""
    print("[venv] Installing docling (this may take several minutes)...")
    rc, _, err = _pip(
        venv_path,
        ["install", "docling"],
        timeout=900,  # docling has many deps
    )
    if rc != 0:
        print("[venv] NOTE: docling installation failed.", file=sys.stderr)
        print("[venv] You may need system libraries:")
        print("[venv]   Debian/Ubuntu: apt install -y libgl1 libglib2.0-0 libsm6 libxrender1 libxext6 poppler-utils tesseract-ocr")
        print("[venv]   Then retry:    /opt/mnemos/venv/bin/pip install docling")
        return False

    print("[venv] docling installed successfully.")
    return True

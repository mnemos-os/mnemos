"""Environment detection for MNEMOS mnemos.installer."""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class SystemInfo:
    os_type: str = ""            # 'linux', 'macos', 'windows'
    distro: str = ""             # 'debian', 'ubuntu', 'rhel', 'fedora', 'arch', 'macos', 'unknown'
    distro_version: str = ""     # e.g. '13.2'
    python_version: tuple = ()   # (3, 13, 5)
    python_ok: bool = False      # >= (3, 11)
    pg_installed: bool = False   # psql found in PATH
    pg_version: str = ""         # e.g. '16.2' or ''
    pg_running: bool = False     # pg_isready returns 0
    pgvector_available: bool = False  # SELECT 1 FROM pg_extension WHERE extname='vector'
    systemd: bool = False        # systemctl exists
    launchd: bool = False        # macOS launchctl exists
    disk_free_gb: float = 0.0    # free GB on /
    pip_available: bool = False  # pip3 or pip in PATH
    venv_available: bool = False # python3 -m venv --help succeeds
    git_available: bool = False


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return 1, "", ""


def _which(name: str) -> bool:
    """Return True if `name` is found in PATH."""
    rc, _, _ = _run(["which", name])
    return rc == 0


def _detect_os() -> tuple[str, str, str]:
    """Return (os_type, distro, distro_version)."""
    system = platform.system().lower()
    if system == "darwin":
        mac_ver = platform.mac_ver()[0]  # e.g. '15.4'
        return "macos", "macos", mac_ver
    if system == "windows":
        return "windows", "unknown", platform.version()
    if system == "linux":
        # Try /etc/os-release first
        try:
            with open("/etc/os-release") as fh:
                lines = {}
                for line in fh:
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        lines[k] = v.strip('"')
            id_val = lines.get("ID", "").lower()
            id_like = lines.get("ID_LIKE", "").lower()
            version_id = lines.get("VERSION_ID", "")

            distro_map = {
                "debian": "debian",
                "ubuntu": "ubuntu",
                "rhel": "rhel",
                "centos": "rhel",
                "fedora": "fedora",
                "arch": "arch",
                "manjaro": "arch",
                "opensuse": "rhel",
                "sles": "rhel",
            }
            distro = distro_map.get(id_val, "unknown")
            if distro == "unknown":
                for key in distro_map:
                    if key in id_like:
                        distro = distro_map[key]
                        break
            return "linux", distro, version_id
        except Exception:
            pass
        # Fallback: lsb_release
        rc, out, _ = _run(["lsb_release", "-is"])
        if rc == 0:
            distro = out.lower()
            _, ver, _ = _run(["lsb_release", "-rs"])
            return "linux", distro, ver
        return "linux", "unknown", ""
    return "unknown", "unknown", ""


def _detect_postgres() -> tuple[bool, str, bool]:
    """Return (pg_installed, pg_version, pg_running)."""
    installed = _which("psql")
    version = ""
    running = False
    if installed:
        rc, out, _ = _run(["psql", "--version"])
        if rc == 0:
            # "psql (PostgreSQL) 16.2" → "16.2"
            parts = out.split()
            for part in reversed(parts):
                if part[0].isdigit():
                    version = part
                    break
    # Check if running via pg_isready
    if _which("pg_isready"):
        rc, _, _ = _run(["pg_isready", "-q"])
        running = rc == 0
    else:
        # Fallback: try connecting
        rc, _, _ = _run(["psql", "-U", "postgres", "-c", "SELECT 1", "-q", "--no-password"])
        running = rc == 0
    return installed, version, running


def _detect_pgvector(pg_running: bool) -> bool:
    """Check if pgvector extension is available in any database."""
    if not pg_running:
        return False
    # Try to query the extension in postgres database
    rc, out, _ = _run([
        "sudo", "-u", "postgres", "psql", "-c",
        "SELECT 1 FROM pg_available_extensions WHERE name='vector'",
        "-t", "-A", "postgres"
    ], timeout=5)
    if rc == 0 and "1" in out:
        return True
    # Try without sudo (may work if running as postgres user)
    rc, out, _ = _run([
        "psql", "-U", "postgres", "-c",
        "SELECT 1 FROM pg_available_extensions WHERE name='vector'",
        "-t", "-A", "postgres", "--no-password"
    ], timeout=5)
    return rc == 0 and "1" in out


def _disk_free_gb(path: str = "/") -> float:
    """Return free disk space in GB for the given path."""
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return 0.0


def detect() -> SystemInfo:
    """Main entry point — assemble and return SystemInfo."""
    info = SystemInfo()

    os_type, distro, distro_version = _detect_os()
    info.os_type = os_type
    info.distro = distro
    info.distro_version = distro_version

    info.python_version = sys.version_info[:3]
    info.python_ok = sys.version_info >= (3, 11)

    pg_installed, pg_version, pg_running = _detect_postgres()
    info.pg_installed = pg_installed
    info.pg_version = pg_version
    info.pg_running = pg_running
    info.pgvector_available = _detect_pgvector(pg_running)

    info.systemd = _which("systemctl")
    info.launchd = os_type == "macos" and _which("launchctl")

    info.disk_free_gb = _disk_free_gb("/")

    info.pip_available = _which("pip3") or _which("pip")

    rc, _, _ = _run([sys.executable, "-m", "venv", "--help"])
    info.venv_available = rc == 0

    info.git_available = _which("git")

    return info


def print_summary(info: SystemInfo) -> None:
    """Pretty-print SystemInfo to stdout with colored OK/WARN/FAIL labels."""
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"

    def ok(label: str) -> str:
        return f"{GREEN}[  OK  ]{RESET} {label}"

    def warn(label: str) -> str:
        return f"{YELLOW}[ WARN ]{RESET} {label}"

    def fail(label: str) -> str:
        return f"{RED}[ FAIL ]{RESET} {label}"

    def status(condition: bool, ok_msg: str, fail_msg: str, warn_mode: bool = False) -> str:
        if condition:
            return ok(ok_msg)
        if warn_mode:
            return warn(fail_msg)
        return fail(fail_msg)

    print("\n=== MNEMOS Environment Check ===\n")

    # OS
    print(ok(f"OS: {info.os_type} / {info.distro} {info.distro_version}"))

    # Python
    ver_str = ".".join(str(x) for x in info.python_version)
    if info.python_ok:
        print(ok(f"Python {ver_str} (>= 3.11 required)"))
    else:
        print(fail(f"Python {ver_str} — need >= 3.11"))

    # PostgreSQL
    if info.pg_installed:
        print(ok(f"PostgreSQL installed: {info.pg_version}"))
    else:
        print(warn("PostgreSQL not installed (will be installed)"))

    if info.pg_running:
        print(ok("PostgreSQL is running"))
    else:
        print(warn("PostgreSQL not running"))

    if info.pgvector_available:
        print(ok("pgvector extension available"))
    else:
        print(warn("pgvector not detected (will be installed)"))

    # Init system
    if info.systemd:
        print(ok("systemd available"))
    elif info.launchd:
        print(ok("launchd available (macOS)"))
    else:
        print(warn("No init system detected — manual service start only"))

    # Disk
    if info.disk_free_gb >= 10:
        print(ok(f"Disk free: {info.disk_free_gb:.1f} GB"))
    elif info.disk_free_gb >= 5:
        print(warn(f"Disk free: {info.disk_free_gb:.1f} GB (low — 10 GB recommended)"))
    else:
        print(fail(f"Disk free: {info.disk_free_gb:.1f} GB (too low)"))

    # Tools
    print(status(info.pip_available, "pip available", "pip not found (will bootstrap)", warn_mode=True))
    print(status(info.venv_available, "venv module available", "venv not available", warn_mode=True))
    print(status(info.git_available, "git available", "git not found", warn_mode=True))

    print()


def check_port_free(port: int) -> bool:
    """Return True if the given TCP port is free to bind on localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    info = detect()
    print_summary(info)

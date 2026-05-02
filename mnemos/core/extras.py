"""Optional subsystem availability probes.

Python packaging does not expose "which extra was selected" at runtime.
MNEMOS therefore treats an extra as available when the modules it needs
can be imported. Extras with no dependency probe are always importable
from the installed wheel; their bundle value is documentation and
install UX rather than runtime detection.
"""

from __future__ import annotations

from collections.abc import Iterable

EXTRA_PROBES: dict[str, tuple[str, ...]] = {
    "morpheus": ("numpy",),
    "persephone": ("zstandard",),
    "pantheon": (),
    "kronos": ("numpy",),
    "knossos": (),
    "apollo": (),
    "artemis": (),
    "nats": ("nats",),
    "hot": ("mnemos_hot",),
}

FEATURE_BUNDLES: dict[str, tuple[str, ...]] = {
    "edge": ("edge",),
    "server": ("nats", "persephone", "pantheon"),
    "ml": ("morpheus", "kronos", "apollo", "artemis", "hot"),
    "interop": ("knossos",),
    "full": (
        "morpheus",
        "persephone",
        "pantheon",
        "kronos",
        "knossos",
        "apollo",
        "artemis",
        "nats",
        "hot",
        "edge",
    ),
}


def is_extra_installed(name: str) -> bool:
    """Check if optional extra ``name`` is available by probing deps."""
    probes = EXTRA_PROBES.get(name)
    if probes is None:
        return False
    for module in probes:
        try:
            __import__(module)
        except ImportError:
            return False
    return True


def install_hint(name: str) -> str:
    return f"pip install mnemos-os[{name}]  (or [server]/[ml]/[full] bundle)"


def missing_extra_detail(name: str, *, label: str | None = None) -> dict[str, str]:
    display = (label or name).upper()
    return {
        "error": f"{display} not installed",
        "install": install_hint(name),
    }


def require_extra(name: str) -> None:
    """Raise RuntimeError with install instruction if extra is missing."""
    if not is_extra_installed(name):
        raise RuntimeError(
            f"{name} subsystem not installed. "
            f"Install via: {install_hint(name)}"
        )


def bundle_status(members: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return ``(have, missing)`` for a bundle member list."""
    have: list[str] = []
    missing: list[str] = []
    for member in members:
        target = "edge" if member == "edge" else member
        if target == "edge":
            try:
                __import__("aiosqlite")
                __import__("sqlite_vec")
            except ImportError:
                missing.append(member)
            else:
                have.append(member)
            continue
        if is_extra_installed(target):
            have.append(member)
        else:
            missing.append(member)
    return have, missing

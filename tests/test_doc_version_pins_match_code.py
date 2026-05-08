"""Slice #193: pin doc "current" version claims to the actual
package version.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS), which found
~10 documentation files still claiming "current is v5.0.0" or
"current v4.0.0 release line" while ``pyproject.toml`` and
``mnemos/_version.py`` were both at v5.3.2.

This test pins the live current-version claim across the
operator-facing docs. Historical mentions (e.g. "v5.0.0 shipped
on 2026-05-02", "Shipped in v5.0.0") are explicitly NOT covered
by this guard — those are accurate historical fact.

What this test guards:

- the literal version string in `pyproject.toml` matches
  `mnemos/_version.py` (already pinned by the release script
  but worth a unit-test backstop)
- "current" version claims in docs match `__version__`
- pip install pins (`mnemos-os==X`) in operator-facing docs match
- single-binary release URL (`releases/download/vX/...`) matches
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _current_version() -> str:
    """Read the canonical version from `mnemos/_version.py`."""
    src = (REPO / "mnemos" / "_version.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert m, "could not parse __version__ from mnemos/_version.py"
    return m.group(1)


def test_pyproject_version_matches_version_py():
    """pyproject.toml and `mnemos/_version.py` must agree on the
    package version. This is also enforced by the release script;
    we add a unit-test backstop so a bad commit can't sneak past
    a missing release dry-run."""
    pyproject = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m, "could not parse version from pyproject.toml"
    assert m.group(1) == _current_version(), (
        f"pyproject.toml version {m.group(1)} does not match "
        f"mnemos/_version.py {_current_version()} — bump both."
    )


@pytest.mark.parametrize("relpath,phrase_template", [
    # operator-facing front matter
    ("README.md", "MNEMOS v{V} is"),
    ("DEPLOYMENT.md", "v{V} current"),
    ("API_DOCUMENTATION.md", "v{V} current"),
    ("SYSTEM_REQUIREMENTS.md", "current v{V} release line"),
    ("ROADMAP.md", "Current status — v{V}"),
    ("EVOLUTION.md", "current v{V} release line"),
    ("docs/OPERATIONS.md", "v{V} production line"),
    ("QUICK_START_REQUIREMENTS.md", "current v{V} release line"),
    ("docs/GRAEAE_FEATURES.md", "current v{V} documentation"),
    ("docs/SPECIFICATION.md", "v{V} current"),
])
def test_current_version_claim_in_doc(relpath: str, phrase_template: str):
    """Each operator-facing doc names its current-version claim.
    The phrase must appear with the live package version."""
    version = _current_version()
    expected = phrase_template.format(V=version)
    src = (REPO / relpath).read_text()
    assert expected in src, (
        f"{relpath} lacks the current-version phrase `{expected}`. "
        "If the wording was intentionally rephrased, also update "
        "this test to match — but don't let it drift back to a "
        "stale version string."
    )


def test_no_stale_install_pins_to_old_version():
    """Pip install pins in operator docs must use the live version
    or remain in MIGRATION sections clearly labelled as historical.
    Pin: no `==<old>` install commands outside CHANGELOG.md or
    explicitly historical sections.
    """
    version = _current_version()
    # CHANGELOG.md, EVOLUTION.md, and ROADMAP.md are
    # historical-narrative surfaces; they're not in the iterated
    # `for md in [...]` list below, which is the actual filter.
    # Match any pip install pin to a 4.x or 5.0.x version.
    pattern = re.compile(
        r"pip install\s+'?mnemos-os(?:\[[^\]]+\])?==(?P<v>[0-9.]+)'?"
    )
    bad: list[str] = []
    for md in [REPO / "README.md", REPO / "DEPLOYMENT.md",
               REPO / "QUICK_START_REQUIREMENTS.md",
               REPO / "docs" / "INSTALL.md",
               REPO / "API_DOCUMENTATION.md",
               REPO / "docs" / "OPERATIONS.md",
               REPO / "docs" / "GRAEAE_FEATURES.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            pinned = m.group("v")
            if pinned == version:
                continue
            # Section "Migration From Earlier v5 Installs" in
            # docs/INSTALL.md historical narrative around line
            # 85 mentions ==5.0.0 to describe past behavior.
            if md.name == "INSTALL.md" and "behaved like an" in line:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: pinned to "
                f"{pinned}, expected {version} — "
                f"{line.strip()[:80]}"
            )
    assert not bad, (
        f"{len(bad)} stale install pin(s) outside CHANGELOG / "
        f"EVOLUTION / ROADMAP:\n" + "\n".join(bad)
    )


def test_no_stale_health_json_version():
    """Doc-embedded `/health` JSON examples must use the live
    version. Codex round-1 of #193 caught two of these
    (API_DOCUMENTATION.md + docs/OPERATIONS.md). Pin so future
    drift trips at test time."""
    version = _current_version()
    pattern = re.compile(r'"version":\s*"(?P<v>\d[^"]*)"')
    bad: list[str] = []
    for md in [REPO / "API_DOCUMENTATION.md",
               REPO / "docs" / "OPERATIONS.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"{v!r} in /health JSON example, expected "
                f"{version!r}"
            )
    assert not bad, (
        f"{len(bad)} stale /health JSON version example(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_docker_image_tag():
    """Docker `ghcr.io/mnemos-os/mnemos:<tag>` references in
    operator/connector docs must pin to the live version. Codex
    round-1 of #193 caught a `:4.0.0` pin in
    `docs/connectors/chatgpt-pro-developer-mode.md`."""
    version = _current_version()
    pattern = re.compile(
        r"ghcr\.io/mnemos-os/mnemos:(?P<v>[0-9.]+)"
    )
    bad: list[str] = []
    operator_docs: list[Path] = [REPO / "README.md",
                                 REPO / "DEPLOYMENT.md"]
    if (REPO / "docs" / "connectors").exists():
        operator_docs.extend(
            (REPO / "docs" / "connectors").glob("*.md")
        )
    for md in operator_docs:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"image tag :{v}, expected :{version}"
            )
    assert not bad, (
        f"{len(bad)} stale Docker image tag(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_tracks_mnemos_server_marker():
    """Doc-footer "Tracks MNEMOS server vX" markers in
    architecture / observability docs must pin to the live
    version. Codex round-4 of #193 caught two of these."""
    version = _current_version()
    pattern = re.compile(
        r"Tracks MNEMOS server v(?P<v>\d+(?:\.\d+)*(?:[a-z]\d+)?)"
    )
    bad: list[str] = []
    docs_dir = REPO / "docs"
    if docs_dir.exists():
        for md in docs_dir.rglob("*.md"):
            for lineno, line in enumerate(md.read_text().splitlines(),
                                          start=1):
                m = pattern.search(line)
                if not m:
                    continue
                v = m.group("v")
                if v == version:
                    continue
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: stale "
                    f"`Tracks MNEMOS server v{v}` (expected v{version})"
                )
    assert not bad, (
        f"{len(bad)} stale `Tracks MNEMOS server vX` marker(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_as_of_version_anywhere():
    """`As of vX` / `as of vX` markers anywhere under docs/**/*.md
    or docs/**/*.json (Grafana dashboards) must pin to the live
    version. Round-5 broadened scope from runbooks-only to all
    docs after codex round-5 caught more drift in OBSERVABILITY,
    MEMORY_ARCHITECTURE, and the Grafana JSON.

    Case-insensitive `as of` so both `As of v5.3.2` and `as of
    v5.3.2` match.
    """
    version = _current_version()
    pattern = re.compile(
        r"\b[Aa]s of v(?P<v>\d+(?:\.\d+)*(?:[a-z]\d+)?)"
    )
    bad: list[str] = []
    docs_dir = REPO / "docs"
    surfaces: list[Path] = []
    if docs_dir.exists():
        surfaces.extend(docs_dir.rglob("*.md"))
        surfaces.extend(docs_dir.rglob("*.json"))
    for md in surfaces:
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"`as of v{v}` (expected v{version})"
            )
    assert not bad, (
        f"{len(bad)} stale `as of vX` marker(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_release_download_url():
    """Single-binary download URLs must reference the live
    version. Historical URLs in CHANGELOG / EVOLUTION are fine.
    """
    version = _current_version()
    pattern = re.compile(
        r"releases/download/v(?P<v>[0-9.]+)/mnemos-linux-x86_64"
    )
    bad: list[str] = []
    for md in [REPO / "README.md", REPO / "DEPLOYMENT.md",
               REPO / "QUICK_START_REQUIREMENTS.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            url_v = m.group("v")
            if url_v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"v{url_v} URL, expected v{version}"
            )
    assert not bad, (
        f"{len(bad)} stale single-binary download URL(s):\n"
        + "\n".join(bad)
    )

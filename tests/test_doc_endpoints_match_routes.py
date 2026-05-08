"""Slice #194: pin doc endpoint references to routes that actually
exist in `mnemos/api/routes/*.py`.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- `docs/MEMORY_EXPORT_FORMAT.md` documented `POST /v1/export`,
  but the live route is `GET /v1/export`
  (`mnemos/api/routes/portability.py:41`).
- `docs/connectors/openai-custom-gpt.md` referenced `/v1/health`
  for a manual auth probe; the actual health route is `/health`
  (unversioned).
- `docs/connectors/{claude-desktop,cline,continue,cursor,README}.md`
  referenced `/v1/mcp/discovery`; that route does NOT exist. MCP
  discovery is the protocol's `tools/list` JSON-RPC method over
  SSE/stdio, not a REST endpoint. Replaced with `/health` server-
  up check + a Python one-liner against the canonical
  `TOOL_REGISTRY`.

This test pins both the corrections AND the live route shape so
a future re-add or route refactor trips at test time.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_v1_export_route_is_get_not_post():
    """`/v1/export` route is GET in the codebase. Confirm both
    sides — the live decorator is `@router.get("/export"...` and
    the doc claim agrees with it."""
    portability = (REPO / "mnemos" / "api" / "routes"
                   / "portability.py").read_text()
    assert '@router.get(\n    "/export"' in portability \
        or '@router.get("/export"' in portability, (
        "/v1/export is no longer a GET. Update "
        "tests/test_doc_endpoints_match_routes.py and the docs "
        "if the verb intentionally changed."
    )

    # No remaining `POST /v1/export` claim in operator-facing docs.
    pattern = re.compile(r"`?\bPOST\s+/v1/export\b`?")
    bad: list[str] = []
    for md in [REPO / "docs" / "MEMORY_EXPORT_FORMAT.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            if pattern.search(line):
                bad.append(f"  {md.relative_to(REPO)}:{lineno}: "
                           f"{line.strip()[:80]}")
    assert not bad, (
        f"{len(bad)} doc(s) still claim `POST /v1/export`:\n"
        + "\n".join(bad)
    )


def test_v1_mcp_discovery_route_does_not_exist_in_routes():
    """`/v1/mcp/discovery` is NOT a real route. Confirm the
    `mnemos/api/routes/` tree has no path string `/mcp/discovery`
    and no doc claims it does."""
    routes_dir = REPO / "mnemos" / "api" / "routes"
    bad_in_code: list[str] = []
    for py in routes_dir.glob("*.py"):
        src = py.read_text()
        if "/mcp/discovery" in src:
            bad_in_code.append(str(py.relative_to(REPO)))
    assert not bad_in_code, (
        "A `/mcp/discovery` route appeared in routes/. If MCP "
        "discovery now has a REST surface, also bring back the "
        "doc references that #194 removed and update this test."
    )

    pattern = re.compile(r"/v1/mcp/discovery\b")
    bad_in_docs: list[str] = []
    docs = REPO / "docs"
    if docs.exists():
        for md in docs.rglob("*.md"):
            for lineno, line in enumerate(md.read_text().splitlines(),
                                          start=1):
                if pattern.search(line):
                    bad_in_docs.append(
                        f"  {md.relative_to(REPO)}:{lineno}: "
                        f"{line.strip()[:80]}"
                    )
    assert not bad_in_docs, (
        f"{len(bad_in_docs)} doc(s) still reference the "
        f"non-existent `/v1/mcp/discovery`:\n"
        + "\n".join(bad_in_docs)
    )


def test_health_route_is_unversioned():
    """`/health` is the unversioned health route; `/v1/health`
    does not exist."""
    # The live route lives in mnemos/api/routes/health.py.
    health = (REPO / "mnemos" / "api" / "routes" / "health.py").read_text()
    assert '"/health"' in health or "'/health'" in health, (
        "/health route disappeared from mnemos/api/routes/health.py. "
        "Update doc connectors and this test if it intentionally "
        "moved."
    )
    # And that it's not behind a /v1 prefix on the router.
    assert 'prefix="/v1"' not in health and "prefix='/v1'" not in health, (
        "/health router gained a /v1 prefix; doc connectors say "
        "the route is unversioned. Resolve the conflict."
    )

    pattern = re.compile(r"\b/v1/health\b")
    bad: list[str] = []
    docs = REPO / "docs"
    if docs.exists():
        for md in docs.rglob("*.md"):
            for lineno, line in enumerate(md.read_text().splitlines(),
                                          start=1):
                if pattern.search(line):
                    bad.append(
                        f"  {md.relative_to(REPO)}:{lineno}: "
                        f"{line.strip()[:80]}"
                    )
    assert not bad, (
        f"{len(bad)} doc(s) still reference `/v1/health` (use "
        f"`/health` — health is unversioned):\n"
        + "\n".join(bad)
    )

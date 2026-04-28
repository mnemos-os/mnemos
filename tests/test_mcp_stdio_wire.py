"""MCP stdio server ↔ REST route wire-contract regression tests.

Regression tests for the prefix mismatch where the MCP tool registry called
`/memories*` but the REST router registers `/v1/memories*`. This is a
static contract check — it imports the FastAPI app, enumerates the
registered route paths, and asserts that every path the registry
targets is actually served.

No running server required. Runs in the normal pytest suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).parent.parent
MCP_TOOLS = REPO_ROOT / "mnemos" / "mcp" / "tools.py"


def _extract_mcp_paths() -> list[str]:
    """Pull every literal/f-string path passed to _rest_* in
    mnemos/mcp/tools.py. Returns the static prefix up to the first f-string hole."""
    src = MCP_TOOLS.read_text(encoding="utf-8")
    # Match _rest_get("..."), _rest_post("..."), _rest_delete("..."), and f"..." variants.
    # We capture the leading literal segment; f-strings with `{arg}` holes
    # keep only the static prefix so we compare against registered path
    # *patterns*, not rendered URIs.
    pattern = re.compile(
        r"""_rest_(?:get|post|delete)\(\s*f?["']([^"'{]+)(?:["']|\{)""",
        re.MULTILINE,
    )
    paths = set()
    for match in pattern.finditer(src):
        path = match.group(1).rstrip("/")
        if path:
            paths.add(path)
    return sorted(paths)


def _registered_prefixes() -> list[str]:
    """Enumerate the static prefixes of every route registered in the
    FastAPI app. Returns prefixes suitable for startswith() matching
    against mnemos/mcp/tools.py's literal path prefixes."""
    # Import lazily — the app import chain pulls in asyncpg / pgvector
    # stubs, which conftest.py sets up.
    from mnemos.api.main import app  # noqa: E402
    prefixes: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path:
            continue
        # Strip FastAPI path params ({memory_id}) — we only compare on
        # the static segments.
        static = re.sub(r"\{[^}]+\}", "", path).rstrip("/")
        if static:
            prefixes.add(static)
    return sorted(prefixes)


def _any_prefix_matches(mcp_path: str, registered: Iterable[str]) -> bool:
    """True if `mcp_path` is a prefix of some registered route's static
    form (or vice versa, to handle f-string holes at different positions)."""
    for route in registered:
        if route == mcp_path:
            return True
        if route.startswith(mcp_path + "/") or route.startswith(mcp_path):
            return True
        if mcp_path.startswith(route + "/") or mcp_path.startswith(route):
            return True
    return False


class TestMCPWireContract:
    """Every path the stdio MCP server calls must be a route the REST
    app serves. This test would have caught the v3.0.0 regression where
    nine memory tools returned 404 because the MCP server called
    /memories while the router registered /v1/memories."""

    def test_every_mcp_path_is_a_real_route(self):
        mcp_paths = _extract_mcp_paths()
        registered = _registered_prefixes()
        assert mcp_paths, "mnemos/mcp/tools.py exposes no paths — something is wrong with extraction"
        assert registered, "FastAPI app exposes no routes — import failed"

        missing: list[str] = []
        for path in mcp_paths:
            if not _any_prefix_matches(path, registered):
                missing.append(path)

        assert not missing, (
            f"mnemos/mcp/tools.py calls these paths that the REST app does not serve: {missing}. "
            f"This is the #M31-01 regression — MCP stdio server returns 404 for callers. "
            f"Registered prefixes (sample): {sorted(registered)[:20]}"
        )

    def test_memory_paths_are_v1_prefixed(self):
        """Explicit regression: every memory-related path in mnemos/mcp/tools.py
        must carry the /v1 prefix. Reverting the prefix would reintroduce
        the v3.0.0 bug."""
        mcp_paths = _extract_mcp_paths()
        memory_paths = [p for p in mcp_paths if "/memor" in p]
        assert memory_paths, "mnemos/mcp/tools.py has no memory paths — extraction regex is broken"

        unprefixed = [p for p in memory_paths if not p.startswith("/v1/")]
        assert not unprefixed, (
            f"These memory paths in mnemos/mcp/tools.py are missing the /v1 prefix: {unprefixed}. "
            f"The REST router registers memories under /v1 (api/handlers/memories.py:34); "
            f"any path without the prefix will 404."
        )

    def test_kg_paths_are_v1_prefixed(self):
        """KG routes were moved from /kg to /v1/kg as part of the v3.3
        API cleanup pass. Any
        unversioned /kg/* path in mnemos/mcp/tools.py would 404 against the
        current API."""
        mcp_paths = _extract_mcp_paths()
        kg_paths = [p for p in mcp_paths if "/kg/" in p or p.endswith("/kg")]
        assert kg_paths, "mnemos/mcp/tools.py has no KG paths — extraction regex is broken"

        unprefixed = [
            p for p in kg_paths
            if (p.startswith("/kg/") or p == "/kg") and not p.startswith("/v1/")
        ]
        assert not unprefixed, (
            f"These KG paths in mnemos/mcp/tools.py are missing the /v1 prefix: "
            f"{unprefixed}. The REST router registers KG under /v1/kg "
            f"(api/handlers/kg.py); any path without the prefix will 404."
        )

"""Slice #197: pin doc claims about the canonical MCP tool count
to the live ``mnemos.mcp.tools.TOOL_REGISTRY``.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- ``README.md:130-138`` listed 22 of the 23 live tools (missing
  ``list_deletions``); :682 + :701 still said "18 tools" / "all
  18 tools".
- ``ROADMAP.md:25`` said "across 22 tools"; :298 said "22 tools
  from one canonical registry".
- ``docs/SPECIFICATION.md:199`` said "18 tools from
  mnemos/mcp/tools/"; :372 said "MCP (stdio and HTTP/SSE, 18
  tools)".
- ``docs/connectors/README.md:48`` named source-of-truth
  modules as ``{memory,kg,dag,models}.py``; current registry
  also uses ``kronos.py`` and ``deletions.py``. :222 said
  "canonical 18-tool registry".

Live count is 23 as of HEAD ``07e1154``. This test reads the
registry at runtime and pins all four doc surfaces against it,
so a future tool addition auto-bumps the assertion target rather
than churning literal numbers across docs.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _canonical_tool_set() -> set[str]:
    """Parse the canonical `_TOOL_ORDER` literal from the source
    file. This is the install-independent answer; the runtime
    `TOOL_REGISTRY` is filtered by `is_extra_installed(...)` so
    it under-counts when optional extras (`kronos`, `pantheon`)
    are not installed. CI runs `pip install -e .[dev]` which omits
    those extras — codex round-1 of #197 caught this exact gap.

    The canonical set is what doc claims like "23 tools from one
    canonical registry" actually describe.
    """
    import ast
    src = (REPO / "mnemos" / "mcp" / "tools" / "__init__.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_TOOL_ORDER":
                    names: set[str] = set()
                    if isinstance(node.value, ast.List):
                        for el in node.value.elts:
                            if isinstance(el, ast.Constant) and \
                                    isinstance(el.value, str):
                                names.add(el.value)
                    return names
    raise AssertionError(
        "could not locate `_TOOL_ORDER` literal in "
        "mnemos/mcp/tools/__init__.py — has the structure changed?"
    )


def _canonical_tool_count() -> int:
    return len(_canonical_tool_set())


def test_no_stale_tool_count_in_docs():
    """No operator/architecture doc should claim a tool count
    other than the canonical one. Pattern matches `<N> tools` /
    `<N> MCP tools` / `<N>-tool` / `<N> tool definitions` where
    the line clearly refers to the MCP/canonical/security/native
    registry.

    Round-2 of #197 broadened both the regex (to also match
    `<N>-tool registry` and `<N> tool definitions` per codex's
    round-1 finding in chatgpt-pro-developer-mode.md and
    KNOSSOS.md) and the surface list (RELEASE_CHECKLIST.md,
    chatgpt-pro-developer-mode.md, KNOSSOS.md added).
    """
    n = _canonical_tool_count()
    # Match `<N> tools`, `<N> MCP tools`, `<N>-tool`,
    # `<N> tool definitions`. The trailing `\w*` after `tool`
    # absorbs `tools`, `tool`, `tool-`, etc.
    pattern = re.compile(
        r"\b(\d{2})[-\s]+(?:MCP\s+)?tool(?:s|\b|\s+(?:definitions|registry))"
    )
    bad: list[str] = []
    surfaces = [
        REPO / "README.md",
        REPO / "ROADMAP.md",
        REPO / "docs" / "SPECIFICATION.md",
        REPO / "docs" / "connectors" / "README.md",
        REPO / "docs" / "RELEASE_CHECKLIST.md",
        REPO / "docs" / "connectors" / "chatgpt-pro-developer-mode.md",
        REPO / "docs" / "KNOSSOS.md",
    ]
    for md in surfaces:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = int(m.group(1))
            if v == n:
                continue
            # Allow non-MCP "tools" mentions (e.g. "16 tools" in
            # an unrelated context). The MCP tool-count claims
            # all live in lines that ALSO say "registry",
            # "canonical", "MCP", "tools/list", "native", or
            # similar registry keywords.
            stripped = line.lower()
            if not any(kw in stripped for kw in (
                "registry", "canonical", "mcp", "tools/list",
                "from one", "across", "tool registry", "native",
                "definitions",
            )):
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: claims "
                f"`{v}` tools, canonical is {n} — {line.strip()[:70]}"
            )
    assert not bad, (
        f"{len(bad)} doc(s) claim a stale MCP tool count "
        f"(canonical registry has {n}):\n" + "\n".join(bad)
    )


def test_canonical_tool_modules_listed_in_connectors_readme():
    """The connectors README's "Source of truth" line names the
    actual MCP tool modules. After the audit, current modules
    include `kronos.py` and `deletions.py` in addition to
    memory/kg/dag/models. Pin both there."""
    src = (REPO / "docs" / "connectors" / "README.md").read_text()
    for module in ("memory", "kg", "dag", "models",
                   "kronos", "deletions"):
        assert module in src, (
            f"docs/connectors/README.md no longer mentions "
            f"`{module}.py` as a source-of-truth MCP tool module."
        )


def test_readme_lists_list_deletions_among_tools():
    """README's bulleted MCP tool list must include
    ``list_deletions``. Audit caught it as the only tool the
    enumerated list missed."""
    src = (REPO / "README.md").read_text()
    assert "list_deletions" in src, (
        "README.md no longer mentions `list_deletions` in the "
        "MCP tool enumeration. The live registry has 23 tools "
        "(verify with: from mnemos.mcp.tools import TOOL_REGISTRY)."
    )


def test_canonical_registry_declares_expected_tools():
    """Sanity-check: pin a minimal set of canonical tools that
    must remain in the source-declared `_TOOL_ORDER`. If any of
    these go away in a future refactor, doc references in
    ``docs/connectors/README.md`` and ``README.md`` need a
    follow-up before this test passes again.

    Tests the canonical declared set, not the runtime-filtered
    `TOOL_REGISTRY`, so optional-extra installs (kronos, pantheon)
    don't affect the assertion. Round-2 of #197.
    """
    declared = _canonical_tool_set()
    must_have = {
        "search_memories", "list_memories", "get_memory",
        "create_memory", "update_memory", "delete_memory",
        "list_deletions", "kg_search", "kg_timeline",
        "log_memory", "branch_memory", "checkout_memory",
        "recommend_model", "pantheon_list_models",
        "pantheon_route_explain", "kronos_anomalies",
        "kronos_forecast",
    }
    missing = must_have - declared
    assert not missing, (
        f"Canonical MCP tools missing from `_TOOL_ORDER` in "
        f"mnemos/mcp/tools/__init__.py: {sorted(missing)}"
    )

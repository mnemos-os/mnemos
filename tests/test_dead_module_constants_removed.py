"""Slice #181: dead module-level constants (no references) are gone.

Pin the specific names so a future "polish" or merge that
re-introduces them without also wiring them up is caught at test
time. AST-scanning all UPPER_CASE module constants codebase-wide
would be too noisy (many legit defensive/exported constants); a
named list is bounded and intentional.

History: 7 constants found by an AST scan that flagged any
module-level UPPER_CASE assignment with only one occurrence in
the entire mnemos/+tests/ tree (i.e. the definition itself).
``_REGISTRY_LOCK`` in ``gpu_guard.py`` was also flagged but kept
— it's defensive code in a single-threaded asyncio context where
the lock would never serialize a real race; safe to leave for
multi-thread readers.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module_path,name",
    [
        ("mnemos/tools/knossos_mcp.py", "TUNNEL_PREDICATE_PREFIX"),
        ("mnemos/mcp/http.py", "DEFAULT_NATS_SSE_SUBJECT"),
        ("mnemos/webhooks/nats_trigger.py", "QUEUE_GROUP"),
        ("mnemos/tools/adapters/cognee.py", "_STRUCTURAL_EDGES"),
        ("mnemos/api/routes/admin.py", "_VALID_REASONS"),
        ("mnemos/api/routes/admin.py", "_VALID_PROFILES"),
        ("mnemos/domain/compression/worker_contest.py", "_INFRA_RESET_SQL"),
        ("mnemos/domain/compression/apollo_schemas/code.py", "_SYMBOL_RE"),
    ],
)
def test_dead_module_constant_removed(module_path, name):
    """Each dead constant must stay removed unless reintroduced
    with a real callsite."""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / module_path).read_text()
    # Look for an assignment of the constant — `NAME = ...` at the
    # start of a line.
    import re
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=", re.MULTILINE)
    assert not pattern.search(src), (
        f"{module_path} re-introduced module constant `{name}`. "
        "If this is intentional, add a real callsite (or remove "
        "this guard for the constant). #181 removed it because no "
        "code path read its value."
    )

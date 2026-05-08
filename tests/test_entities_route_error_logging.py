"""Slice #161: every except handler in entities route must log.

The entities route had 4 `except Exception:` blocks that raised HTTP
500 without any log entry. Operators saw "Internal server error" in
the response with no breadcrumb in logs to diagnose the underlying
cause (DB connection drop, asyncpg error, missing column, etc.).

This is a source-level regression guard: every `except Exception` in
mnemos/api/routes/entities.py must be paired with a `logger.error(...,
exc_info=True)` call. Without this, a future "polish" pass that
removes the logger call would silently regress diagnostics.
"""
from __future__ import annotations

import inspect
import re

from mnemos.api.routes import entities


def test_every_except_exception_in_entities_logs_with_exc_info():
    """Source-level guard: each `except Exception` block must contain
    a logger.error(..., exc_info=True) call before raising."""
    src = inspect.getsource(entities)

    # Find every `except Exception` block. We capture from the
    # `except Exception` line through the next `raise HTTPException`
    # call (which terminates the block).
    pattern = re.compile(
        r"except Exception(?: as \w+)?:\s*\n"
        r"((?:[ \t]+[^\n]*\n)+?)"  # body lines
        r"[ \t]+raise HTTPException",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(src))
    assert matches, "expected at least one `except Exception:` block in entities.py"

    for match in matches:
        body = match.group(1)
        snippet = match.group(0)
        # Must reference logger.error AND exc_info=True.
        assert "logger.error" in body, (
            f"except Exception block missing logger.error call:\n{snippet}"
        )
        assert "exc_info=True" in body, (
            f"except Exception block missing exc_info=True:\n{snippet}"
        )


def test_entities_module_imports_logger():
    """Defensive: the module must import logging + define a `logger`
    name. Without it, the regression test above would still pass for
    a file that imports a non-functional logger."""
    src = inspect.getsource(entities)
    assert "import logging" in src
    assert re.search(r"^logger = logging\.getLogger", src, re.MULTILINE), (
        "expected `logger = logging.getLogger(...)` at module scope"
    )

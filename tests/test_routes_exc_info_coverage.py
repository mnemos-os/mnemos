"""Codebase-wide guard: every except-block ``logger.error`` in
mnemos/ must include ``exc_info=True``.

Routes that don't use exc_info=True swallow the stack trace —
operators see only the exception's __str__ in logs, making it hard
to diagnose where the failure originated. This test is a single
regression guard across the entire mnemos/ tree so a future
"polish" pass that drops exc_info=True from any module gets caught.

History (kept for context — file name preserved for git blame):
- #161 entities route fix
- #172 dag route sweep
- #173 remaining routes (5 modules) + parametrized regression for
  mnemos/api/routes/
- #174 extended to mnemos/workers/
- #175 extended to mnemos/domain/ + mnemos/core/
- #176 extended to mnemos/nats/ + mnemos/mcp/ (round-2 fix:
  __init__.py files no longer skipped)
- #177 unified into a single sweep covering ALL of mnemos/

Patterns intentionally exempt:
- ``except Exception:`` (no ``as``) — usually intentional silent
  swallow (cache invalidation, etc.)
- ``logger.warning`` / ``logger.exception`` — the latter already
  includes exc_info implicitly; the former is operator-discretion.
- ``logger.error`` calls outside ``except`` blocks (defensive logs
  with no exception in scope).

Implementation note: uses a simple line-based scan instead of a
multiline regex (catastrophic backtracking made the regex variant
hang on large modules like dag.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _all_mnemos_modules() -> list[tuple[str, str]]:
    """Yield (relative_path, source) for every Python file under
    ``mnemos/``. Empty / trivial modules pass the guard via
    ``assert not missing``."""
    repo = Path(__file__).resolve().parents[1]
    base = repo / "mnemos"
    out: list[tuple[str, str]] = []
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        relpath = path.relative_to(repo).as_posix()
        out.append((relpath, path.read_text()))
    return out


def _find_missing_exc_info(src: str) -> list[str]:
    """Walk lines, track when we're inside an ``except Exception as
    <name>:`` block, flag ``logger.error(...)`` calls inside those
    blocks that lack ``exc_info=True``. Block ends at first dedented
    non-blank line."""
    lines = src.splitlines()
    missing: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        # Detect the start of an `except Exception as <name>:` block.
        if stripped.startswith("except Exception as ") and stripped.rstrip().endswith(":"):
            block_indent = len(line) - len(stripped)
            # Walk forward through the block.
            j = i + 1
            block_lines: list[str] = []
            while j < len(lines):
                next_line = lines[j]
                if next_line.strip() == "":
                    block_lines.append(next_line)
                    j += 1
                    continue
                next_indent = len(next_line) - len(next_line.lstrip())
                if next_indent <= block_indent:
                    break
                block_lines.append(next_line)
                j += 1
            block = "\n".join(block_lines)
            # If logger.error appears in the block but exc_info=True
            # doesn't, flag it.
            if "logger.error" in block and "exc_info=True" not in block:
                # Capture the except line + first 8 block lines for
                # the failure message.
                snippet = line + "\n" + "\n".join(block_lines[:8])
                missing.append(snippet)
            i = j
        else:
            i += 1
    return missing


@pytest.mark.parametrize("relpath,src", _all_mnemos_modules())
def test_mnemos_module_except_blocks_include_exc_info(relpath, src):
    """Source-level guard across the entire mnemos/ tree.

    Every ``except Exception as <name>:`` block that calls
    ``logger.error(...)`` must include ``exc_info=True``. Routes,
    workers, domain logic, persistence, NATS, MCP, installer,
    federation — all the same contract.

    A new module added to mnemos/ that doesn't follow the contract
    will fail this test the moment its file lands."""
    missing = _find_missing_exc_info(src)
    assert not missing, (
        f"{len(missing)} `except Exception as` block(s) in "
        f"{relpath} call logger.error without exc_info=True — "
        f"operators lose stack traces:\n\n"
        + "\n---\n".join(missing)
    )

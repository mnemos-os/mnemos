"""Slice #172: every except-block logger.error in dag.py uses exc_info=True.

The dag route had 5 `except Exception as e:` blocks that called
``logger.error(f"... {e}")`` without ``exc_info=True``. Operators
saw the exception's __str__ in logs but no stack trace, making it
hard to diagnose where the failure originated.

Mirrors the #161 entities-route fix. This is a source-level
regression guard so a future "polish" pass that removes the
``exc_info=True`` keyword silently doesn't get through unnoticed.
"""
from __future__ import annotations

import inspect
import re

from mnemos.api.routes import dag


def test_every_except_block_logger_error_in_dag_uses_exc_info():
    """Source-level guard: each `except Exception as e:` block in
    dag.py followed by `logger.error(...)` must include
    ``exc_info=True``."""
    src = inspect.getsource(dag)

    # Find every `except Exception as e:` block. The logger.error
    # line should appear within ~6 lines of the except line.
    pattern = re.compile(
        r"except Exception as e:\s*\n"
        r"((?:[ \t]+[^\n]*\n){1,8}?)",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(src))
    assert matches, "expected at least one `except Exception as e:` in dag.py"

    missing: list[str] = []
    for match in matches:
        body = match.group(1)
        if "logger.error" not in body:
            # Some except handlers raise/re-shape rather than logging
            # — skip those.
            continue
        if "exc_info=True" not in body:
            missing.append(match.group(0).rstrip())

    assert not missing, (
        f"{len(missing)} `except Exception as e:` block(s) in dag.py "
        f"call logger.error without exc_info=True:\n\n"
        + "\n---\n".join(missing)
    )


def test_dag_module_imports_logger():
    """Defensive: dag.py must define a `logger` at module scope.
    Without it, the regression test above would still pass for a
    file that imports a non-functional logger."""
    src = inspect.getsource(dag)
    assert "import logging" in src
    assert re.search(r"^logger = logging\.getLogger", src, re.MULTILINE)

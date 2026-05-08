"""Slice #202: pin Postgres `semantic_search` embedding-dim validation.

Audit MED finding (mem_1778221719390_8cb1ba) at
``mnemos/persistence/postgres.py:semantic_search``: the path cast
arbitrary-length embeddings to `vector` without dim validation.
``SqliteMemoryRepository`` had ``_require_dim`` for this exact
case; the Postgres path was the asymmetric gap.

This test exercises the new
``PostgresMemoryRepository._require_dim`` helper directly + the
``semantic_search`` integration point. It does NOT need a live
Postgres instance — the dim guard runs before the asyncpg cast
and raises a Python ``ValueError`` with the operator-facing
message.
"""
from __future__ import annotations

import inspect

import pytest

from mnemos.persistence.postgres import PostgresMemoryRepository


def test_require_dim_no_op_when_unset():
    """When `_expected_embedding_dim` is None, the guard is a
    no-op (matches the SQLite pattern for tests bypassing the
    backend)."""
    repo = PostgresMemoryRepository()
    assert repo._expected_embedding_dim is None
    repo._require_dim([0.1, 0.2, 0.3], "semantic_search")  # no raise


def test_require_dim_raises_on_short_vector():
    """A vector shorter than configured raises ValueError with a
    message naming the actual vs expected dim."""
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 768
    with pytest.raises(ValueError) as excinfo:
        repo._require_dim([0.1] * 384, "semantic_search")
    msg = str(excinfo.value)
    assert "Postgres" in msg
    assert "384-D vector" in msg
    assert "MNEMOS_EMBEDDING_DIM is 768" in msg


def test_require_dim_raises_on_long_vector():
    """A vector longer than configured raises with a similarly
    actionable message."""
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 768
    with pytest.raises(ValueError) as excinfo:
        repo._require_dim([0.1] * 1536, "semantic_search")
    assert "1536-D vector" in str(excinfo.value)


def test_require_dim_passes_on_match():
    """An exactly-sized vector passes silently."""
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 768
    repo._require_dim([0.1] * 768, "semantic_search")  # no raise


def test_require_dim_message_names_remediation_steps():
    """The error message must point operators at
    ``INFERENCE_EMBED_HOST`` / model selection so they know
    what to fix. Same shape as the SQLite-path message."""
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 512
    with pytest.raises(ValueError) as excinfo:
        repo._require_dim([0.1] * 768, "semantic_search")
    msg = str(excinfo.value)
    assert "INFERENCE_EMBED_HOST" in msg
    assert "MNEMOS_EMBEDDING_DIM" in msg


def test_semantic_search_calls_require_dim_first():
    """Source-level guard: the very first executable statement of
    ``semantic_search`` must be the ``_require_dim`` call, so the
    guard runs before any asyncpg work."""
    src = inspect.getsource(PostgresMemoryRepository.semantic_search)
    # Strip the function header + docstring + leading whitespace
    # and find the first non-comment/non-blank line.
    body_started = False
    first_stmt: str | None = None
    for line in src.splitlines():
        stripped = line.strip()
        if not body_started:
            # Skip until the closing `) -> list[Row]:` of the def.
            if stripped.endswith("-> list[Row]:") or stripped.endswith(":"):
                body_started = True
            continue
        if not stripped or stripped.startswith("#"):
            continue
        first_stmt = stripped
        break
    assert first_stmt is not None
    assert "_require_dim" in first_stmt, (
        "semantic_search no longer guards the embedding dim as its "
        f"first statement (got: {first_stmt!r}). The guard must run "
        "before any asyncpg work so the operator-facing error names "
        "the model mismatch instead of the asyncpg cast layer."
    )

"""Slice #201: pin BulkCreateRequest.memories max_length cap.

Audit MED finding (mem_1778221719390_8cb1ba):
``BulkCreateRequest.memories`` had no ``max_length`` cap, unlike
newer hardened request fields. The ``/v1/memories/bulk`` handler
iterates the list with one transaction per memory (N+1 writes +
publishes), so an unbounded request can open thousands of round-
trips through dedup + insert + version trigger + webhook outbox.

Capped at 1000 to match the compression-enqueue admin pattern.
Validation rejects over-cap requests at the Pydantic boundary
as 422 before any auth/RLS work happens.

This test pins:
1. The Pydantic model raises a validation error when given more
   than 1000 items.
2. Exactly 1000 still validates (the boundary).
3. The cap is named in the model's `Field(...)` so a future
   refactor can't silently widen it without tripping a test.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest


def _mk(n: int) -> list[dict]:
    return [{"content": f"m{i}", "category": "facts"} for i in range(n)]


def test_bulk_create_rejects_over_cap():
    """1001 items is over the cap; Pydantic must raise."""
    with pytest.raises(ValidationError):
        BulkCreateRequest(memories=_mk(1001))


def test_bulk_create_accepts_at_cap():
    """Exactly 1000 must still validate. If the cap moves, this
    test must be updated alongside."""
    req = BulkCreateRequest(memories=_mk(1000))
    assert len(req.memories) == 1000
    assert isinstance(req.memories[0], MemoryCreateRequest)


def test_bulk_create_accepts_under_cap():
    """Sanity-check: 1 and 100 still validate."""
    BulkCreateRequest(memories=_mk(1))
    BulkCreateRequest(memories=_mk(100))


def test_bulk_create_max_length_is_explicit_in_source():
    """Pin the cap in the source so a future refactor can't
    silently raise the limit. The literal ``max_length=1000`` must
    appear in the BulkCreateRequest definition; if you intentionally
    move the cap, update both this test and the comment block in
    ``domain/models.py``."""
    src = (Path(__file__).resolve().parents[1]
           / "mnemos" / "domain" / "models.py").read_text()
    assert "class BulkCreateRequest(BaseModel):" in src
    # Pull the section between the class header and the next class.
    start = src.index("class BulkCreateRequest(BaseModel):")
    end = src.index("\n\nclass ", start)
    section = src[start:end]
    assert "max_length=1000" in section, (
        "BulkCreateRequest.memories no longer caps at 1000; if the "
        "cap moved, update this test and the rationale comment in "
        "the model definition."
    )

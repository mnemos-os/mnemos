"""Slice #178: MemoryUpdateRequest must not declare quality_rating.

The field was declared but never consumed by the update_memory
route handler. Clients setting it expected updates that silently
never happened — a doc-vs-behavior gap. Removed in #178.

This test pins the removal so a future "polish" pass that
re-introduces the field doesn't slip through unnoticed without
also wiring it through the handler.
"""
from __future__ import annotations

import typing

from mnemos.domain.models import MemoryUpdateRequest


def test_memory_update_request_has_no_quality_rating_field():
    """The field must not be re-introduced without also wiring
    it through update_memory's handler."""
    fields = typing.get_type_hints(MemoryUpdateRequest)
    assert "quality_rating" not in fields, (
        "MemoryUpdateRequest.quality_rating was re-introduced. "
        "Either wire it through to backend.memories.update_memory "
        "(+ test the wiring) or keep the field removed."
    )


def test_memory_update_request_silently_ignores_quality_rating():
    """Pydantic v2 default extra='ignore' means clients still pass
    through unchanged. This is a regression guard — if someone
    flips `extra='forbid'`, the silent-ignore promise breaks."""
    # Construct with a stray quality_rating; Pydantic should accept
    # and silently drop the unknown field.
    req = MemoryUpdateRequest.model_validate(
        {"content": "x", "quality_rating": 99}
    )
    assert req.content == "x"
    assert not hasattr(req, "quality_rating") or req.__pydantic_extra__ in (
        None, {}, {"quality_rating": 99}
    )


def test_update_memory_handler_does_not_read_quality_rating():
    """Source-level guard: update_memory must not reference
    request.quality_rating. If a future PR wires the field through,
    it must update this test (the contract changes)."""
    import inspect
    from mnemos.api.routes import memories

    src = inspect.getsource(memories.update_memory)
    assert "quality_rating" not in src, (
        "update_memory handler now references quality_rating — "
        "wire the field back into MemoryUpdateRequest if you intend "
        "for clients to set it via the PATCH route."
    )

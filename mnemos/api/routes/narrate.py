"""APOLLO narration endpoint — dense → prose readback for human display.

GET /v1/memories/{memory_id}/narrate[?format=prose|dense]

For memories whose winning compression variant is APOLLO's dense
form, expand back to prose. Non-APOLLO winners pass through
unchanged (ARTEMIS output is already prose-shaped). When no
winning variant exists, return the raw memory content.

v3.3 S-II ships the rule-based narrator dispatcher (see
``mnemos.domain.compression.apollo.narrate_encoded``). S-III replaces with a
cached small-LLM call behind the same seam — the HTTP surface is
stable across that change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import mnemos.core.lifecycle as _lc  # noqa: F401  (kept for symmetry; helpers consume _lc)
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import backend_or_503, maybe_set_pg_rls
from mnemos.core.extras import is_extra_installed, missing_extra_detail
from mnemos.core.security import is_root
from mnemos.persistence.visibility import VisibilityFilter

router = APIRouter(prefix="/v1/memories", tags=["narrate"])


# ── response model ────────────────────────────────────────────────────────


class NarrateResponse(BaseModel):
    """Response for GET /v1/memories/{id}/narrate.

    `source` distinguishes where the returned content came from:
      * ``narrated``            — APOLLO dense form expanded to prose
      * ``variant_passthrough`` — non-APOLLO winning variant
                                  (e.g. ARTEMIS output, already prose)
      * ``variant_dense``       — raw dense form when format=dense
      * ``raw``                 — no winning variant; raw memories.content
    """

    memory_id: str
    format: str = Field(..., description="prose | dense")
    content: str
    source: str
    engine_id: Optional[str] = None
    engine_version: Optional[str] = None


def _narrate_apollo(encoded: str) -> str:
    if not is_extra_installed("apollo"):
        raise HTTPException(
            status_code=503,
            detail=missing_extra_detail("apollo", label="APOLLO"),
        )
    from mnemos.domain.compression.apollo import narrate_encoded

    return narrate_encoded(encoded)


def _render_narration(memory_row: dict, variant_row: dict | None, format: str) -> str:
    """Pure dispatch — no I/O. Picks raw / passthrough / narrated
    based on (variant present, engine, format).

    Pulled out so both the backend-neutral helpers and tests can
    share the same branch logic without duplicating it.
    """
    raw_content = memory_row.get("content") or ""
    if format == "dense":
        if variant_row is None:
            return raw_content
        return variant_row["compressed_content"] or ""
    # prose
    if variant_row is None:
        return raw_content
    if variant_row["engine_id"] != "apollo":
        return variant_row["compressed_content"] or ""
    return _narrate_apollo(variant_row["compressed_content"])


async def build_narration_body(
    backend, tx, memory_row: dict, format: str,
) -> str:
    """Build just the narrated body string from a pre-fetched memory row.

    Used by ``GET /v1/memories/{id}`` content negotiation, where the
    visibility-gated memory lookup is already done by the canonical
    JSON path (``backend.memories.get_memory`` under
    ``VisibilityFilter.for_read``). This helper does NOT re-check
    tenancy — that is the caller's responsibility — and only fetches
    the winning compressed variant before dispatching prose / dense
    / passthrough through ``_render_narration``.

    ``memory_row`` must be a mapping that exposes ``id`` and
    ``content`` keys (matches both the asyncpg Row shape and the
    backend-neutral row shape). ``format`` must be
    ``"prose"`` or ``"dense"``.

    Backend-neutral: uses ``backend.compression.fetch_compressed_
    variant_by_memory_id(tx, memory_id)`` under the caller's
    transaction so SQLite-backed profiles (no asyncpg pool) work
    identically to Postgres-backed profiles.
    """
    variant_row = await backend.compression.fetch_compressed_variant_by_memory_id(
        tx, memory_row["id"],
    )
    return _render_narration(memory_row, variant_row, format)


# ── endpoint ──────────────────────────────────────────────────────────────


def _build_narrate_response(
    memory_id: str,
    memory_row: dict,
    variant_row: dict | None,
    format: str,
) -> NarrateResponse:
    """Compose the rich NarrateResponse model.

    Same dispatch as ``_render_narration`` but emits the structured
    response object that ``/v1/memories/{id}/narrate`` returns
    (``source`` discriminator + engine metadata included).
    """
    raw_content = memory_row.get("content") or ""

    if format == "dense":
        if variant_row is None:
            return NarrateResponse(
                memory_id=memory_id,
                format="dense",
                content=raw_content,
                source="raw",
            )
        return NarrateResponse(
            memory_id=memory_id,
            format="dense",
            content=variant_row["compressed_content"] or "",
            source="variant_dense",
            engine_id=variant_row["engine_id"],
            engine_version=variant_row["engine_version"],
        )

    if variant_row is None:
        return NarrateResponse(
            memory_id=memory_id,
            format="prose",
            content=raw_content,
            source="raw",
        )

    engine_id = variant_row["engine_id"]
    if engine_id != "apollo":
        return NarrateResponse(
            memory_id=memory_id,
            format="prose",
            content=variant_row["compressed_content"] or "",
            source="variant_passthrough",
            engine_id=engine_id,
            engine_version=variant_row["engine_version"],
        )

    return NarrateResponse(
        memory_id=memory_id,
        format="prose",
        content=_narrate_apollo(variant_row["compressed_content"]),
        source="narrated",
        engine_id=engine_id,
        engine_version=variant_row["engine_version"],
    )


async def compute_narrate(
    memory_id: str,
    user: UserContext,
    format: str,
) -> NarrateResponse:
    """Build the narrate response for a memory + caller + format.

    Used by GET /v1/memories/{id}/narrate. Goes through the same
    backend-neutral read contract as GET /v1/memories/{id}: the
    memory lookup is gated by ``VisibilityFilter.for_read`` (admits
    owner, federated, world-readable, and group-readable memories),
    not the narrower owner+namespace gate the v3.3 S-II handler used.
    The winning-variant lookup uses the persistence backend's
    compression repo so SQLite-backed profiles work identically to
    Postgres-backed profiles.

    ``format`` must be ``"prose"`` or ``"dense"``. The caller is
    responsible for validating that — narrate's own pydantic Query
    pattern enforces it on the HTTP edge.

    Raises HTTPException(503) when the persistence backend is
    unavailable and HTTPException(404) when the memory is not
    visible under the caller's READABLE scope. Postgres RLS GUCs
    are applied inside the transaction so RLS-enabled deployments
    cannot fall back to the personal_bypass policy — same defense-
    in-depth as ``GET /v1/memories/{id}``.
    """
    backend = backend_or_503()
    visibility = VisibilityFilter.for_read(
        user, namespace=None if is_root(user) else user.namespace,
    )
    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        memory_row = await backend.memories.get_memory(
            tx, memory_id, visibility=visibility,
        )
        if memory_row is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        variant_row = await backend.compression.fetch_compressed_variant_by_memory_id(
            tx, memory_id,
        )

    return _build_narrate_response(memory_id, memory_row, variant_row, format)


@router.get("/{memory_id}/narrate", response_model=NarrateResponse)
async def narrate(
    memory_id: str,
    format: str = Query(
        "prose",
        pattern="^(prose|dense)$",
        description=(
            "prose → expand APOLLO dense forms to human-readable text; "
            "non-APOLLO variants passed through unchanged. "
            "dense → return the raw winning-variant content verbatim; "
            "falls back to raw memory content when no variant exists."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Expand APOLLO dense forms back to prose for human reading.

    Always safe to call — missing variants degrade gracefully to the
    raw memory content, non-APOLLO variants pass through unchanged,
    unknown dense shapes fall through verbatim rather than raising.

    Tenancy: non-root callers filtered by ``owner_id + namespace``.
    404 when the memory does not exist under the caller's tenancy
    scope — matches the visibility rules of GET /v1/memories/{id}.
    """
    return await compute_narrate(memory_id, user, format)

"""APOLLO narration endpoint — dense → prose readback for human display.

GET /v1/memories/{memory_id}/narrate[?format=prose|dense]

For memories whose winning compression variant is APOLLO's dense
form, expand back to prose. Non-APOLLO winners pass through
unchanged (ARTEMIS output is already prose-shaped). When no
winning variant exists, return the raw memory content.

v3.3 S-II ships the rule-based narrator dispatcher (see
``compression.apollo.narrate_encoded``). S-III replaces with a
cached small-LLM call behind the same seam — the HTTP surface is
stable across that change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.security import is_root
from compression.apollo import narrate_encoded


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


async def _fetch_memory(conn, memory_id: str, user: UserContext) -> Optional[dict]:
    """Fetch a memory subject to the two-axis tenancy gate. Root
    bypasses both owner_id and namespace filters."""
    if is_root(user):
        return await conn.fetchrow(
            "SELECT id, content FROM memories WHERE id = $1",
            memory_id,
        )
    return await conn.fetchrow(
        "SELECT id, content FROM memories "
        "WHERE id = $1 AND owner_id = $2 AND namespace = $3",
        memory_id, user.user_id, user.namespace,
    )


async def _fetch_winning_variant(conn, memory_id: str) -> Optional[dict]:
    """Return the current winning variant row or None."""
    return await conn.fetchrow(
        """
        SELECT engine_id, engine_version, compressed_content
        FROM memory_compressed_variants
        WHERE memory_id = $1
        """,
        memory_id,
    )


# ── endpoint ──────────────────────────────────────────────────────────────


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
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        memory_row = await _fetch_memory(conn, memory_id, user)
        if memory_row is None:
            raise HTTPException(status_code=404, detail="Memory not found")

        variant_row = await _fetch_winning_variant(conn, memory_id)

        # Dense-form request: either return the raw winning variant or
        # fall back to raw memory content when no variant exists.
        if format == "dense":
            if variant_row is None:
                return NarrateResponse(
                    memory_id=memory_id,
                    format="dense",
                    content=memory_row["content"] or "",
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

        # Prose-form request: narrate APOLLO outputs; pass through
        # ARTEMIS (already prose-shaped); fall back to raw on
        # missing variant.
        if variant_row is None:
            return NarrateResponse(
                memory_id=memory_id,
                format="prose",
                content=memory_row["content"] or "",
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

        narrated = narrate_encoded(variant_row["compressed_content"])
        return NarrateResponse(
            memory_id=memory_id,
            format="prose",
            content=narrated,
            source="narrated",
            engine_id=engine_id,
            engine_version=variant_row["engine_version"],
        )

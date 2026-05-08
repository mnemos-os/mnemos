"""Memory Portability Format (MPF) export / import endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.domain.portability.export import (
    _EXPORT_HARD_LIMIT,
    _EXPORT_SIDECAR_HARD_LIMIT,
    export_memories as _export_memories,
)
from mnemos.domain.portability.import_ import import_memories as _import_memories
from mnemos.domain.portability.schemas import (
    ImportStats,
    MPFEnvelope,
    MPFRecord,
    MEMORY_PAYLOAD_VERSION,
    MPF_VERSION,
    MPF_VERSION_PREFIX,
    SOURCE_SYSTEM,
    SOURCE_VERSION,
)
from mnemos.domain.portability.version_topology import _topo_sort_versions

router = APIRouter(prefix="/v1", tags=["portability"])

__all__ = [
    "ImportStats", "MEMORY_PAYLOAD_VERSION", "MPFEnvelope", "MPFRecord",
    "MPF_VERSION", "MPF_VERSION_PREFIX", "SOURCE_SYSTEM", "SOURCE_VERSION",
    "_EXPORT_HARD_LIMIT", "_EXPORT_SIDECAR_HARD_LIMIT", "_topo_sort_versions",
    "export_memories", "import_memories", "router",
]


@router.get(
    "/export",
    response_model=MPFEnvelope,
    # CRITICAL: exclude None values from the JSON. Without this, the new
    # optional v0.2 record fields (provenance, valid_time_*, transaction_time)
    # serialize as `null` on v0.1 envelopes — breaking backward compat.
    # And the envelope's optional sidecars (kg_triples, memory_versions,
    # compression_manifest, deletion_log) serialize as `null` when absent —
    # the published v0.2 schema (mnemos-os/mpf/blob/master/schema/mpf-v0.2.json)
    # defines them as arrays when present, never null.
    response_model_exclude_none=True,
)
async def export_memories(
    category: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=_EXPORT_HARD_LIMIT)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
    owner_id: Annotated[
        Optional[str],
        Query(
            min_length=1,
            description=(
                "Tenant owner_id to scope export. Omit for the caller's "
                "own tenant (root may pass an explicit owner_id to target "
                "another tenant). Empty string is rejected with 422."
            ),
        ),
    ] = None,
    namespace: Annotated[
        Optional[str],
        Query(
            min_length=1,
            description=(
                "Tenant namespace to scope export. Omit for the caller's "
                "own namespace (root may pass an explicit namespace to "
                "target another tenant). Empty string is rejected with 422."
            ),
        ),
    ] = None,
    include_sidecars: Annotated[bool, Query()] = False,
    include_unattached_kg: Annotated[bool, Query()] = False,
    mpf_version: Annotated[
        Optional[str],
        Query(
            description=(
                "Output MPF version. Default 0.1.x (legacy, backward-compatible). "
                "Pass 0.2 / 0.2.0 to emit native v0.2 envelopes with W3C PROV-DM "
                "provenance + bi-temporal fields per the published mnemos-os/mpf v0.2.0 spec."
            ),
        ),
    ] = None,
    deletion_log_from: Annotated[
        Optional[datetime],
        Query(
            description=(
                "Optional ISO-8601 lower bound on deletion_log.executed_at "
                "(inclusive). Must be timezone-aware (e.g. ...Z or ...+00:00). "
                "Used with mpf_version=0.2 + include_sidecars=true to chunk "
                "audit trails larger than the per-envelope cap."
            ),
        ),
    ] = None,
    deletion_log_to: Annotated[
        Optional[datetime],
        Query(
            description=(
                "Optional ISO-8601 upper bound on deletion_log.executed_at "
                "(inclusive). Must be timezone-aware."
            ),
        ),
    ] = None,
    deletion_log_cursor: Annotated[
        Optional[str],
        Query(
            min_length=1,
            description=(
                "Opaque keyset cursor for chunked deletion_log export. "
                "Pass back the `deletion_log_next_cursor` value from the "
                "previous /v1/export response to fetch the next page. "
                "Required for the bulk-wipe edge case where >50k tombstones "
                "share the same executed_at and time-window pagination "
                "alone cannot split them. Do not edit the token — it's "
                "opaque base64-JSON. To start a fresh pagination, OMIT "
                "this parameter entirely — passing an empty string is "
                "rejected with 422."
            ),
        ),
    ] = None,
    user: UserContext = Depends(get_current_user),
):
    require_postgres_pool_or_503(route_label="GET /v1/export")

    # Reject naive datetimes — timestamptz comparison would otherwise
    # be interpreted in DB session tz and silently shift the export
    # window. Same enforcement for inverted ranges.
    for name, value in (
        ("deletion_log_from", deletion_log_from),
        ("deletion_log_to", deletion_log_to),
    ):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} must be timezone-aware (e.g. ...Z or "
                    f"...+00:00). Naive datetimes would be interpreted "
                    f"in the DB session timezone and silently shift "
                    f"the audit window."
                ),
            )
    if (
        deletion_log_from is not None
        and deletion_log_to is not None
        and deletion_log_from > deletion_log_to
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"deletion_log_from ({deletion_log_from.isoformat()}) "
                f"must be <= deletion_log_to ({deletion_log_to.isoformat()})."
            ),
        )

    # Reject cursor + window-param combinations. The cursor is
    # self-contained — it carries the original page-1 window — so
    # accepting both creates ambiguity (which wins?) and an attack
    # surface for silently broadening or narrowing an audit slice.
    # Operators paginating must round-trip the cursor verbatim and
    # NOT re-send window params on subsequent pages.
    if deletion_log_cursor is not None and (
        deletion_log_from is not None or deletion_log_to is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "deletion_log_cursor cannot be combined with "
                "deletion_log_from / deletion_log_to. The cursor is "
                "self-contained — it carries the original page-1 "
                "window. Pass the cursor verbatim on subsequent "
                "pages and omit the window params."
            ),
        )

    # Reject cursor + tenant-scope params (owner_id / namespace) for
    # the same reason: the cursor binds the page-1 tenant scope. A
    # root operator paginating with a cursor for owner=A who passes
    # owner=B on page 2 would otherwise apply A's keyset position to
    # B's data, silently mixing audit slices across tenants. Forcing
    # the operator to omit owner_id / namespace on subsequent pages
    # makes the scope binding explicit.
    if deletion_log_cursor is not None and (
        owner_id is not None or namespace is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "deletion_log_cursor cannot be combined with "
                "owner_id / namespace. The cursor binds the original "
                "page-1 tenant scope. Pass the cursor verbatim on "
                "subsequent pages and omit owner_id / namespace."
            ),
        )

    # Reject deletion_log params when the surface that consumes them
    # is disabled. Without these guards the route silently 200s with
    # no deletion_log + no next_cursor, and an operator paginating
    # in a loop would terminate believing they had drained the
    # audit log when in fact the query never ran. Affects:
    # (a) include_sidecars=false: the deletion_log fetch is skipped
    #     entirely (gated under the include_sidecars block)
    # (b) mpf_version != 0.2: deletion_log is a v0.2-only sidecar;
    #     v0.1 envelopes don't carry it
    deletion_log_params_present = (
        deletion_log_cursor is not None
        or deletion_log_from is not None
        or deletion_log_to is not None
    )
    if deletion_log_params_present and not include_sidecars:
        raise HTTPException(
            status_code=400,
            detail=(
                "deletion_log_cursor / deletion_log_from / "
                "deletion_log_to require include_sidecars=true. "
                "The deletion_log surface is gated on the sidecars "
                "block; without it, the params are silently ignored "
                "and pagination loops can terminate prematurely."
            ),
        )
    if deletion_log_params_present:
        # Resolve target version up front to surface the v0.1
        # rejection here rather than letting the request proceed
        # to a no-op return. mpf_version is a query string; treat
        # None or "0.1" as v0.1.
        target = (mpf_version or "0.1").strip()
        if not (target.startswith("0.2") or target == "0.2"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "deletion_log_cursor / deletion_log_from / "
                    "deletion_log_to are v0.2-only fields. Pass "
                    "mpf_version=0.2 (or omit the deletion_log "
                    "params on a v0.1 export)."
                ),
            )

    async with _lc.get_pool_manager().acquire() as conn:
        return await _export_memories(
            conn,
            user=user,
            category=category,
            limit=limit,
            offset=offset,
            owner_id=owner_id,
            namespace=namespace,
            include_sidecars=include_sidecars,
            include_unattached_kg=include_unattached_kg,
            mpf_version=mpf_version,
            deletion_log_from=(
                deletion_log_from.isoformat() if deletion_log_from else None
            ),
            deletion_log_to=(
                deletion_log_to.isoformat() if deletion_log_to else None
            ),
            deletion_log_cursor=deletion_log_cursor,
        )


@router.post("/import", response_model=ImportStats, status_code=200)
async def import_memories(
    envelope: Annotated[MPFEnvelope, Body()],
    preserve_owner: Annotated[bool, Query()] = False,
    user: UserContext = Depends(get_current_user),
):
    require_postgres_pool_or_503(route_label="POST /v1/import")

    async with _lc.get_pool_manager().acquire() as conn:
        return await _import_memories(
            conn,
            envelope=envelope,
            preserve_owner=preserve_owner,
            user=user,
        )

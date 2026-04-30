"""Memory Portability Format (MPF) export / import endpoints."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
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


@router.get("/export", response_model=MPFEnvelope)
async def export_memories(
    category: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=_EXPORT_HARD_LIMIT)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
    owner_id: Annotated[Optional[str], Query()] = None,
    namespace: Annotated[Optional[str], Query()] = None,
    include_sidecars: Annotated[bool, Query()] = False,
    include_unattached_kg: Annotated[bool, Query()] = False,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

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
        )


@router.post("/import", response_model=ImportStats, status_code=200)
async def import_memories(
    envelope: Annotated[MPFEnvelope, Body()],
    preserve_owner: Annotated[bool, Query()] = False,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc.get_pool_manager().acquire() as conn:
        return await _import_memories(
            conn,
            envelope=envelope,
            preserve_owner=preserve_owner,
            user=user,
        )

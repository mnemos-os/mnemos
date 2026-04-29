"""MPF export orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from mnemos.core.security import is_root
from mnemos.db import portability_repo as repo

from .schemas import MPF_VERSION, SOURCE_SYSTEM, SOURCE_VERSION, MPFEnvelope
from .serializers import (
    _compression_variant_to_entry,
    _kg_triple_to_entry,
    _memory_to_record,
    _memory_version_to_entry,
)
from .version_topology import _topo_sort_versions

_EXPORT_HARD_LIMIT = 10_000
_EXPORT_SIDECAR_HARD_LIMIT = 50_000


def _enforce_sidecar_cap(rows, surface: str) -> None:
    if len(rows) > _EXPORT_SIDECAR_HARD_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{surface} export exceeds the per-surface hard limit of "
                f"{_EXPORT_SIDECAR_HARD_LIMIT} rows for one envelope. Narrow "
                "the slice (filter by category, owner_id, namespace, or a "
                "smaller `limit`) and re-export, or split the export into "
                "multiple chunks."
            ),
        )


async def export_memories(
    conn,
    *,
    user,
    category: Optional[str],
    limit: int,
    offset: int,
    owner_id: Optional[str],
    namespace: Optional[str],
    include_sidecars: bool,
    include_unattached_kg: bool = False,
) -> MPFEnvelope:
    if is_root(user):
        effective_owner = owner_id
        effective_ns = namespace
    else:
        if owner_id and owner_id != user.user_id:
            raise HTTPException(status_code=403, detail="cross-owner export requires root")
        if namespace and namespace != user.namespace:
            raise HTTPException(status_code=403, detail="cross-namespace export requires root")
        effective_owner = user.user_id
        effective_ns = user.namespace

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        rows = await repo.fetch_memory_export(
            conn,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            category=category,
            limit=limit,
            offset=offset,
        )
        records = [_memory_to_record(dict(r)) for r in rows]

        kg_triples_out: Optional[List[Dict[str, Any]]] = None
        memory_versions_out: Optional[List[Dict[str, Any]]] = None
        compression_manifest_out: Optional[List[Dict[str, Any]]] = None

        if include_sidecars:
            memory_ids = [r["id"] for r in rows]
            kg_rows = await repo.fetch_kg_triples_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                include_unattached=include_unattached_kg,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(kg_rows, "kg_triples")
            kg_triples_out = [_kg_triple_to_entry(dict(r)) for r in kg_rows]

            mv_rows = await repo.fetch_memory_versions_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(mv_rows, "memory_versions")
            memory_versions_out = _topo_sort_versions(
                [_memory_version_to_entry(dict(r)) for r in mv_rows]
            )

            cv_rows = await repo.fetch_compressed_variants_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(cv_rows, "compression_manifest")
            compression_manifest_out = [
                _compression_variant_to_entry(dict(r)) for r in cv_rows
            ]

    return MPFEnvelope(
        mpf_version=MPF_VERSION,
        source_system=SOURCE_SYSTEM,
        source_version=SOURCE_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        record_count=len(records),
        records=records,
        kg_triples=kg_triples_out,
        memory_versions=memory_versions_out,
        compression_manifest=compression_manifest_out,
    )

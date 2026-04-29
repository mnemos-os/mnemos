"""Cross-reference allowlist validation for MPF sidecars."""

from __future__ import annotations

from typing import Dict, Optional

from mnemos.db import portability_repo as repo

from .schemas import MPFEnvelope


async def _build_referenced_memory_allowlist(
    conn,
    envelope: MPFEnvelope,
    *,
    scope_owner: Optional[str] = None,
    scope_namespace: Optional[str] = None,
) -> Dict[str, tuple]:
    referenced: set[str] = set()
    for entry in envelope.kg_triples or []:
        mid = entry.get("memory_id")
        if mid:
            referenced.add(mid)
    for entry in envelope.memory_versions or []:
        rid = entry.get("record_id")
        if rid:
            referenced.add(rid)
    for entry in envelope.compression_manifest or []:
        rid = entry.get("record_id")
        if rid:
            referenced.add(rid)
    if not referenced:
        return {}
    rows = await repo.fetch_referenced_memory_allowlist(
        conn,
        referenced_ids=list(referenced),
        scope_owner=scope_owner,
        scope_namespace=scope_namespace,
    )
    return {r["id"]: (r["owner_id"], r["namespace"]) for r in rows}


def _is_allowed_reference(
    memory_id: Optional[str],
    *,
    effective_owner: str,
    effective_namespace: Optional[str],
    allowlist: Dict[str, tuple],
    require_namespace_match: bool = True,
) -> tuple[bool, str]:
    if memory_id is None:
        return True, ""
    if memory_id not in allowlist:
        return False, (
            f"record_id {memory_id!r} not in caller-owned memory id set; "
            "skipped (cross-tenant attachment refused)"
        )
    actual_owner, actual_ns = allowlist[memory_id]
    if actual_owner != effective_owner:
        return False, (
            f"record_id {memory_id!r} belongs to owner {actual_owner!r}, "
            f"not the sidecar's effective owner {effective_owner!r}; "
            "skipped (cross-tenant attachment refused)"
        )
    if require_namespace_match and actual_ns != effective_namespace:
        return False, (
            f"record_id {memory_id!r} is in namespace {actual_ns!r}, "
            f"not the sidecar's effective namespace {effective_namespace!r}; "
            "skipped (cross-tenant attachment refused)"
        )
    return True, ""

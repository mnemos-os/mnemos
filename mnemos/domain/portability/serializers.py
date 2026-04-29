"""DB-row to MPF-entry serializers."""

from __future__ import annotations

import json
from typing import Any, Dict

from .schemas import MEMORY_PAYLOAD_VERSION, MPFRecord
from .timestamps import _iso


def _memory_to_record(row) -> MPFRecord:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {"_raw": metadata}

    payload: Dict[str, Any] = {
        "content": row.get("content"),
        "category": row.get("category"),
        "subcategory": row.get("subcategory"),
        "created": _iso(row.get("created")),
        "updated": _iso(row.get("updated")),
        "owner_id": row.get("owner_id"),
        "namespace": row.get("namespace"),
        "permission_mode": row.get("permission_mode"),
        "quality_rating": row.get("quality_rating"),
        "source_model": row.get("source_model"),
        "source_provider": row.get("source_provider"),
        "source_session": row.get("source_session"),
        "source_agent": row.get("source_agent"),
        "metadata": metadata,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return MPFRecord(
        id=row["id"],
        kind="memory",
        payload_version=MEMORY_PAYLOAD_VERSION,
        payload=payload,
    )


def _kg_triple_to_entry(row) -> Dict[str, Any]:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {"_raw": metadata}
    entry: Dict[str, Any] = {
        "id": row["id"],
        "predicate": row["predicate"],
        "subject_literal": row.get("subject"),
        "object_literal": row.get("object"),
        "subject_type": row.get("subject_type"),
        "object_type": row.get("object_type"),
        "memory_id": row.get("memory_id"),
        "confidence": row.get("confidence"),
        "valid_from": _iso(row.get("valid_from")),
        "valid_until": _iso(row.get("valid_until")),
        "created": _iso(row.get("created")),
        "owner_id": row.get("owner_id"),
        "namespace": row.get("namespace"),
    }
    if metadata:
        entry["metadata"] = metadata
    return {k: v for k, v in entry.items() if v is not None}


def _memory_version_to_entry(row) -> Dict[str, Any]:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {"_raw": metadata}
    merge_parents = row.get("merge_parents") or None
    if merge_parents is not None:
        merge_parents = [str(p) for p in merge_parents]
    parent_version_id = row.get("parent_version_id")
    if parent_version_id is not None:
        parent_version_id = str(parent_version_id)
    entry: Dict[str, Any] = {
        "id": str(row["id"]),
        "record_id": row["memory_id"],
        "version_num": row["version_num"],
        "commit_hash": row.get("commit_hash"),
        "branch": row.get("branch"),
        "parent_version_id": parent_version_id,
        "merge_parents": merge_parents,
        "content": row["content"],
        "category": row.get("category"),
        "subcategory": row.get("subcategory"),
        "metadata": metadata or None,
        "verbatim_content": row.get("verbatim_content"),
        "owner_id": row.get("owner_id"),
        "namespace": row.get("namespace"),
        "permission_mode": row.get("permission_mode"),
        "source_model": row.get("source_model"),
        "source_provider": row.get("source_provider"),
        "source_session": row.get("source_session"),
        "source_agent": row.get("source_agent"),
        "snapshot_at": _iso(row.get("snapshot_at")),
        "snapshot_by": row.get("snapshot_by"),
        "change_type": row.get("change_type"),
    }
    return {k: v for k, v in entry.items() if v is not None}


def _compression_variant_to_entry(row) -> Dict[str, Any]:
    winner_id = row.get("winner_candidate_id")
    if winner_id is not None:
        winner_id = str(winner_id)
    entry: Dict[str, Any] = {
        "record_id": row["memory_id"],
        "engine_id": row["engine_id"],
        "engine_version": row.get("engine_version"),
        "compressed_content": row.get("compressed_content"),
        "compressed_tokens": row.get("compressed_tokens"),
        "compression_ratio": row.get("compression_ratio"),
        "quality_score": row.get("quality_score"),
        "composite_score": row.get("composite_score"),
        "scoring_profile": row.get("scoring_profile"),
        "judge_model": row.get("judge_model"),
        "selected_at": _iso(row.get("selected_at")),
        "winner_contest_id": winner_id,
        "owner_id": row.get("owner_id"),
    }
    return {k: v for k, v in entry.items() if v is not None}

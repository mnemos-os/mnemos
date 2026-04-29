"""MPF import phases for sidecar tables."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from mnemos.db import portability_repo as repo

from .allowlist import _is_allowed_reference
from .ids import _derive_caller_scoped_uuid, _row_owner_ns
from .schemas import ImportStats
from .timestamps import _parse_iso
from .version_topology import _topo_sort_versions, _validate_version_parents

logger = logging.getLogger(__name__)


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


async def _import_kg_triples(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
    inserted_record_ids: Optional[set] = None,
) -> None:
    surface = "kg_triples"
    if not preserve_owner:
        for entry in sidecar:
            eid = entry.get("id")
            if eid:
                entry["id"] = _derive_caller_scoped_uuid(
                    str(eid),
                    caller_owner=caller_user_id,
                    caller_namespace=caller_namespace,
                    extra="kg_triples",
                )

    for entry in sidecar:
        if not entry.get("id") or not entry.get("predicate"):
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(f"[{surface}] missing required id/predicate; skipped")
            continue
        subject = entry.get("subject_literal") or entry.get("subject_id")
        obj = entry.get("object_literal") or entry.get("object_id")
        if not subject:
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(
                f"[{surface}] {entry['id']}: missing subject_literal/subject_id; skipped"
            )
            continue
        if not obj:
            obj = ""
        row_owner, row_ns = _row_owner_ns(
            entry,
            caller_user_id=caller_user_id,
            caller_namespace=caller_namespace,
            preserve_owner=preserve_owner,
        )
        allowed, reason = _is_allowed_reference(
            entry.get("memory_id"),
            effective_owner=row_owner,
            effective_namespace=row_ns,
            allowlist=allowlist,
        )
        if not allowed:
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(f"[{surface}] {entry['id']}: {reason}")
            continue
        try:
            async with conn.transaction():
                row = await repo.insert_kg_triple(
                    conn,
                    triple_id=entry["id"],
                    subject=subject,
                    predicate=entry["predicate"],
                    obj=obj,
                    subject_type=entry.get("subject_type"),
                    object_type=entry.get("object_type"),
                    valid_from=_parse_iso(entry.get("valid_from")),
                    valid_until=_parse_iso(entry.get("valid_until")),
                    memory_id=entry.get("memory_id"),
                    confidence=entry.get("confidence"),
                    created=_parse_iso(entry.get("created")),
                    owner_id=row_owner,
                    namespace=row_ns,
                )
            if row == "INSERT 0 0":
                existing = await repo.fetch_kg_triple_by_id(conn, entry["id"])
                expected_valid_from = _parse_iso(entry.get("valid_from"))
                expected_valid_until = _parse_iso(entry.get("valid_until"))
                expected_created = _parse_iso(entry.get("created"))
                referenced_memory_id = entry.get("memory_id")
                fresh_memory = (
                    inserted_record_ids is not None
                    and referenced_memory_id is not None
                    and referenced_memory_id in inserted_record_ids
                )
                tolerate_valid_from = not fresh_memory and entry.get("valid_from") is None
                tolerate_created = not fresh_memory and entry.get("created") is None
                if existing is None or (
                    existing["subject"] != subject
                    or existing["predicate"] != entry["predicate"]
                    or existing["object"] != obj
                    or existing["subject_type"] != entry.get("subject_type")
                    or existing["object_type"] != entry.get("object_type")
                    or existing["memory_id"] != entry.get("memory_id")
                    or existing["confidence"]
                    != (entry["confidence"] if entry.get("confidence") is not None else 1.0)
                    or existing["owner_id"] != row_owner
                    or existing["namespace"] != row_ns
                    or (not tolerate_valid_from and existing["valid_from"] != expected_valid_from)
                    or existing["valid_until"] != expected_valid_until
                    or (not tolerate_created and existing["created"] != expected_created)
                ):
                    _bump(stats.sidecars_failed, surface)
                    stats.errors.append(
                        f"[{surface}] {entry['id']}: existing row doesn't match envelope "
                        "claim (likely stale or orphaned triple); rejected"
                    )
                    continue
                _bump(stats.sidecars_skipped, surface)
            else:
                _bump(stats.sidecars_imported, surface)
        except Exception as exc:
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(f"[{surface}] {entry['id']}: {type(exc).__name__}: {exc}")
            logger.exception("MPF kg_triples import failed for entry %s", entry.get("id"))


async def _restore_memory_branches(
    conn,
    memory_ids: List[str],
    *,
    authorized_version_uuids: Optional[List[str]] = None,
) -> None:
    if not memory_ids:
        return
    if authorized_version_uuids is not None and not authorized_version_uuids:
        return
    rows = await repo.fetch_memory_branch_heads(
        conn,
        memory_ids,
        authorized_version_uuids=authorized_version_uuids,
    )
    for r in rows:
        await repo.upsert_memory_branch_head(
            conn,
            memory_id=r["memory_id"],
            branch=r["branch"],
            head_version_id=r["head_version_id"],
        )


async def _import_memory_versions(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
    inserted_record_ids: Optional[set] = None,
) -> tuple:
    surface = "memory_versions"
    authorized_record_ids: set = set()
    failed_record_ids: set = set()
    authorized_version_uuids: set = set()
    freshly_inserted_version_uuids: set = set()

    non_root_pk_rewrite = not preserve_owner
    version_id_remap: Dict[str, str] = {}
    if non_root_pk_rewrite:
        for entry in sidecar:
            eid = entry.get("id")
            if eid:
                version_id_remap[str(eid)] = _derive_caller_scoped_uuid(
                    str(eid),
                    caller_owner=caller_user_id,
                    caller_namespace=caller_namespace,
                    extra="memory_versions",
                )
        for entry in sidecar:
            if entry.get("id") in version_id_remap:
                entry["id"] = version_id_remap[entry["id"]]
            pv = entry.get("parent_version_id")
            if pv and str(pv) in version_id_remap:
                entry["parent_version_id"] = version_id_remap[str(pv)]
            mp_list = entry.get("merge_parents")
            if mp_list:
                entry["merge_parents"] = [
                    version_id_remap.get(str(mp), str(mp)) for mp in mp_list
                ]
            ch = entry.get("commit_hash")
            if ch:
                entry["commit_hash"] = hashlib.sha256(
                    b"\x00".join(
                        [
                            caller_user_id.encode("utf-8"),
                            caller_namespace.encode("utf-8"),
                            str(ch).encode("utf-8"),
                        ]
                    )
                ).hexdigest()

    sidecar = _topo_sort_versions(sidecar)
    in_envelope_index = {str(e["id"]): e for e in sidecar if e.get("id")}
    for entry in sidecar:
        record_id_for_tracking = entry.get("record_id")
        for required in ("id", "record_id", "version_num", "content"):
            if entry.get(required) in (None, ""):
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] missing required field {required!r}; skipped")
                if record_id_for_tracking:
                    failed_record_ids.add(record_id_for_tracking)
                break
        else:
            row_owner, row_ns = _row_owner_ns(
                entry,
                caller_user_id=caller_user_id,
                caller_namespace=caller_namespace,
                preserve_owner=preserve_owner,
            )
            allowed, reason = _is_allowed_reference(
                entry.get("record_id"),
                effective_owner=row_owner,
                effective_namespace=row_ns,
                allowlist=allowlist,
            )
            if not allowed:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry['id']}: {reason}")
                failed_record_ids.add(entry["record_id"])
                continue

            parent_uuids: List[str] = []
            if entry.get("parent_version_id"):
                parent_uuids.append(str(entry["parent_version_id"]))
            for mp in entry.get("merge_parents") or []:
                if mp:
                    parent_uuids.append(str(mp))
            if parent_uuids:
                require_in_envelope = (
                    inserted_record_ids is not None
                    and entry["record_id"] in inserted_record_ids
                )
                ok, bad = await _validate_version_parents(
                    conn,
                    parent_uuids,
                    expected_record_id=entry["record_id"],
                    effective_owner=row_owner,
                    effective_ns=row_ns,
                    in_envelope_index=in_envelope_index,
                    preserve_owner=preserve_owner,
                    require_in_envelope=require_in_envelope,
                    freshly_inserted_uuids=freshly_inserted_version_uuids,
                )
                if not ok:
                    _bump(stats.sidecars_failed, surface)
                    stats.errors.append(
                        f"[{surface}] {entry['id']}: parent_version_id/"
                        f"merge_parents reference foreign-tenant or foreign-record "
                        f"version(s) {bad}; rejected"
                    )
                    failed_record_ids.add(entry["record_id"])
                    continue

            metadata = entry.get("metadata") or {}
            try:
                async with conn.transaction():
                    row = await repo.insert_memory_version(
                        conn,
                        version_id=entry["id"],
                        memory_id=entry["record_id"],
                        version_num=entry["version_num"],
                        content=entry["content"],
                        category=entry.get("category"),
                        subcategory=entry.get("subcategory"),
                        metadata_json=json.dumps(metadata),
                        verbatim_content=entry.get("verbatim_content"),
                        owner_id=row_owner,
                        namespace=row_ns,
                        permission_mode=entry.get("permission_mode"),
                        source_model=entry.get("source_model"),
                        source_provider=entry.get("source_provider"),
                        source_session=entry.get("source_session"),
                        source_agent=entry.get("source_agent"),
                        snapshot_at=_parse_iso(entry.get("snapshot_at")),
                        snapshot_by=entry.get("snapshot_by"),
                        change_type=entry.get("change_type"),
                        commit_hash=entry.get("commit_hash"),
                        parent_version_id=entry.get("parent_version_id"),
                        branch=entry.get("branch"),
                        merge_parents=entry.get("merge_parents"),
                    )
                if row == "INSERT 0 0":
                    existing = await repo.fetch_memory_version_by_id(conn, entry["id"])
                    expected_parent = (
                        str(entry["parent_version_id"])
                        if entry.get("parent_version_id")
                        else None
                    )
                    expected_branch = entry.get("branch") or "main"
                    expected_merge_parents = entry.get("merge_parents") or None
                    expected_change_type = entry.get("change_type") or "create"
                    expected_permission_mode = entry.get("permission_mode") or 600
                    actual_merge_parents = existing["merge_parents"] if existing else None

                    def _norm_mp(mp):
                        if not mp:
                            return None
                        return [str(x) for x in mp]

                    def _norm_jsonb(j):
                        if j is None:
                            return None
                        if isinstance(j, str):
                            try:
                                return json.loads(j)
                            except Exception:
                                return j
                        return j

                    expected_metadata = entry.get("metadata") or {}
                    actual_metadata = _norm_jsonb(existing["metadata"]) if existing else None
                    expected_snapshot_at = _parse_iso(entry.get("snapshot_at"))
                    actual_snapshot_at = existing["snapshot_at"] if existing else None
                    fresh_memory = (
                        inserted_record_ids is not None
                        and entry.get("record_id") in inserted_record_ids
                    )
                    tolerate_snapshot_at = (
                        not fresh_memory and entry.get("snapshot_at") is None
                    )
                    if existing is None or (
                        existing["memory_id"] != entry["record_id"]
                        or existing["owner_id"] != row_owner
                        or existing["namespace"] != row_ns
                        or existing["version_num"] != entry["version_num"]
                        or existing["content"] != entry["content"]
                        or (
                            not entry.get("commit_hash")
                            or existing["commit_hash"] != entry["commit_hash"]
                        )
                        or existing["parent_version_id"] != expected_parent
                        or existing["branch"] != expected_branch
                        or _norm_mp(actual_merge_parents) != _norm_mp(expected_merge_parents)
                        or existing["category"] != entry.get("category")
                        or existing["subcategory"] != entry.get("subcategory")
                        or actual_metadata != expected_metadata
                        or existing["verbatim_content"] != entry.get("verbatim_content")
                        or existing["permission_mode"] != expected_permission_mode
                        or existing["source_model"] != entry.get("source_model")
                        or existing["source_provider"] != entry.get("source_provider")
                        or existing["source_session"] != entry.get("source_session")
                        or existing["source_agent"] != entry.get("source_agent")
                        or (not tolerate_snapshot_at and actual_snapshot_at != expected_snapshot_at)
                        or existing["snapshot_by"] != entry.get("snapshot_by")
                        or existing["change_type"] != expected_change_type
                    ):
                        _bump(stats.sidecars_failed, surface)
                        stats.errors.append(
                            f"[{surface}] {entry['id']}: existing row doesn't match "
                            "envelope claim (likely prior-lifetime stale row); rejected"
                        )
                        failed_record_ids.add(entry["record_id"])
                        continue
                    _bump(stats.sidecars_skipped, surface)
                else:
                    _bump(stats.sidecars_imported, surface)
                    if entry.get("id"):
                        freshly_inserted_version_uuids.add(str(entry["id"]))
                authorized_record_ids.add(entry["record_id"])
                if entry.get("id"):
                    authorized_version_uuids.add(str(entry["id"]))
            except Exception as exc:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry['id']}: {type(exc).__name__}: {exc}")
                logger.exception(
                    "MPF memory_versions import failed for entry %s",
                    entry.get("id"),
                )
                failed_record_ids.add(entry["record_id"])
    return authorized_record_ids, failed_record_ids, authorized_version_uuids


async def _import_compression_manifest(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
    inserted_record_ids: Optional[set] = None,
) -> None:
    surface = "compression_manifest"
    for entry in sidecar:
        for required in ("record_id", "engine_id"):
            if entry.get(required) in (None, ""):
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] missing required field {required!r}; skipped")
                break
        else:
            row_owner, _ = _row_owner_ns(
                entry,
                caller_user_id=caller_user_id,
                caller_namespace=caller_namespace,
                preserve_owner=preserve_owner,
                has_namespace_column=False,
            )
            ref_namespace = None if preserve_owner else caller_namespace
            allowed, reason = _is_allowed_reference(
                entry.get("record_id"),
                effective_owner=row_owner,
                effective_namespace=ref_namespace,
                allowlist=allowlist,
                require_namespace_match=not preserve_owner,
            )
            if not allowed:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry.get('record_id')}: {reason}")
                continue

            winner_id_raw = entry.get("winner_contest_id")
            winner_id: Optional[str] = None
            if winner_id_raw:
                try:
                    winner_id = str(uuid.UUID(str(winner_id_raw)))
                except (ValueError, AttributeError):
                    winner_id = None
            try:
                async with conn.transaction():
                    if winner_id is not None:
                        exists = await repo.compression_candidate_exists(
                            conn,
                            candidate_id=winner_id,
                            memory_id=entry["record_id"],
                            owner_id=row_owner,
                        )
                        if not exists:
                            winner_id = None
                    row = await repo.insert_compressed_variant(
                        conn,
                        memory_id=entry["record_id"],
                        owner_id=row_owner,
                        winner_candidate_id=winner_id,
                        engine_id=entry["engine_id"],
                        engine_version=entry.get("engine_version"),
                        compressed_content=entry.get("compressed_content"),
                        compressed_tokens=entry.get("compressed_tokens"),
                        compression_ratio=entry.get("compression_ratio"),
                        quality_score=entry.get("quality_score"),
                        composite_score=entry.get("composite_score"),
                        scoring_profile=entry.get("scoring_profile"),
                        judge_model=entry.get("judge_model"),
                        selected_at=_parse_iso(entry.get("selected_at")),
                    )
                if row == "INSERT 0 0":
                    existing = await repo.fetch_compressed_variant_by_memory_id(
                        conn,
                        entry["record_id"],
                    )
                    expected_winner = str(winner_id) if winner_id else None
                    expected_scoring = entry.get("scoring_profile") or "balanced"
                    expected_selected_at = _parse_iso(entry.get("selected_at"))
                    fresh_memory = (
                        inserted_record_ids is not None
                        and entry.get("record_id") in inserted_record_ids
                    )
                    tolerate_selected_at = (
                        not fresh_memory and entry.get("selected_at") is None
                    )
                    if existing is None or (
                        existing["owner_id"] != row_owner
                        or existing["winner_candidate_id"] != expected_winner
                        or existing["engine_id"] != entry["engine_id"]
                        or existing["engine_version"] != entry.get("engine_version")
                        or existing["compressed_content"] != entry.get("compressed_content")
                        or existing["compressed_tokens"] != entry.get("compressed_tokens")
                        or existing["compression_ratio"] != entry.get("compression_ratio")
                        or existing["quality_score"] != entry.get("quality_score")
                        or existing["composite_score"] != entry.get("composite_score")
                        or existing["scoring_profile"] != expected_scoring
                        or existing["judge_model"] != entry.get("judge_model")
                        or (not tolerate_selected_at and existing["selected_at"] != expected_selected_at)
                    ):
                        _bump(stats.sidecars_failed, surface)
                        stats.errors.append(
                            f"[{surface}] {entry.get('record_id')}: existing variant "
                            "doesn't match envelope claim (likely stale prior-lifetime "
                            "row); rejected"
                        )
                        continue
                    _bump(stats.sidecars_skipped, surface)
                else:
                    _bump(stats.sidecars_imported, surface)
            except Exception as exc:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(
                    f"[{surface}] {entry.get('record_id')}: {type(exc).__name__}: {exc}"
                )
                logger.exception(
                    "MPF compression_manifest import failed for record_id %s",
                    entry.get("record_id"),
                )

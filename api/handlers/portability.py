"""Memory Portability Format (MPF) export / import endpoints.

Reference implementation of `docs/mpf_v0.1.json` (currently 0.1.1).

Scope of this surface:

  * GET  /v1/export — bundles the caller's memories into a single
    MPF envelope as `kind: memory` records. Non-root callers get
    only their own owner_id + namespace; root may pass query params
    to export any owner/namespace/category slice.

    With `?include_sidecars=true` the envelope also carries
    `kg_triples`, `memory_versions`, and `compression_manifest`
    sidecar arrays scoped to the same owner/namespace and to the
    set of memory ids in the export.

  * POST /v1/import — accepts an MPF envelope and upserts
    `kind: memory` records plus the same three sidecars when
    present. Non-root rewrites every record's owner_id + namespace
    to the caller's identity (you can't smuggle other owners' rows
    in via an import). Root may pass `?preserve_owner=true` to
    honor the envelope's owner_id + namespace fields verbatim —
    useful for migrations between MNEMOS instances.

Forward-compat ratchet: the import handler accepts any envelope
whose mpf_version is `0.1.x`. Newer minor versions can introduce
optional fields without bumping major; existing field validation
is unchanged from 0.1.0.

Deferred to later commits:

  * document / fact / event record kinds — currently routed under
    `unsupported_kinds` on import. Need per-adapter normalization
    (Graphiti / Cognee mislabel these as payload_version mnemos-3.1
    instead of mpf-0.1) before they can land in MNEMOS storage.
  * relations / embeddings sidecars — each is a separate surface
    with its own round-trip rules.
  * JSONL streaming for large corpora (single-file JSON only today;
    tight cap on `limit` keeps request bodies manageable).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["portability"])


# ─── Constants ────────────────────────────────────────────────────────────────

MPF_VERSION = "0.1.1"
MPF_VERSION_PREFIX = "0.1."  # forward-compat: accept any 0.1.x on import
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "mnemos"
from _version import __version__ as SOURCE_VERSION

# Server-side export cap. Anything larger should use the streaming
# JSONL variant — not in this v3.2.0 cut. Prevents a pathological
# full-table export from pinning memory.
_EXPORT_HARD_LIMIT = 10_000


# ─── Pydantic models (wire shape) ────────────────────────────────────────────

class MPFRecord(BaseModel):
    """A single record in an MPF envelope. Discriminated union by `kind`."""

    id: str
    kind: str  # "document" | "memory" | "fact" | "event" (we only emit/accept "memory" today)
    payload_version: str
    payload: Dict[str, Any]


class MPFEnvelope(BaseModel):
    """An MPF v0.1.x file envelope.

    Fields kept optional / additive so this endpoint can consume MPF
    files emitted by other tools (docling, Mem0, Letta) that may
    populate sidecars this handler doesn't process. Unknown record
    kinds are skipped per the spec's forward-compatibility rule.

    The three sidecar fields (`kg_triples`, `memory_versions`,
    `compression_manifest`) are MNEMOS-native and carried as plain
    dicts to keep the wire form schema-driven (see
    `docs/mpf_v0.1.json` $defs) rather than tied to the Pydantic
    model. The handler validates required fields per-row and skips
    malformed rows during import.
    """

    mpf_version: str = MPF_VERSION
    source_system: Optional[str] = SOURCE_SYSTEM
    source_version: Optional[str] = SOURCE_VERSION
    source_instance: Optional[str] = None
    exported_at: Optional[str] = None
    record_count: Optional[int] = None
    records: List[MPFRecord] = Field(default_factory=list)
    kg_triples: Optional[List[Dict[str, Any]]] = None
    memory_versions: Optional[List[Dict[str, Any]]] = None
    compression_manifest: Optional[List[Dict[str, Any]]] = None


class ImportStats(BaseModel):
    """Summary of an import run.

    The top-level `imported` / `skipped` / `failed` counters cover
    `kind: memory` records. The `sidecars_*` dicts break out per-
    sidecar counts (`kg_triples`, `memory_versions`,
    `compression_manifest`) so an operator can tell at a glance
    which surface had write activity. `errors` aggregates per-row
    failure messages across all surfaces with a `[<surface>]`
    prefix on sidecar-originating entries.
    """

    imported: int
    skipped: int
    failed: int
    unsupported_kinds: Dict[str, int] = Field(default_factory=dict)
    sidecars_imported: Dict[str, int] = Field(default_factory=dict)
    sidecars_skipped: Dict[str, int] = Field(default_factory=dict)
    sidecars_failed: Dict[str, int] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


def _memory_to_record(row) -> MPFRecord:
    """Shape a memories-row dict into an MPFRecord(kind='memory').

    The payload is the MNEMOS v3.1 native memory schema as-is
    (content + category + provenance + tenancy fields). An importer
    running against a different MNEMOS version keys off
    payload_version to decide what to do with it.
    """
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
    # Strip None entries to keep the envelope tidy — importers default
    # missing fields via the schema, and nulls on absent columns
    # inflate envelope size noticeably at 10k rows.
    payload = {k: v for k, v in payload.items() if v is not None}

    return MPFRecord(
        id=row["id"],
        kind="memory",
        payload_version=MEMORY_PAYLOAD_VERSION,
        payload=payload,
    )


def _iso(value) -> Optional[str]:
    """Render a DB timestamp value as an RFC 3339 / ISO 8601 string,
    or None when the source is None. Top-level helper so the sidecar
    mappers can share it."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _kg_triple_to_entry(row) -> Dict[str, Any]:
    """Convert a `kg_triples` DB row into an MPF kg_triple sidecar
    entry. The DB stores subject/object as plain TEXT with optional
    *_type discriminators; we emit them as `subject_literal` /
    `object_literal` and pass the type tags through verbatim. An
    importer with a memories table can promote literals back to
    record references when the literal value matches a known id."""
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
    # Prune Nones to keep the envelope tidy. MPF schema marks all
    # optional fields as nullable so consumers tolerate either form.
    return {k: v for k, v in entry.items() if v is not None}


def _memory_version_to_entry(row) -> Dict[str, Any]:
    """Convert a `memory_versions` DB row into an MPF
    memory_version_entry. Carries the full DAG fields (commit_hash,
    branch, parent_version_id, merge_parents) plus the snapshot's
    tenancy + provenance so the entry round-trips even if the parent
    record isn't in the same envelope (partial export case)."""
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {"_raw": metadata}
    merge_parents = row.get("merge_parents") or None
    if merge_parents is not None:
        # asyncpg renders UUID[] as a list of uuid.UUID; stringify
        # for the JSON payload.
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
    """Convert a `memory_compressed_variants` DB row into an MPF
    compression_manifest_entry. The DB primary key is `memory_id`
    (one winner per memory), and `winner_candidate_id` references the
    contest row that produced this variant."""
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


# ─── GET /v1/export ───────────────────────────────────────────────────────────


async def _fetch_sidecar(
    conn,
    *,
    table: str,
    columns: str,
    memory_id_column: str,
    memory_ids: List[str],
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    bound_to_memories: bool,
) -> Any:
    """Build and execute a sidecar SELECT with optional owner /
    namespace / memory_id filters. Centralizes the placeholder math
    so each sidecar query stays declarative.

    `bound_to_memories=True` means the sidecar rows must reference an
    id in `memory_ids` — this is correct for memory_versions and
    memory_compressed_variants, both of which only exist as
    children of a parent memory. `False` is for tables like
    kg_triples whose rows are first-class (memory_id may be NULL).

    Empty `memory_ids` with `bound_to_memories=True` short-circuits
    to no rows without hitting the DB.
    """
    if bound_to_memories and not memory_ids:
        return []
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1
    if bound_to_memories:
        conditions.append(f"{memory_id_column} = ANY(${idx}::text[])")
        params.append(memory_ids)
        idx += 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT {columns} FROM {table} {where}"
    return await conn.fetch(sql, *params)


@router.get("/export", response_model=MPFEnvelope)
async def export_memories(
    category: Optional[str] = Query(None, description="Filter by category; all categories if unset."),
    limit: int = Query(1000, ge=1, le=_EXPORT_HARD_LIMIT),
    offset: int = Query(0, ge=0),
    owner_id: Optional[str] = Query(None, description="Root only. Export a specific owner's memories; defaults to the caller."),
    namespace: Optional[str] = Query(None, description="Root only. Export a specific namespace; defaults to the caller's."),
    include_sidecars: bool = Query(
        False,
        description=(
            "When true, also emit kg_triples / memory_versions / "
            "compression_manifest sidecars scoped to the same owner "
            "+ namespace and to the memory ids in the export. "
            "Defaults false to keep envelope sizes small for the "
            "common cross-system case."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Export memories as an MPF v0.1.x envelope.

    Non-root callers are scoped to their own owner_id + namespace,
    regardless of the query params. Root callers may target a specific
    owner/namespace slice for migration or support work.

    When `include_sidecars` is true, the envelope also carries the
    three MNEMOS-native sidecars (KG triples, memory-version DAG,
    compression manifest) scoped to the exported memory ids and the
    same owner/namespace.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    if _is_root(user):
        effective_owner = owner_id  # may be None = no filter
        effective_ns = namespace    # may be None = no filter
    else:
        # Non-root cannot exfiltrate outside their own tenancy. If the
        # caller passed owner/namespace params that don't match their
        # identity, reject loudly — silent narrowing would hide the
        # mistake.
        if owner_id and owner_id != user.user_id:
            raise HTTPException(status_code=403, detail="cross-owner export requires root")
        if namespace and namespace != user.namespace:
            raise HTTPException(status_code=403, detail="cross-namespace export requires root")
        effective_owner = user.user_id
        effective_ns = user.namespace

    conditions: List[str] = []
    params: List[Any] = []
    idx = 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = (
        "SELECT id, content, category, subcategory, created, updated, "
        "owner_id, namespace, permission_mode, quality_rating, "
        "source_model, source_provider, source_session, source_agent, "
        "metadata "
        "FROM memories "
        f"{where} "
        f"ORDER BY created ASC "
        f"LIMIT ${idx} OFFSET ${idx + 1}"
    )
    params.extend([limit, offset])

    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

        records = [_memory_to_record(dict(r)) for r in rows]

        kg_triples_out: Optional[List[Dict[str, Any]]] = None
        memory_versions_out: Optional[List[Dict[str, Any]]] = None
        compression_manifest_out: Optional[List[Dict[str, Any]]] = None

        if include_sidecars:
            memory_ids = [r["id"] for r in rows]

            # Owner/namespace filters mirror the memories query so a
            # root caller targeting a single owner gets the matching
            # slice on each sidecar; non-root is locked to their own
            # identity by the time we get here. Sidecars also constrain
            # to the exported memory_ids, so a category-filtered
            # export only carries the sidecar rows for those memories.
            kg_rows = await _fetch_sidecar(
                conn,
                table="kg_triples",
                columns=(
                    "id, subject, predicate, object, subject_type, "
                    "object_type, valid_from, valid_until, memory_id, "
                    "confidence, created, owner_id, namespace"
                ),
                memory_id_column="memory_id",
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                # KG triples can stand alone (memory_id NULL when the
                # triple wasn't extracted from a specific memory), so
                # we filter on owner/namespace only — not on the
                # exported memory id list.
                bound_to_memories=False,
            )
            kg_triples_out = [_kg_triple_to_entry(dict(r)) for r in kg_rows]

            mv_rows = await _fetch_sidecar(
                conn,
                table="memory_versions",
                columns=(
                    "id, memory_id, version_num, content, category, "
                    "subcategory, metadata, verbatim_content, owner_id, "
                    "namespace, permission_mode, source_model, source_provider, "
                    "source_session, source_agent, snapshot_at, snapshot_by, "
                    "change_type, commit_hash, parent_version_id, branch, "
                    "merge_parents"
                ),
                memory_id_column="memory_id",
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                bound_to_memories=True,
            )
            memory_versions_out = [_memory_version_to_entry(dict(r)) for r in mv_rows]

            cv_rows = await _fetch_sidecar(
                conn,
                table="memory_compressed_variants",
                columns=(
                    "memory_id, owner_id, winner_candidate_id, engine_id, "
                    "engine_version, compressed_content, compressed_tokens, "
                    "compression_ratio, quality_score, composite_score, "
                    "scoring_profile, judge_model, selected_at"
                ),
                memory_id_column="memory_id",
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                # memory_compressed_variants has no `namespace` column;
                # tenancy is owner-only here.
                effective_ns=None,
                bound_to_memories=True,
            )
            compression_manifest_out = [_compression_variant_to_entry(dict(r)) for r in cv_rows]

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


# ─── POST /v1/import ──────────────────────────────────────────────────────────


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse for the handful of timestamp fields MPF
    memory payloads carry. Returns None on any failure — the caller
    lets the DB default fire instead of inserting garbage."""
    if not value:
        return None
    try:
        # `fromisoformat` handles "2026-01-15T10:30:00+00:00" and its
        # bare variants. Strip a trailing Z since older pre-3.11
        # Python doesn't accept it (we're on 3.11+ but belt+braces).
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


async def _build_referenced_memory_allowlist(
    conn,
    envelope: "MPFEnvelope",
) -> Dict[str, tuple]:
    """Return {memory_id: (owner_id, namespace)} for every memory_id
    a sidecar in this envelope claims to attach to, looked up in DB.

    This is the set of memories the caller is *capable of* attaching
    sidecars to. Each sidecar import helper then checks per-entry
    whether the effective (post-rewrite) owner/namespace matches
    what's in the DB — that's what blocks a malicious envelope from
    smuggling kg_triples / version history / compressed-content onto
    another tenant's memory_id.

    First-class kg_triples (memory_id absent) are not subject to
    this check. They have no FK to a memory record by design.
    """
    referenced: set = set()
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
    rows = await conn.fetch(
        "SELECT id, owner_id, namespace FROM memories WHERE id = ANY($1::text[])",
        list(referenced),
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
    """Return (allowed, reason). The sidecar is allowed iff the
    referenced memory_id either (a) is None — first-class triple
    case — or (b) exists in the allowlist AND the memory's actual
    owner+namespace matches the post-rewrite effective owner+ns
    that this sidecar would land under.

    `require_namespace_match=False` for compression_manifest, which
    has no namespace column — owner match is sufficient there.
    """
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


def _row_owner_ns(
    entry: Dict[str, Any],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    has_namespace_column: bool = True,
) -> tuple[str, Optional[str]]:
    """Apply the same owner/namespace rewrite rule to a sidecar row
    that the records loop applies to memories. Non-root or
    preserve_owner=false → caller identity. preserve_owner=true →
    honor the entry's fields, falling back to caller identity when
    the entry omits them.

    `has_namespace_column=False` for tables without a namespace
    column (compression manifest); the second element is None.
    """
    if preserve_owner:
        owner = entry.get("owner_id") or caller_user_id
        ns = (entry.get("namespace") or caller_namespace) if has_namespace_column else None
    else:
        owner = caller_user_id
        ns = caller_namespace if has_namespace_column else None
    return owner, ns


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
) -> None:
    """Upsert MPF kg_triples sidecar entries into the kg_triples
    table. Idempotent on `id`. Subject/object literals and id
    references both flatten into the DB's TEXT columns; the
    *_type tags persist as discriminators.
    """
    surface = "kg_triples"
    for entry in sidecar:
        if not entry.get("id") or not entry.get("predicate"):
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(f"[{surface}] missing required id/predicate; skipped")
            continue
        # Per the schema's anyOf, exactly one of subject_id /
        # subject_literal must be present. Prefer the literal because
        # the DB has a single TEXT column; downgrade subject_id to
        # literal form (the importer can't re-resolve a foreign id).
        subject = entry.get("subject_literal") or entry.get("subject_id")
        obj = entry.get("object_literal") or entry.get("object_id")
        if not subject:
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(
                f"[{surface}] {entry['id']}: missing subject_literal/subject_id; skipped"
            )
            continue
        if not obj:
            # Object can be missing in some adapters; default to empty
            # string rather than failing the row outright.
            obj = ""
        row_owner, row_ns = _row_owner_ns(
            entry,
            caller_user_id=caller_user_id,
            caller_namespace=caller_namespace,
            preserve_owner=preserve_owner,
        )
        # Tenant-scope check: the triple's memory_id (when present)
        # must reference a memory whose actual owner+namespace match
        # the post-rewrite owner+ns we'd persist this triple under.
        # Blocks alice from attaching kg_triples to bob's memory_id.
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
            row = await conn.execute(
                """
                INSERT INTO kg_triples (
                    id, subject, predicate, object,
                    subject_type, object_type,
                    valid_from, valid_until,
                    memory_id, confidence, created,
                    owner_id, namespace
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6,
                    COALESCE($7, NOW()), $8,
                    $9, COALESCE($10, 1.0),
                    COALESCE($11, NOW()),
                    $12, $13
                )
                ON CONFLICT (id) DO NOTHING
                """,
                entry["id"], subject, entry["predicate"], obj,
                entry.get("subject_type"), entry.get("object_type"),
                _parse_iso(entry.get("valid_from")),
                _parse_iso(entry.get("valid_until")),
                entry.get("memory_id"),
                entry.get("confidence"),
                _parse_iso(entry.get("created")),
                row_owner, row_ns,
            )
            if row == "INSERT 0 0":
                _bump(stats.sidecars_skipped, surface)
            else:
                _bump(stats.sidecars_imported, surface)
        except Exception as exc:
            _bump(stats.sidecars_failed, surface)
            stats.errors.append(f"[{surface}] {entry['id']}: {type(exc).__name__}: {exc}")
            logger.exception("MPF kg_triples import failed for entry %s", entry.get("id"))


async def _import_memory_versions(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
) -> None:
    """Upsert MPF memory_version_entry sidecar entries into
    memory_versions. Idempotent on `id`. Carries the full DAG
    triplet (commit_hash, parent_version_id, branch); merge_parents
    flows through verbatim as a UUID[]."""
    surface = "memory_versions"
    for entry in sidecar:
        for required in ("id", "record_id", "version_num", "content"):
            if entry.get(required) in (None, ""):
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(
                    f"[{surface}] missing required field {required!r}; skipped"
                )
                break
        else:
            row_owner, row_ns = _row_owner_ns(
                entry,
                caller_user_id=caller_user_id,
                caller_namespace=caller_namespace,
                preserve_owner=preserve_owner,
            )
            # Same tenant-scope check as kg_triples — every version
            # must attach to a memory the caller (post-rewrite) owns.
            allowed, reason = _is_allowed_reference(
                entry.get("record_id"),
                effective_owner=row_owner,
                effective_namespace=row_ns,
                allowlist=allowlist,
            )
            if not allowed:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry['id']}: {reason}")
                continue
            metadata = entry.get("metadata") or {}
            try:
                row = await conn.execute(
                    """
                    INSERT INTO memory_versions (
                        id, memory_id, version_num, content,
                        category, subcategory, metadata, verbatim_content,
                        owner_id, namespace, permission_mode,
                        source_model, source_provider, source_session, source_agent,
                        snapshot_at, snapshot_by, change_type,
                        commit_hash, parent_version_id, branch, merge_parents
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4,
                        $5, $6, $7::jsonb, $8,
                        $9, $10, COALESCE($11, 600),
                        $12, $13, $14, $15,
                        COALESCE($16, NOW()), $17, COALESCE($18, 'create'),
                        $19, $20::uuid, COALESCE($21, 'main'), $22::uuid[]
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    entry["id"], entry["record_id"], entry["version_num"], entry["content"],
                    entry.get("category"), entry.get("subcategory"),
                    json.dumps(metadata), entry.get("verbatim_content"),
                    row_owner, row_ns, entry.get("permission_mode"),
                    entry.get("source_model"), entry.get("source_provider"),
                    entry.get("source_session"), entry.get("source_agent"),
                    _parse_iso(entry.get("snapshot_at")), entry.get("snapshot_by"),
                    entry.get("change_type"),
                    entry.get("commit_hash"), entry.get("parent_version_id"),
                    entry.get("branch"), entry.get("merge_parents"),
                )
                if row == "INSERT 0 0":
                    _bump(stats.sidecars_skipped, surface)
                else:
                    _bump(stats.sidecars_imported, surface)
            except Exception as exc:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry['id']}: {type(exc).__name__}: {exc}")
                logger.exception("MPF memory_versions import failed for entry %s", entry.get("id"))


async def _import_compression_manifest(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
) -> None:
    """Upsert MPF compression_manifest_entry sidecar entries into
    memory_compressed_variants. Primary key is `memory_id` (one
    winner per memory), so re-imports of the same envelope are
    no-ops via ON CONFLICT (memory_id) DO NOTHING.

    Note: this table has no namespace column — tenancy is owner-
    only here, intentionally simpler than memory_versions."""
    surface = "compression_manifest"
    for entry in sidecar:
        for required in ("record_id", "engine_id"):
            if entry.get(required) in (None, ""):
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(
                    f"[{surface}] missing required field {required!r}; skipped"
                )
                break
        else:
            row_owner, _ = _row_owner_ns(
                entry,
                caller_user_id=caller_user_id,
                caller_namespace=caller_namespace,
                preserve_owner=preserve_owner,
                has_namespace_column=False,
            )
            # Same tenant-scope check as the other sidecars; namespace
            # match not required because this table has no namespace
            # column. Owner-only check is the right granularity.
            allowed, reason = _is_allowed_reference(
                entry.get("record_id"),
                effective_owner=row_owner,
                effective_namespace=None,
                allowlist=allowlist,
                require_namespace_match=False,
            )
            if not allowed:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry.get('record_id')}: {reason}")
                continue
            # winner_candidate_id is FK→memory_compression_candidates(id)
            # ON DELETE SET NULL. The manifest sidecar carries the
            # winner ID for traceability but CHARON does not also
            # ship the candidate row, so the FK target may be absent
            # after import. NULL-out when missing so the import
            # doesn't fail on a back-pointer to data that doesn't
            # exist; it can be repaired later by re-running a
            # compression contest. Lossless on the manifest's
            # primary content (engine_id, compressed_content, scores).
            winner_id = entry.get("winner_contest_id")
            if winner_id:
                exists = await conn.fetchval(
                    "SELECT 1 FROM memory_compression_candidates WHERE id = $1::uuid",
                    winner_id,
                )
                if not exists:
                    winner_id = None
            try:
                row = await conn.execute(
                    """
                    INSERT INTO memory_compressed_variants (
                        memory_id, owner_id, winner_candidate_id,
                        engine_id, engine_version, compressed_content,
                        compressed_tokens, compression_ratio,
                        quality_score, composite_score,
                        scoring_profile, judge_model, selected_at
                    )
                    VALUES (
                        $1, $2, $3::uuid,
                        $4, $5, $6,
                        $7, $8,
                        $9, $10,
                        COALESCE($11, 'balanced'), $12,
                        COALESCE($13, NOW())
                    )
                    ON CONFLICT (memory_id) DO NOTHING
                    """,
                    entry["record_id"], row_owner, winner_id,
                    entry["engine_id"], entry.get("engine_version"),
                    entry.get("compressed_content"),
                    entry.get("compressed_tokens"),
                    entry.get("compression_ratio"),
                    entry.get("quality_score"),
                    entry.get("composite_score"),
                    entry.get("scoring_profile"),
                    entry.get("judge_model"),
                    _parse_iso(entry.get("selected_at")),
                )
                if row == "INSERT 0 0":
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


@router.post("/import", response_model=ImportStats, status_code=200)
async def import_memories(
    envelope: MPFEnvelope = Body(..., description="An MPF v0.1 envelope."),
    preserve_owner: bool = Query(
        False,
        description=(
            "Root only. When true, honor the owner_id + namespace on "
            "each incoming record instead of rewriting to the caller's "
            "identity. Required for cross-tenant migrations; refused for "
            "non-root callers even if passed."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Import an MPF envelope.

    Accepts `kind: memory` records and the three MNEMOS-native
    sidecars (`kg_triples`, `memory_versions`,
    `compression_manifest`). Other record kinds (document, fact,
    event) are counted under `unsupported_kinds` and skipped per
    the spec's forward-compatibility rule — they need per-adapter
    payload mapping that's deferred to a follow-up.

    All sidecar inserts run in the same transaction as the
    records, so a fatal error on any surface rolls the whole
    envelope back."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    if not envelope.mpf_version.startswith(MPF_VERSION_PREFIX):
        # Forward-compat ratchet: accept any 0.1.x envelope. Newer
        # minor versions add optional fields that this server may
        # ignore, but the required-fields contract is unchanged
        # within the 0.1 series. A 0.2.x bump would change required
        # contracts and re-introduces strict matching.
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported MPF version {envelope.mpf_version!r}; expected {MPF_VERSION_PREFIX}x",
        )

    if preserve_owner and not _is_root(user):
        raise HTTPException(
            status_code=403, detail="preserve_owner=true requires root",
        )

    stats = ImportStats(
        imported=0, skipped=0, failed=0,
        unsupported_kinds={},
        sidecars_imported={}, sidecars_skipped={}, sidecars_failed={},
        errors=[],
    )

    async with _lc._pool.acquire() as conn:
        async with conn.transaction():
            # When the envelope carries its own memory_versions
            # sidecar, the import is restoring authoritative history.
            # The mnemos_version_snapshot trigger would otherwise
            # synthesize a fresh v1 on every memory INSERT, then
            # collide with the envelope's v1 on the
            # idx_mv_main_linear partial unique index `(memory_id,
            # version_num) WHERE branch='main'`. Suppress the trigger
            # for the duration of this transaction; the envelope IS
            # the version log.
            #
            # Targeted suppression via the `mnemos.suppress_version_snapshot`
            # custom GUC. The trigger creation in
            # db/migrations_v3_4_charon_trigger_guard.sql attaches a
            # WHEN clause that no-ops the three version-snapshot
            # triggers when this GUC is '1'. Custom dot-namespaced
            # GUCs are settable per-transaction by any role — no
            # superuser required, unlike `session_replication_role`,
            # which is what the v3.3 cut originally used. The GUC also
            # leaves FK enforcement and every other user-defined
            # trigger untouched.
            if envelope.memory_versions:
                await conn.execute(
                    "SET LOCAL mnemos.suppress_version_snapshot = '1'"
                )

            for record in envelope.records:
                if record.kind != "memory":
                    stats.unsupported_kinds[record.kind] = (
                        stats.unsupported_kinds.get(record.kind, 0) + 1
                    )
                    continue

                if record.payload_version != MEMORY_PAYLOAD_VERSION:
                    # Payload version mismatch isn't fatal — record the
                    # skip for operator visibility. Migrating payloads
                    # across versions is a follow-up commit.
                    stats.skipped += 1
                    stats.errors.append(
                        f"{record.id}: unsupported payload_version "
                        f"{record.payload_version!r}; expected {MEMORY_PAYLOAD_VERSION}"
                    )
                    continue

                p = record.payload

                if preserve_owner:
                    imported_owner = p.get("owner_id") or user.user_id
                    imported_ns = p.get("namespace") or user.namespace
                else:
                    imported_owner = user.user_id
                    imported_ns = user.namespace

                content = p.get("content")
                if not content or not str(content).strip():
                    stats.failed += 1
                    stats.errors.append(f"{record.id}: empty content; skipped")
                    continue

                category = p.get("category") or "imported"
                subcategory = p.get("subcategory")
                permission_mode = p.get("permission_mode") or 600
                metadata = p.get("metadata") or {}
                quality_rating = p.get("quality_rating") or 75

                # Use the envelope-provided id verbatim. ON CONFLICT DO NOTHING
                # gives us idempotent re-imports — running /v1/export followed
                # by /v1/import against the same DB is a no-op.
                try:
                    row = await conn.execute(
                        """
                        INSERT INTO memories (
                            id, content, category, subcategory, metadata,
                            quality_rating, owner_id, namespace, permission_mode,
                            source_model, source_provider, source_session, source_agent,
                            created, updated
                        )
                        VALUES (
                            $1, $2, $3, $4, $5::jsonb,
                            $6, $7, $8, $9,
                            $10, $11, $12, $13,
                            COALESCE($14, NOW()), COALESCE($15, NOW())
                        )
                        ON CONFLICT (id) DO NOTHING
                        """,
                        record.id, content, category, subcategory,
                        json.dumps(metadata),
                        quality_rating, imported_owner, imported_ns, permission_mode,
                        p.get("source_model"), p.get("source_provider"),
                        p.get("source_session"), p.get("source_agent"),
                        _parse_iso(p.get("created")),
                        _parse_iso(p.get("updated")),
                    )
                    if row == "INSERT 0 0":
                        stats.skipped += 1
                    else:
                        stats.imported += 1
                except Exception as exc:
                    stats.failed += 1
                    stats.errors.append(f"{record.id}: {type(exc).__name__}: {exc}")
                    logger.exception("MPF import failed for record %s", record.id)

            # Build the per-request allowlist of memory_ids the
            # caller may attach sidecars to. Computed AFTER the
            # records loop so any memory just imported in this
            # request is included. The check inside each helper
            # confirms post-rewrite owner+namespace matches the
            # memory's actual ownership in DB — so an envelope
            # cannot smuggle sidecars onto another tenant's
            # memory_id by labeling them with the caller's
            # identity. This is the cross-tenant-attachment fix.
            allowlist = await _build_referenced_memory_allowlist(
                conn, envelope,
            )

            # Sidecars run inside the same transaction so a partial
            # failure rolls everything back. Order: kg_triples first
            # (no FK to memories required), then memory_versions and
            # compression_manifest, both of which reference memories.id.
            if envelope.kg_triples:
                await _import_kg_triples(
                    conn, envelope.kg_triples,
                    caller_user_id=user.user_id,
                    caller_namespace=user.namespace,
                    preserve_owner=preserve_owner,
                    stats=stats,
                    allowlist=allowlist,
                )
            if envelope.memory_versions:
                await _import_memory_versions(
                    conn, envelope.memory_versions,
                    caller_user_id=user.user_id,
                    caller_namespace=user.namespace,
                    preserve_owner=preserve_owner,
                    stats=stats,
                    allowlist=allowlist,
                )
            if envelope.compression_manifest:
                await _import_compression_manifest(
                    conn, envelope.compression_manifest,
                    caller_user_id=user.user_id,
                    caller_namespace=user.namespace,
                    preserve_owner=preserve_owner,
                    stats=stats,
                    allowlist=allowlist,
                )

    logger.info(
        "[MPF] import: user=%s imported=%d skipped=%d failed=%d unsupported=%s sidecars_imported=%s",
        user.user_id, stats.imported, stats.skipped, stats.failed,
        stats.unsupported_kinds, stats.sidecars_imported,
    )
    return stats

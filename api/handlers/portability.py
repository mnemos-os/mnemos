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
import uuid
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
    null_ok: bool = False,
    order_by: Optional[str] = None,
) -> Any:
    """Build and execute a sidecar SELECT with optional owner /
    namespace / memory_id filters. Centralizes the placeholder math
    so each sidecar query stays declarative.

    `bound_to_memories=True` means the sidecar rows must reference an
    id in `memory_ids`. `null_ok=True` extends that to also include
    rows whose `memory_id_column` is NULL — used by kg_triples,
    where some triples are first-class (no memory FK) and some are
    extracted from a specific memory. With `null_ok=False` (default)
    only rows with a matching memory_id come back.

    `bound_to_memories=False` drops the memory-id filter entirely —
    NOT what kg_triples wants in a category-filtered export, since
    that lets attached triples for non-exported memories slip
    through (Codex round-5 finding). Use `True + null_ok=True`
    instead.

    Empty `memory_ids` with `bound_to_memories=True, null_ok=False`
    short-circuits to no rows without hitting the DB. With
    `null_ok=True` the query still runs (NULL memory_ids may match).
    """
    # Empty memory slice short-circuit. The intent: a category-
    # filtered export with no matching memories shouldn't leak
    # all first-class kg_triples in the owner/namespace (round-6
    # finding). But this short-circuit must NOT fire when the
    # caller explicitly opted in via null_ok=True — that's the
    # KG-only migration use case where there are no memories but
    # the caller does want first-class triples (round-9 finding).
    if bound_to_memories and not memory_ids and not null_ok:
        return []
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1
    if bound_to_memories:
        if null_ok and memory_ids:
            conditions.append(
                f"({memory_id_column} IS NULL OR {memory_id_column} = ANY(${idx}::text[]))"
            )
            params.append(memory_ids)
            idx += 1
        elif null_ok:
            # Explicit opt-in with empty slice: first-class only.
            # Combined with owner/namespace filters this still
            # scopes the export to the caller's tenancy.
            conditions.append(f"{memory_id_column} IS NULL")
        else:
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
    order = f"ORDER BY {order_by}" if order_by else ""
    sql = f"SELECT {columns} FROM {table} {where} {order}"
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
    include_unattached_kg: bool = Query(
        False,
        description=(
            "When true (and include_sidecars=true), also include "
            "kg_triples whose memory_id IS NULL — first-class facts "
            "not extracted from a specific memory. Defaults false: "
            "an export of memories M1, M2 carries only triples "
            "attached to M1 or M2, NOT every standalone fact in the "
            "owner/namespace. Set to true for full-corpus exports "
            "(e.g. cross-system migration) where unattached facts "
            "are part of the migration scope."
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
                # KG triples have two flavors: first-class (memory_id
                # NULL, e.g. external Graphiti-style facts) and
                # attached (memory_id pointing at a specific memory
                # they were extracted from).
                #
                # Default behavior (include_unattached_kg=False):
                # only attached triples whose memory_id is in the
                # exported slice. A category- or limit-filtered
                # export of M1+M2 does NOT carry every standalone
                # fact in the owner/namespace, which would leak
                # unrelated data into a partial export.
                #
                # Opt-in (include_unattached_kg=True): also include
                # the NULL-memory_id rows. Used for full-corpus
                # exports / cross-system migrations where first-
                # class facts are part of the intended scope.
                bound_to_memories=True,
                null_ok=include_unattached_kg,
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
                # Topological-stable order — parent rows ship before
                # child rows so the import-side parent_version_id FK
                # check passes even on consumers that import in
                # received order. See _import_memory_versions for
                # the matching defensive sort. Codex round-8 finding.
                order_by="memory_id ASC, branch ASC, version_num ASC",
            )
            # SQL ORDER BY (memory_id, branch, version_num) is a
            # heuristic that breaks for forked branches — feature/v1
            # whose parent_version_id points at main/vN sorts before
            # main/vN. Apply the real Kahn's-algorithm topo sort
            # (defined alongside _import_memory_versions) before
            # emitting, so consumers that import in received order
            # get a topologically-correct envelope without needing
            # the defensive sort our own importer applies.
            memory_versions_out = _topo_sort_versions(
                [_memory_version_to_entry(dict(r)) for r in mv_rows]
            )

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
    *,
    scope_owner: Optional[str] = None,
    scope_namespace: Optional[str] = None,
) -> Dict[str, tuple]:
    """Return {memory_id: (owner_id, namespace)} for every sidecar-
    referenced memory_id, looked up in DB.

    When `scope_owner` and/or `scope_namespace` are provided, the
    SELECT is filtered to that tenancy. Non-root callers always
    pass their own identity here so the lookup is structurally
    scoped — a foreign memory_id simply doesn't appear in the
    result, indistinguishable from a non-existent id (Codex
    round-13 finding: an unscoped lookup combined with a
    descriptive rejection error lets a non-root caller probe
    foreign tenants' memory_ids and learn their owners).

    Root callers with preserve_owner=true pass scope_owner=None
    so the migration/admin path can resolve any memory_id.

    First-class kg_triples (memory_id absent) are not subject to
    this check.
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
    sql = "SELECT id, owner_id, namespace FROM memories WHERE id = ANY($1::text[])"
    params: List[Any] = [list(referenced)]
    if scope_owner is not None:
        sql += " AND owner_id = $2"
        params.append(scope_owner)
        if scope_namespace is not None:
            sql += " AND namespace = $3"
            params.append(scope_namespace)
    elif scope_namespace is not None:
        sql += " AND namespace = $2"
        params.append(scope_namespace)
    rows = await conn.fetch(sql, *params)
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
        # Generic message for both nonexistent and foreign-tenant
        # ids. The caller's allowlist SELECT is structurally scoped
        # to their tenancy (see _build_referenced_memory_allowlist),
        # so the two cases are indistinguishable from here — which
        # is the point: a non-root attacker can't probe for foreign
        # memory_ids via rejection-message inference.
        return False, (
            f"record_id {memory_id!r} not in caller-owned memory id set; "
            "skipped (cross-tenant attachment refused)"
        )
    actual_owner, actual_ns = allowlist[memory_id]
    # The remaining branches only fire for root callers with
    # preserve_owner=true (the unscoped allowlist path). Their
    # descriptive messages are intentional — root is doing
    # migration work and needs the diagnostic to debug envelope
    # tenancy mismatches.
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
        # Per-row SAVEPOINT — a Postgres error on one kg_triple
        # mustn't abort the surrounding import transaction.
        try:
            async with conn.transaction():
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


async def _restore_memory_branches(conn, memory_ids: List[str]) -> None:
    """After memory_versions sidecar import, repopulate memory_branches
    HEAD pointers for the imported records.

    The mnemos_version_snapshot trigger normally upserts memory_branches
    on every memory INSERT, but during a CHARON import the trigger is
    suppressed via the mnemos.suppress_version_snapshot GUC. Without
    this restore step, /log + branch-walk endpoints see the version
    rows but no HEAD pointer, so DAG queries return empty.

    For each imported (memory_id, branch), the HEAD is the version
    row with the highest version_num — same rule the trigger applies
    on update. UPSERT into memory_branches keyed on (memory_id, name).
    """
    if not memory_ids:
        return
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (memory_id, branch)
            memory_id, branch, id AS head_version_id
        FROM memory_versions
        WHERE memory_id = ANY($1::text[])
        ORDER BY memory_id, branch, version_num DESC
        """,
        memory_ids,
    )
    for r in rows:
        await conn.execute(
            """
            INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
            VALUES ($1, $2, $3, NULL)
            ON CONFLICT (memory_id, name) DO UPDATE
            SET head_version_id = EXCLUDED.head_version_id
            """,
            r["memory_id"], r["branch"], r["head_version_id"],
        )


async def _validate_version_parents(
    conn,
    parent_uuids: List[str],
    *,
    expected_record_id: str,
    effective_owner: str,
    effective_ns: Optional[str],
    in_envelope_index: Dict[str, Dict[str, Any]],
) -> tuple:
    """Verify every parent UUID points at a row under the same
    record_id + owner + namespace as the entry being imported.

    DB IS THE SOURCE OF TRUTH. The in-envelope index alone is not
    enough — INSERT ... ON CONFLICT (id) DO NOTHING means an
    envelope-supplied "parent" entry with an id that already exists
    in DB will be SKIPPED. The pre-existing DB row's tenancy is
    what the child's FK actually resolves to. So if an adversary
    crafts a fake parent entry labeled with their tenancy but
    using a known-foreign UUID, the conflict-skip path bypasses
    the in-envelope check and the child links to the foreign row.
    (Codex round-12 finding.)

    Algorithm:
      1. SELECT every parent UUID from memory_versions in DB.
      2. For UUIDs found in DB: DB tenancy is authoritative;
         reject if it doesn't match the entry's effective owner+ns
         + record_id.
      3. For UUIDs only in the envelope (truly new parents):
         the envelope's claim is checked; topological sort guarantees
         these will be inserted before children, so once persisted
         the DB row will reflect the envelope's claim.
      4. UUIDs in neither DB nor envelope: reject (dangling).

    Returns (all_valid, bad_parents_list).
    """
    if not parent_uuids:
        return True, []
    # DB-truth lookup over every parent UUID — including those
    # also referenced by the envelope. The envelope's claim only
    # matters when the UUID is GENUINELY new (not in DB yet).
    rows = await conn.fetch(
        "SELECT id::text AS id, memory_id, owner_id, namespace "
        "FROM memory_versions WHERE id = ANY($1::uuid[])",
        parent_uuids,
    )
    db_truth: Dict[str, tuple] = {
        r["id"]: (r["memory_id"], r["owner_id"], r["namespace"])
        for r in rows
    }
    bad: List[str] = []
    for p in parent_uuids:
        if p in db_truth:
            # DB has this parent. Tenancy is whatever's in DB,
            # NOT what the envelope claims (the envelope's INSERT
            # would no-op via ON CONFLICT). Validate against DB.
            mem_id, owner, ns = db_truth[p]
            if (mem_id != expected_record_id
                    or owner != effective_owner
                    or ns != effective_ns):
                bad.append(p)
            continue
        if p in in_envelope_index:
            ref = in_envelope_index[p]
            ref_record = ref.get("record_id")
            ref_owner = ref.get("owner_id")
            ref_ns = ref.get("namespace")
            same_record = ref_record == expected_record_id
            same_owner = (ref_owner is None) or (ref_owner == effective_owner)
            same_ns = (ref_ns is None) or (ref_ns == effective_ns)
            if not (same_record and same_owner and same_ns):
                bad.append(p)
            continue
        # Not in DB, not in envelope — dangling reference.
        bad.append(p)
    return (not bad), bad


def _topo_sort_versions(sidecar: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Kahn's-algorithm topological sort over a memory_versions
    sidecar list, treating parent_version_id + merge_parents as
    incoming edges.

    Parents that aren't in the sidecar (already in DB, or absent
    entirely) count as zero in-degree edges — they're treated as
    roots from this envelope's POV; their FK will resolve at INSERT
    time against existing DB rows.

    Tie-breaker: lower version_num first, then id-string ASC, so
    the order is deterministic for round-trip reproducibility.

    Cycles (shouldn't happen in a real DAG but defensive): any
    nodes left in the working set after Kahn's terminates get
    appended at the end in tie-broken order. The DB will reject
    them at INSERT time, but the sort itself never deadlocks.
    """
    if not sidecar:
        return []

    # Build the index: id → entry. Some entries may have no id
    # (already counted as failed by the caller's required-field
    # check); skip those for the graph but keep them at the end
    # so the loop still sees them and counts the failure.
    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    for entry in sidecar:
        eid = entry.get("id")
        if eid:
            by_id[str(eid)] = entry
        else:
            no_id.append(entry)

    # Build incoming-edge counts. Only edges whose target is also
    # in this envelope contribute.
    in_degree: Dict[str, int] = {eid: 0 for eid in by_id}
    children: Dict[str, List[str]] = {eid: [] for eid in by_id}
    for eid, entry in by_id.items():
        parents: List[str] = []
        pv = entry.get("parent_version_id")
        if pv:
            parents.append(str(pv))
        for mp in entry.get("merge_parents") or []:
            if mp:
                parents.append(str(mp))
        for p in parents:
            if p in by_id:
                in_degree[eid] += 1
                children[p].append(eid)

    def _key(eid: str) -> tuple:
        e = by_id[eid]
        return (int(e.get("version_num") or 0), eid)

    ready = sorted([eid for eid, d in in_degree.items() if d == 0], key=_key)
    out: List[Dict[str, Any]] = []
    while ready:
        eid = ready.pop(0)
        out.append(by_id[eid])
        for child in children[eid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                # Insert maintaining tie-break order.
                k = _key(child)
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _key(ready[mid]) < k:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, child)

    if len(out) < len(by_id):
        # Cycle detected (or unreachable dep graph). Append the
        # remaining nodes in tie-broken order; the DB will reject
        # them at INSERT time, which the per-row SAVEPOINT and
        # all-or-nothing post-check both handle.
        leftover = sorted(
            [eid for eid in by_id if by_id[eid] not in out],
            key=_key,
        )
        out.extend(by_id[eid] for eid in leftover)

    return out + no_id


async def _import_memory_versions(
    conn,
    sidecar: List[Dict[str, Any]],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    stats: ImportStats,
    allowlist: Dict[str, tuple],
) -> tuple:
    """Upsert MPF memory_version_entry sidecar entries into
    memory_versions. Idempotent on `id`. Carries the full DAG
    triplet (commit_hash, parent_version_id, branch); merge_parents
    flows through verbatim as a UUID[].

    Returns ``(authorized_record_ids, failed_record_ids)``:
      - authorized_record_ids: record_ids whose version row was
        successfully imported OR was already present (skipped via
        ON CONFLICT). Used downstream for memory_branches HEAD
        restoration — only memory ids the caller was actually
        authorized to write get touched.
      - failed_record_ids: record_ids that had at least one entry
        rejected (allowlist failure, validation failure, DB error,
        missing required field). The caller uses this to enforce
        the all-or-nothing-history contract: if a record was
        INSERTed in this request AND any of its memory_versions
        entries failed, the import must roll back. Otherwise the
        record commits with partial / inconsistent history.
    """
    surface = "memory_versions"
    authorized_record_ids: set = set()
    failed_record_ids: set = set()
    # Real topological sort over parent_version_id + merge_parents
    # so parent rows are inserted before children regardless of
    # envelope order or branch name. The previous (record_id,
    # branch, version_num) sort failed for forked histories where
    # a feature branch's v1 parents to main's vN — alphabetic on
    # branch name put feature/v1 before main/vN, FK violation
    # (Codex round-9 finding).
    #
    # Algorithm: Kahn's. Build edges child→parent for both
    # parent_version_id and merge_parents (UUID[]). A node's
    # "in-degree" is the number of its parents that ALSO appear
    # in this envelope; parents already in DB count as zero
    # (treated as roots, their FK will resolve at INSERT time).
    # Pop zero-in-degree nodes in ties broken by version_num.
    sidecar = _topo_sort_versions(sidecar)
    # Index in-envelope entries by id for parent-validation
    # lookups. parent_version_id and merge_parents may point at
    # entries inside this envelope OR at rows already in DB; the
    # validator falls back to a SELECT for the latter.
    in_envelope_index = {
        str(e["id"]): e for e in sidecar if e.get("id")
    }
    for entry in sidecar:
        # Capture record_id even when other required fields are
        # missing — we need it for the failure-tracking set so the
        # caller can decide whether to roll back.
        record_id_for_tracking = entry.get("record_id")
        for required in ("id", "record_id", "version_num", "content"):
            if entry.get(required) in (None, ""):
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(
                    f"[{surface}] missing required field {required!r}; skipped"
                )
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
                failed_record_ids.add(entry["record_id"])
                continue

            # Tenant-scope every parent UUID. parent_version_id and
            # merge_parents both reference memory_versions(id) — the
            # DB FK only proves the UUID exists, NOT that it points
            # at a same-memory, same-tenant row. Validate explicitly
            # so an adversarial sidecar can't link its child commit
            # to another tenant's parent (Codex round-11 finding).
            parent_uuids: List[str] = []
            if entry.get("parent_version_id"):
                parent_uuids.append(str(entry["parent_version_id"]))
            for mp in entry.get("merge_parents") or []:
                if mp:
                    parent_uuids.append(str(mp))
            if parent_uuids:
                ok, bad = await _validate_version_parents(
                    conn, parent_uuids,
                    expected_record_id=entry["record_id"],
                    effective_owner=row_owner,
                    effective_ns=row_ns,
                    in_envelope_index=in_envelope_index,
                )
                if not ok:
                    _bump(stats.sidecars_failed, surface)
                    stats.errors.append(
                        f"[{surface}] {entry['id']}: parent_version_id/"
                        f"merge_parents reference foreign-tenant or "
                        f"foreign-record version(s) {bad}; rejected"
                    )
                    failed_record_ids.add(entry["record_id"])
                    continue

            metadata = entry.get("metadata") or {}
            # Per-row SAVEPOINT — same reasoning as kg_triples loop.
            try:
                async with conn.transaction():
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
                # Either way, the row IS in memory_versions for this
                # record_id (just inserted or already there). It's
                # safe and necessary to restore branches for it.
                authorized_record_ids.add(entry["record_id"])
            except Exception as exc:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry['id']}: {type(exc).__name__}: {exc}")
                logger.exception("MPF memory_versions import failed for entry %s", entry.get("id"))
                failed_record_ids.add(entry["record_id"])
    return authorized_record_ids, failed_record_ids


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
            # Tenant-scope check. Even though
            # memory_compressed_variants has no `namespace` column,
            # the referenced memories row does — and a same-owner
            # cross-namespace import (alice.ns_A claiming a variant
            # for alice.ns_B's memory) would silently poison the
            # compressed content read paths in ns_B. Validate
            # against the caller's effective namespace, NOT against
            # row_ns (which is None here because the table has no
            # namespace column).
            #
            # For preserve_owner=true, fall back to the entry's
            # stated owner_id matching the memory's actual one;
            # namespace check uses the memory's actual namespace
            # since the entry has no namespace field by schema.
            ref_namespace = (
                None if preserve_owner else caller_namespace
            )
            allowed, reason = _is_allowed_reference(
                entry.get("record_id"),
                effective_owner=row_owner,
                effective_namespace=ref_namespace,
                allowlist=allowlist,
                # Skip the namespace match only under preserve_owner,
                # where the caller is root and migrations span ns.
                require_namespace_match=not preserve_owner,
            )
            if not allowed:
                _bump(stats.sidecars_failed, surface)
                stats.errors.append(f"[{surface}] {entry.get('record_id')}: {reason}")
                continue
            # winner_candidate_id is FK→memory_compression_candidates(id)
            # ON DELETE SET NULL. The manifest sidecar carries the
            # winner ID for traceability but CHARON does not also
            # ship the candidate row, so the FK target may be absent
            # after import. Pre-validate format in Python (rejecting
            # malformed UUIDs without a SQL roundtrip), then check
            # existence INSIDE the per-row SAVEPOINT so a Postgres
            # error there can't abort the surrounding import
            # transaction (Codex round-6 finding).
            #
            # Lossless on the manifest's primary content (engine_id,
            # compressed_content, scores) — only the back-pointer
            # to which contest produced this winner gets nulled when
            # the candidate row isn't carried.
            winner_id_raw = entry.get("winner_contest_id")
            winner_id: Optional[str] = None
            if winner_id_raw:
                try:
                    # uuid.UUID() raises ValueError on malformed input —
                    # pure Python, no SQL hit.
                    winner_id = str(uuid.UUID(str(winner_id_raw)))
                except (ValueError, AttributeError):
                    winner_id = None
            # Per-row SAVEPOINT — same reasoning as the other sidecars.
            try:
                async with conn.transaction():
                    if winner_id is not None:
                        # Tenancy-scoped existence check (Codex round-7
                        # finding): the candidate row must belong to
                        # THIS memory_id AND THIS owner. Without that
                        # constraint, an envelope can point its
                        # winner_candidate_id at another tenant's
                        # candidate UUID (if known), creating cross-
                        # tenant linkage in the audit log. The check
                        # also runs inside the savepoint so a malformed
                        # input or DB error rolls back only this row.
                        exists = await conn.fetchval(
                            "SELECT 1 FROM memory_compression_candidates "
                            "WHERE id = $1::uuid AND memory_id = $2 "
                            "AND owner_id = $3",
                            winner_id, entry["record_id"], row_owner,
                        )
                        if not exists:
                            winner_id = None
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

    # Pre-validate memory_versions coverage BEFORE opening the
    # transaction. When an envelope carries memory_versions, the
    # records loop runs with the version-snapshot trigger
    # suppressed — so any record_id that *isn't* covered by the
    # sidecar would land in `memories` without a v1 history entry
    # at all. That's worse than the original collision bug:
    # silently-unversioned production data. Refuse the envelope
    # up front rather than partially-import broken history.
    if envelope.memory_versions:
        memory_record_ids = {
            r.id for r in envelope.records if r.kind == "memory"
        }
        sidecar_versioned_ids = {
            e.get("record_id") for e in envelope.memory_versions
            if e.get("record_id")
        }
        uncovered = memory_record_ids - sidecar_versioned_ids
        if uncovered:
            raise HTTPException(
                status_code=400,
                detail=(
                    "memory_versions sidecar must cover every "
                    f"kind: memory record being imported. {len(uncovered)} "
                    f"record(s) have no version entry: "
                    f"{sorted(uncovered)[:5]}{'...' if len(uncovered) > 5 else ''}. "
                    "Either ship a complete sidecar or omit the "
                    "memory_versions array entirely (the trigger will "
                    "synthesize default v1 history)."
                ),
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
            # the version log. Pre-validation above guarantees that
            # every record being inserted has authoritative history.
            #
            # Targeted suppression via the `mnemos.suppress_version_snapshot`
            # custom GUC. The trigger creation in
            # db/migrations_charon_trigger_guard.sql attaches a
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

            # Track which memory IDs this import actually INSERTed
            # (vs which hit ON CONFLICT DO NOTHING and were
            # pre-existing). The post-import v1 verification scopes
            # to THIS set, not envelope.records — otherwise a
            # pre-existing legacy memory with no v1 history would
            # roll back an unrelated import (Codex round-4 finding).
            inserted_record_ids: set = set()

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
                #
                # Wrap the per-row INSERT in a SAVEPOINT (asyncpg's
                # nested transaction context) so a Postgres-level
                # error on one row aborts ONLY that row, not the
                # whole import transaction. Without this, a single
                # constraint violation poisons the outer transaction
                # and every subsequent statement (allowlist SELECT,
                # sidecar INSERTs, post-verification SELECT) fails
                # with InFailedSQLTransaction. Codex round-5 finding.
                try:
                    async with conn.transaction():
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
                        inserted_record_ids.add(record.id)
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
            # Scope the allowlist SELECT structurally so a non-root
            # caller can't probe for foreign memory_ids via
            # rejection-message inference (Codex round-13 finding).
            # Non-root: SELECT is filtered to caller's owner+ns;
            # foreign ids simply don't appear, identical to
            # nonexistent ids. Root with preserve_owner=true: lookup
            # is unscoped — that's the migration/admin path where
            # cross-tenant resolution is the intended behavior.
            if _is_root(user) and preserve_owner:
                scope_owner: Optional[str] = None
                scope_namespace: Optional[str] = None
            else:
                scope_owner = user.user_id
                scope_namespace = user.namespace
            allowlist = await _build_referenced_memory_allowlist(
                conn, envelope,
                scope_owner=scope_owner,
                scope_namespace=scope_namespace,
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
                authorized_version_ids, failed_version_record_ids = (
                    await _import_memory_versions(
                        conn, envelope.memory_versions,
                        caller_user_id=user.user_id,
                        caller_namespace=user.namespace,
                        preserve_owner=preserve_owner,
                        stats=stats,
                        allowlist=allowlist,
                    )
                )
                # All-or-nothing per record (Codex round-7 finding):
                # if a record was INSERTed in this transaction AND
                # any of its memory_versions sidecar entries failed,
                # roll back the whole import. Otherwise the record
                # commits with partial / inconsistent history and
                # the API returns a 200 with sidecars_failed >0,
                # which is a silent integrity violation.
                fatal_record_ids = inserted_record_ids & failed_version_record_ids
                if fatal_record_ids:
                    sample = sorted(fatal_record_ids)[:5]
                    extra = "..." if len(fatal_record_ids) > 5 else ""
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "CHARON import: memory_versions sidecar had "
                            f"failed entries for {len(fatal_record_ids)} "
                            f"newly inserted record(s): {sample}{extra}. "
                            "Authoritative history is all-or-nothing per "
                            "record under trigger suppression — partial "
                            "history would be inconsistent. Transaction "
                            "rolled back; fix the sidecar and retry."
                        ),
                    )

                # Restore memory_branches HEAD pointers. The trigger
                # would normally do this on memory INSERT, but it's
                # suppressed during CHARON imports. Without this,
                # the imported version rows are orphans from the
                # branch-walk perspective (DAG endpoints return empty).
                #
                # Only restore branches for record_ids the import
                # was authorized to write — rejected cross-tenant
                # entries must not drive memory_branches mutations
                # for memories the caller can't touch.
                if authorized_version_ids:
                    await _restore_memory_branches(
                        conn, list(authorized_version_ids),
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

            # Post-import v1 verification: every memory THIS request
            # actually INSERTed (under trigger suppression) must
            # have at least one row in memory_versions. Pre-existing
            # rows that hit ON CONFLICT DO NOTHING are NOT verified
            # here — their v1 history is whatever it already was;
            # this transaction didn't touch them.
            if envelope.memory_versions and inserted_record_ids:
                covered = await conn.fetch(
                    "SELECT DISTINCT memory_id FROM memory_versions "
                    "WHERE memory_id = ANY($1::text[])",
                    list(inserted_record_ids),
                )
                covered_ids = {r["memory_id"] for r in covered}
                uncovered = inserted_record_ids - covered_ids
                if uncovered:
                    sample = sorted(uncovered)[:5]
                    extra = "..." if len(uncovered) > 5 else ""
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "CHARON import inserted "
                            f"{len(uncovered)} memory record(s) "
                            "without version history under "
                            f"trigger suppression: {sample}{extra}. "
                            "Sidecar likely contained malformed or "
                            "rejected entries that did not produce "
                            "rows. Transaction rolled back."
                        ),
                    )

    logger.info(
        "[MPF] import: user=%s imported=%d skipped=%d failed=%d unsupported=%s sidecars_imported=%s",
        user.user_id, stats.imported, stats.skipped, stats.failed,
        stats.unsupported_kinds, stats.sidecars_imported,
    )
    return stats

"""Federation endpoints — /v1/federation/*.

Two halves:
  * Admin side (root only): register peers, inspect sync status, trigger manual sync.
  * Protocol side (federation role): the `/feed` endpoint that remote peers pull from.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.core.ids import parse_uuid_or_404
from mnemos.domain import federation as _fed
from mnemos.domain.models import (
    FederationFeedResponse,
    FederationPeer,
    FederationPeerCreateRequest,
    FederationPeerListResponse,
    FederationPeerUpdateRequest,
    FederationStatusResponse,
    FederationSyncLogEntry,
    FederationSyncLogResponse,
    FederationSyncTriggerResponse,
    MemoryItem,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/federation", tags=["federation"])


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _require_federation_role(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Allow feed access for roles 'federation' or 'root'."""
    if user.role not in ("federation", "root"):
        raise HTTPException(status_code=403, detail="federation role required")
    return user


def _to_peer(row) -> FederationPeer:
    # compat_mode + peer_mnemos_version + last_schema_check_at landed in
    # migrations_v3_4_federation_compat.sql. Older rows / older test
    # mocks won't have them — fall back rather than KeyError.
    def _opt(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    last_schema = _opt("last_schema_check_at")
    return FederationPeer(
        id=str(row["id"]),
        name=row["name"],
        base_url=row["base_url"],
        namespace_filter=list(row["namespace_filter"]) if row["namespace_filter"] else None,
        category_filter=list(row["category_filter"]) if row["category_filter"] else None,
        enabled=row["enabled"],
        sync_interval_secs=row["sync_interval_secs"],
        last_sync_at=row["last_sync_at"].isoformat() if row["last_sync_at"] else None,
        last_sync_cursor=row["last_sync_cursor"].isoformat() if row["last_sync_cursor"] else None,
        last_error=row["last_error"],
        last_error_at=row["last_error_at"].isoformat() if row["last_error_at"] else None,
        total_pulled=row["total_pulled"],
        compat_mode=_opt("compat_mode", "strict"),
        peer_mnemos_version=_opt("peer_mnemos_version"),
        last_schema_check_at=last_schema.isoformat() if last_schema else None,
        created=row["created"].isoformat(),
        updated=row["updated"].isoformat(),
    )


def _memory_item_from_row(row, include_compressed: bool = False) -> MemoryItem:
    """Build a MemoryItem from a feed row.

    ``include_compressed`` populates ``compressed_content`` from the
    LEFT JOIN against memory_compressed_variants when the row has one
    (and the caller of the row-fetch actually selected the column).
    Receivers that don't recognize compressed_content fall through
    to the raw ``content`` with no behavior change.
    """
    compressed = None
    if include_compressed:
        try:
            compressed = row["compressed_content"]
        except (KeyError, IndexError):
            compressed = None
    try:
        archived_at = row["archived_at"]
    except (KeyError, IndexError):
        archived_at = None
    return MemoryItem(
        id=row["id"],
        content=row["content"],
        category=row["category"],
        subcategory=row["subcategory"],
        created=row["created"].isoformat(),
        updated=row["updated"].isoformat() if row["updated"] else None,
        metadata=(
            json.loads(row["metadata"])
            if isinstance(row["metadata"], str)
            else (dict(row["metadata"]) if row["metadata"] else None)
        ),
        quality_rating=row["quality_rating"],
        compressed_content=compressed,
        verbatim_content=row["verbatim_content"],
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        permission_mode=row["permission_mode"],
        source_model=row["source_model"],
        source_provider=row["source_provider"],
        source_session=row["source_session"],
        source_agent=row["source_agent"],
        archived_at=(
            archived_at.isoformat()
            if archived_at and hasattr(archived_at, "isoformat")
            else archived_at
        ),
        archived=archived_at is not None,
    )


def _federation_visibility_filters() -> list[str]:
    # Match `/feed`: only local, explicitly world-readable memories are visible
    # to federation peers.
    return [
        "m.federation_source IS NULL",
        "(m.permission_mode % 10) >= 4",
        "m.deleted_at IS NULL",
    ]


# ── Schema discovery (peers query this before deciding to sync) ──────────────


@router.get("/schema")
async def federation_schema(
    _: UserContext = Depends(_require_federation_role),
):
    """Return this node's mnemos_version + a coarse schema signature.

    Federation peers query this endpoint at the start of each sync to
    decide whether the local schema is compatible with theirs. The
    `signature` is the major.minor of mnemos_version — e.g. peers
    on different minor versions are flagged for the operator's
    `compat_mode` decision (strict vs permissive).

    A future v3.5/v4.0 evolution per docs/V3_5_CHARTER.md replaces
    this with a "core fields + extensions" contract; today's check
    is the minimum safe surface.

    `migrations_fingerprint` is a SHA256-prefix of the deployed
    migration filename list — peers compare this layered on top of
    `schema_signature` to catch branch-skew within a major.minor.
    """
    from mnemos._version import __version__ as _v
    from mnemos.domain.federation import _local_migrations_fingerprint
    parts = _v.split(".")
    major_minor = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else _v
    return {
        "mnemos_version": _v,
        "schema_signature": major_minor,
        "migrations_fingerprint": _local_migrations_fingerprint(),
        # Optional informational fields — peers may inspect to decide
        # whether their schema matches enough to pull. Not used as a
        # hard gate yet.
        "core_fields": [
            "id", "content", "category", "subcategory",
            "owner_id", "namespace", "permission_mode",
            "quality_rating", "created", "updated",
        ],
    }


# ── Admin: peer CRUD ─────────────────────────────────────────────────────────


async def _validate_peer_base_url(base_url: str) -> None:
    """Require https:// and reject SSRF targets for peer base URLs. Set FEDERATION_ALLOW_INSECURE=true
    to permit http:// (lab/local testing only — the peer auth token ships
    in clear over HTTP)."""
    from urllib.parse import urlparse
    from mnemos.core.config import get_settings
    from mnemos.webhooks.validation import validate_webhook_url

    settings = get_settings().federation
    await validate_webhook_url(base_url, allow_private=settings.allow_private)
    allow_insecure = get_settings().federation.allow_insecure
    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and allow_insecure:
        logger.warning(
            "federation: registering insecure peer base_url=%s — "
            "auth token will be sent in cleartext", base_url,
        )
        return
    raise HTTPException(
        status_code=422,
        detail="peer base_url must use https:// "
               "(set FEDERATION_ALLOW_INSECURE=true to permit http, not for prod)",
    )


@router.post("/peers", response_model=FederationPeer, status_code=201)
async def register_peer(
    request: FederationPeerCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Register a remote peer to pull from."""
    await _validate_peer_base_url(request.base_url)
    if request.compat_mode not in ("strict", "permissive"):
        raise HTTPException(
            status_code=422,
            detail="compat_mode must be 'strict' or 'permissive'",
        )
    require_postgres_pool_or_503(route_label="POST /v1/federation/peers")

    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO federation_peers
              (name, base_url, auth_token, namespace_filter, category_filter,
               enabled, sync_interval_secs, compat_mode)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            request.name, request.base_url, request.auth_token,
            request.namespace_filter, request.category_filter,
            request.enabled, request.sync_interval_secs,
            request.compat_mode,
        )
    logger.info(
        "federation: peer registered name=%s compat_mode=%s",
        request.name, request.compat_mode,
    )
    return _to_peer(row)


@router.get("/peers", response_model=FederationPeerListResponse)
async def list_peers(_: UserContext = Depends(require_root)):
    require_postgres_pool_or_503(route_label="GET /v1/federation/peers")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_peers ORDER BY name")
    peers = [_to_peer(r) for r in rows]
    return FederationPeerListResponse(count=len(peers), peers=peers)


@router.get("/peers/{peer_id}", response_model=FederationPeer)
async def get_peer(peer_id: str, _: UserContext = Depends(require_root)):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    require_postgres_pool_or_503(route_label="GET /v1/federation/peers/{peer_id}")
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM federation_peers WHERE id = $1::uuid", peer_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="peer not found")
    return _to_peer(row)


@router.patch("/peers/{peer_id}", response_model=FederationPeer)
async def update_peer(
    peer_id: str,
    request: FederationPeerUpdateRequest,
    _: UserContext = Depends(require_root),
):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    require_postgres_pool_or_503(route_label="PATCH /v1/federation/peers/{peer_id}")
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "base_url" in updates:
        await _validate_peer_base_url(updates["base_url"])
    if "compat_mode" in updates and updates["compat_mode"] not in ("strict", "permissive"):
        raise HTTPException(
            status_code=422,
            detail="compat_mode must be 'strict' or 'permissive'",
        )
    # Defense-in-depth: allow only whitelisted column names in the dynamic SET
    # clause. Keys come from a Pydantic model today but this prevents future
    # additions from accidentally enabling injection.
    _ALLOWED_PEER_COLS = {
        "name", "base_url", "auth_token", "namespace_filter", "category_filter",
        "enabled", "sync_interval_secs", "compat_mode",
    }
    bad = set(updates.keys()) - _ALLOWED_PEER_COLS
    if bad:
        raise HTTPException(status_code=422, detail=f"unknown fields: {sorted(bad)}")
    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE federation_peers SET {', '.join(set_clauses)} "
            f"WHERE id=$1::uuid RETURNING *",
            peer_id, *updates.values(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="peer not found")
    return _to_peer(row)


@router.delete("/peers/{peer_id}", status_code=204)
async def delete_peer(peer_id: str, _: UserContext = Depends(require_root)):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    require_postgres_pool_or_503(route_label="DELETE /v1/federation/peers/{peer_id}")
    async with _lc.get_pool_manager().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM federation_peers WHERE id = $1::uuid", peer_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="peer not found")


@router.post("/peers/{peer_id}/sync", response_model=FederationSyncTriggerResponse)
async def trigger_sync(
    peer_id: str,
    _: UserContext = Depends(require_root),
):
    """Run a sync against a peer right now (blocks on completion)."""
    peer_id = parse_uuid_or_404(peer_id, "peer")
    require_postgres_pool_or_503(route_label="POST /v1/federation/peers/{peer_id}/sync")
    try:
        pulled, new, updated = await _fed.sync_peer(_lc._pool, peer_id)
    except _fed.FederationSchemaIncompatible as e:
        # Confirmed mismatch (signature or fingerprint differs, or
        # peer durably 4xx-rejects /schema). Operator config issue —
        # 409 Conflict is the right shape for "I see the resource but
        # it conflicts with my expectations."
        raise HTTPException(status_code=409, detail=str(e))
    except _fed.FederationSchemaUnverifiable as e:
        # Peer responded with parseable error (e.g. /schema returns 200
        # but missing fields). 409 — the resource exists but the
        # contract is broken.
        raise HTTPException(status_code=409, detail=str(e))
    except _fed.FederationSchemaTransient as e:
        # Network/timeout/5xx — peer-side transient infra issue.
        # 503 is the right "try again later" shape.
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        # Genuinely missing peer (UUID not in federation_peers).
        raise HTTPException(status_code=404, detail=str(e))
    return FederationSyncTriggerResponse(
        pulled=pulled, new=new, updated=updated,
    )


@router.get("/peers/{peer_id}/log", response_model=FederationSyncLogResponse)
async def peer_sync_log(
    peer_id: str,
    _: UserContext = Depends(require_root),
    limit: int = 50,
):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    require_postgres_pool_or_503(route_label="GET /v1/federation/peers/{peer_id}/log")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = $1::uuid
            ORDER BY started_at DESC
            LIMIT $2
            """,
            peer_id, limit,
        )
    entries = [
        FederationSyncLogEntry(
            id=r["id"],
            started_at=r["started_at"].isoformat(),
            finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
            memories_pulled=r["memories_pulled"],
            memories_new=r["memories_new"],
            memories_updated=r["memories_updated"],
            error=r["error"],
            cursor_before=r["cursor_before"].isoformat() if r["cursor_before"] else None,
            cursor_after=r["cursor_after"].isoformat() if r["cursor_after"] else None,
        )
        for r in rows
    ]
    return FederationSyncLogResponse(count=len(entries), entries=entries)


@router.get("/status", response_model=FederationStatusResponse)
async def federation_status(_: UserContext = Depends(require_root)):
    require_postgres_pool_or_503(route_label="GET /v1/federation/status")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_peers ORDER BY name")
    peers = [_to_peer(r) for r in rows]
    return FederationStatusResponse(
        count=len(peers),
        enabled_count=sum(1 for p in peers if p.enabled),
        error_count=sum(1 for p in peers if p.last_error),
        peers=peers,
    )


# ── Protocol: serving peers pulling from us ──────────────────────────────────


@router.get("/feed", response_model=FederationFeedResponse)
async def federation_feed(
    request: Request,
    _: UserContext = Depends(_require_federation_role),
    since: Optional[str] = Query(None, description="Opaque federation cursor"),
    namespace: Optional[str] = Query(None, description="Comma-separated namespace filter"),
    category: Optional[str] = Query(None, description="Comma-separated category filter"),
    limit: int = Query(100, ge=1, le=500),
    prefer_compressed: bool = Query(
        False,
        description=(
            "When true, prefer the engine-compressed variant from "
            "memory_compressed_variants over the raw memory.content "
            "for memories that have a compressed variant. Reduces "
            "wire bytes on cross-cluster federation pulls. The peer "
            "knows compression was applied because compressed_content "
            "is populated on the MemoryItem; raw content stays "
            "fetchable via /v1/federation/memory/{id} without the "
            "flag."
        ),
    ),
):
    """Serve memories for a remote peer to pull. Requires role='federation' or 'root'."""
    require_postgres_pool_or_503(route_label="GET /v1/federation/feed")

    since_ts: Optional[datetime] = None
    since_id: Optional[str] = None
    if since:
        try:
            cursor = _fed._decode_feed_cursor(since)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid federation cursor")
        since_ts = _fed._cursor_timestamp_for_db(cursor.updated)
        since_id = cursor.memory_id

    namespaces = [s.strip() for s in namespace.split(",") if s.strip()] if namespace else []
    categories = [s.strip() for s in category.split(",") if s.strip()] if category else []

    # Only share memories explicitly marked for federation. `permission_mode`
    # is stored as a Unix-style mode integer (decimal 644 = "owner-rw,
    # group-r, others-r"; 600 = "owner-only"). We treat memories with the
    # others-read bit set (ones digit >= 4) as opt-in for federation; local
    # memories default to 600 and are invisible to peers unless the owner
    # explicitly sets something like 644. This replaces the previous
    # behaviour where any non-federation-sourced memory was served to any
    # peer with role='federation' — effectively a full read grant.
    #
    # `federation_source IS NULL` prevents loops (don't re-export memories
    # we ourselves pulled from another peer).
    query_parts = _federation_visibility_filters()
    args: list = []
    if since_ts is not None:
        args.append(since_ts)
        since_updated_arg = len(args)
        args.append(since_id)
        since_id_arg = len(args)
        query_parts.append(
            f"(m.updated > ${since_updated_arg} "
            f"OR (m.updated = ${since_updated_arg} AND m.id > ${since_id_arg}))"
        )
    if namespaces:
        args.append(namespaces)
        query_parts.append(f"m.namespace = ANY(${len(args)})")
    if categories:
        args.append(categories)
        query_parts.append(f"m.category = ANY(${len(args)})")

    args.append(limit + 1)   # request one extra to detect has_more
    where_clause = " AND ".join(query_parts)

    # prefer_compressed contract:
    #   * Per-row byte gate: the variant is used only when its
    #     octet_length is STRICTLY SMALLER than m.content. A
    #     pathological compression ratio > 1 (rare but possible
    #     for already-dense or short content) leaves the row on
    #     the raw payload, so the prefer_compressed path can
    #     never make a feed payload bigger than the legacy one.
    #     Codex round-11 audit (2026-05-01) — pre-fix the COALESCE
    #     used the variant unconditionally on presence, which was
    #     correct in the common case but broke the "wire bytes go
    #     down" guarantee on edge cases.
    #   * When the variant IS used: ``content`` carries the
    #     compressed payload, ``compressed_content`` field is
    #     populated to the same string (peer detector), and
    #     ``verbatim_content`` is NULLed (avoid double-shipping).
    #   * When the variant is NOT used (no row OR compressed
    #     bigger): identical to the default branch — raw content,
    #     compressed_content NULL, raw verbatim_content.
    # Default path (prefer_compressed=False) is unchanged — same
    # SQL shape, same MemoryItem fields, identical behavior peers
    # see today.
    if prefer_compressed:
        # Byte gate predicate (reused 3x below). Truthy iff
        # swapping to the variant produces a STRICTLY SMALLER
        # serialized JSON response than the legacy raw shape.
        #
        # We measure ACTUAL JSON-escaped bytes using Postgres
        # ``to_json(text)::text``, which produces the on-the-wire
        # form a JSON serializer would emit. Worst-case JSON
        # escape multipliers per input byte:
        #   * plain ASCII printable: 1.0×  (a → "a")
        #   * `\` and `"`           : 2.0× (\ → \\, " → \")
        #   * 0x00 control byte     : 6.0× (\u0000 / \u0001 etc.)
        # Pre-round-6 the gate used a heuristic 4× factor on the
        # raw octet count, which fell short for variants
        # containing control characters (codex round-14 audit:
        # `` escapes to 6 bytes and the 4× factor allowed
        # response growth on those rows). The to_json measurement
        # makes the predicate exact for any legal text input.
        #
        # Variant emitted twice (content + compressed_content);
        # the predicate is:
        #     2 * octet_length(to_json(variant)::text)
        #         < octet_length(to_json(m.content)::text)
        #           + COALESCE(octet_length(to_json(m.verbatim_content)::text), 0)
        use_variant = (
            "m.archived_at IS NULL "
            "AND v.compressed_content IS NOT NULL "
            "AND (2 * octet_length(to_json(v.compressed_content)::text)) "
            "  < (octet_length(to_json(m.content)::text) "
            "     + COALESCE(octet_length(to_json(m.verbatim_content)::text), 0))"
        )
        content_select = (
            f"CASE WHEN {use_variant} THEN v.compressed_content "
            "ELSE m.content END AS content,"
        )
        compressed_select = (
            f"CASE WHEN {use_variant} THEN v.compressed_content "
            "ELSE NULL::text END AS compressed_content,"
        )
        verbatim_select = (
            f"CASE WHEN {use_variant} THEN NULL "
            "ELSE m.verbatim_content END AS verbatim_content,"
        )
        join_compressed = "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
    else:
        content_select = "m.content,"
        compressed_select = "NULL::text AS compressed_content,"
        verbatim_select = "m.verbatim_content,"
        join_compressed = ""

    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT m.id, {content_select}
                   m.category, m.subcategory, m.metadata,
                   m.quality_rating, {verbatim_select}
                   m.owner_id, m.namespace,
                   m.permission_mode, m.source_model, m.source_provider,
                   m.source_session, m.source_agent, m.created, m.updated,
                   m.archived_at,
                   {compressed_select.rstrip(',')}
            FROM memories m
            {join_compressed}
            WHERE {where_clause}
            ORDER BY m.updated ASC, m.id ASC
            LIMIT ${len(args)}
            """,
            *args,
        )

    has_more = len(rows) > limit
    rows = rows[:limit]

    memories = [_memory_item_from_row(r, include_compressed=prefer_compressed) for r in rows]
    if rows and rows[-1]["updated"]:
        next_cursor = _fed._encode_feed_cursor(rows[-1]["updated"], rows[-1]["id"])
    else:
        next_cursor = since

    return FederationFeedResponse(
        memories=memories,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/memory/{memory_id}", response_model=MemoryItem)
async def federation_memory(
    memory_id: str,
    _: UserContext = Depends(_require_federation_role),
    namespace: Optional[str] = Query(None, description="Comma-separated namespace filter"),
    category: Optional[str] = Query(None, description="Comma-separated category filter"),
):
    """Serve one visible memory for a remote peer. Requires role='federation' or 'root'."""
    require_postgres_pool_or_503(route_label="GET /v1/federation/memory/{memory_id}")

    namespaces = [s.strip() for s in namespace.split(",") if s.strip()] if namespace else []
    categories = [s.strip() for s in category.split(",") if s.strip()] if category else []

    query_parts = _federation_visibility_filters()
    args: list = [memory_id]
    query_parts.append("m.id = $1")
    if namespaces:
        args.append(namespaces)
        query_parts.append(f"m.namespace = ANY(${len(args)})")
    if categories:
        args.append(categories)
        query_parts.append(f"m.category = ANY(${len(args)})")
    where_clause = " AND ".join(query_parts)

    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated, archived_at
            FROM memories m
            WHERE {where_clause}
            """,
            *args,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return _memory_item_from_row(row)

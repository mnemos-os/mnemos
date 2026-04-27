"""Federation endpoints — /v1/federation/*.

Two halves:
  * Admin side (root only): register peers, inspect sync status, trigger manual sync.
  * Protocol side (federation role): the `/feed` endpoint that remote peers pull from.
"""
from __future__ import annotations

import json
import logging
import uuid as _uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request


def _parse_uuid_or_404(value: str) -> str:
    try:
        _uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="peer not found")
    return value

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user, require_root
from api import federation as _fed
from api.models import (
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
    from _version import __version__ as _v
    from api.federation import _local_migrations_fingerprint
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


def _validate_peer_base_url(base_url: str) -> None:
    """Require https:// for peer base URLs. Set FEDERATION_ALLOW_INSECURE=true
    to permit http:// (lab/local testing only — the peer auth token ships
    in clear over HTTP)."""
    import os as _os
    from urllib.parse import urlparse
    allow_insecure = _os.getenv("FEDERATION_ALLOW_INSECURE", "false").lower() == "true"
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
    _validate_peer_base_url(request.base_url)
    if request.compat_mode not in ("strict", "permissive"):
        raise HTTPException(
            status_code=422,
            detail="compat_mode must be 'strict' or 'permissive'",
        )
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
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
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_peers ORDER BY name")
    peers = [_to_peer(r) for r in rows]
    return FederationPeerListResponse(count=len(peers), peers=peers)


@router.get("/peers/{peer_id}", response_model=FederationPeer)
async def get_peer(peer_id: str, _: UserContext = Depends(require_root)):
    _parse_uuid_or_404(peer_id)
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
    _parse_uuid_or_404(peer_id)
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "base_url" in updates:
        _validate_peer_base_url(updates["base_url"])
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
    async with _lc._pool.acquire() as conn:
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
    _parse_uuid_or_404(peer_id)
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
    _parse_uuid_or_404(peer_id)
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
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
    _parse_uuid_or_404(peer_id)
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
):
    """Serve memories for a remote peer to pull. Requires role='federation' or 'root'."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    since_ts: Optional[datetime] = None
    since_id = _fed.FEDERATION_CURSOR_LOWER_ID
    if since:
        try:
            cursor = _fed._decode_feed_cursor(since)
        except ValueError:
            raise HTTPException(status_code=422, detail="since must be a federation cursor")
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
    query_parts = [
        "m.federation_source IS NULL",
        "(m.permission_mode % 10) >= 4",
    ]
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

    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated
            FROM memories m
            WHERE {where_clause}
            ORDER BY m.updated ASC, m.id ASC
            LIMIT ${len(args)}
            """,
            *args,
        )

    has_more = len(rows) > limit
    rows = rows[:limit]

    memories = [
        MemoryItem(
            id=r["id"],
            content=r["content"],
            category=r["category"],
            subcategory=r["subcategory"],
            created=r["created"].isoformat(),
            updated=r["updated"].isoformat() if r["updated"] else None,
            metadata=(json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (dict(r["metadata"]) if r["metadata"] else None)),
            quality_rating=r["quality_rating"],
            verbatim_content=r["verbatim_content"],
            owner_id=r["owner_id"],
            namespace=r["namespace"],
            permission_mode=r["permission_mode"],
            source_model=r["source_model"],
            source_provider=r["source_provider"],
            source_session=r["source_session"],
            source_agent=r["source_agent"],
        )
        for r in rows
    ]
    if rows and rows[-1]["updated"]:
        next_cursor = _fed._encode_feed_cursor(rows[-1]["updated"], rows[-1]["id"])
    else:
        next_cursor = since

    return FederationFeedResponse(
        memories=memories,
        next_cursor=next_cursor,
        has_more=has_more,
    )

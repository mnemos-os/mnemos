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

from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.core.ids import parse_uuid_or_404
from mnemos.domain import federation as _fed
from mnemos.domain.models import (
    FederationConsolidationEvent,
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

    def _as_list(value):
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            return list(parsed) if isinstance(parsed, list) else None
        return list(value)

    def _iso(value):
        if value is None:
            return None
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    last_schema = _opt("last_schema_check_at")
    return FederationPeer(
        id=str(row["id"]),
        name=row["name"],
        base_url=row["base_url"],
        namespace_filter=_as_list(row["namespace_filter"]),
        category_filter=_as_list(row["category_filter"]),
        enabled=bool(row["enabled"]),
        sync_interval_secs=row["sync_interval_secs"],
        last_sync_at=_iso(row["last_sync_at"]),
        last_sync_cursor=_iso(row["last_sync_cursor"]),
        last_error=row["last_error"],
        last_error_at=_iso(row["last_error_at"]),
        total_pulled=row["total_pulled"],
        compat_mode=_opt("compat_mode", "strict"),
        peer_mnemos_version=_opt("peer_mnemos_version"),
        last_schema_check_at=_iso(last_schema),
        created=_iso(row["created"]) or "",
        updated=_iso(row["updated"]) or "",
    )


def _iso_value(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _cursor_datetime(value):
    if value is None or hasattr(value, "tzinfo"):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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
        created=_iso_value(row["created"]) or "",
        updated=_iso_value(row["updated"]),
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
            _iso_value(archived_at)
        ),
        archived=archived_at is not None,
    )


def _feed_item_from_row(
    row,
    *,
    include_compressed: bool = False,
) -> MemoryItem | FederationConsolidationEvent:
    try:
        item_type = row["type"]
    except (KeyError, IndexError):
        item_type = None
    if item_type == "consolidation":
        consolidated_at = row["consolidated_at"]
        return FederationConsolidationEvent(
            id=row["id"],
            consolidated_into=row["consolidated_into"],
            consolidated_at=_iso_value(consolidated_at) or "",
        )
    return _memory_item_from_row(row, include_compressed=include_compressed)


def _federation_visibility_filters() -> list[str]:
    # Match `/feed`: only local, explicitly world-readable memories are visible
    # to federation peers.
    return [_fed.eligible_for_federation("m")]


# #183: removed `_federation_tombstone_filters` — defined but
# never called. The actual federation filter list is built inline
# at the call sites that need it.


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
    # #168: compat_mode is now Literal["strict", "permissive"] in
    # the request model — Pydantic auto-422s before we get here.
    backend = backend_or_503()
    try:
        async with backend.transactional() as tx:
            row = await backend.federation.create_peer(
                tx,
                name=request.name,
                base_url=request.base_url,
                auth_token=request.auth_token,
                namespace_filter=request.namespace_filter,
                category_filter=request.category_filter,
                enabled=request.enabled,
                sync_interval_secs=request.sync_interval_secs,
                compat_mode=request.compat_mode,
            )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    logger.info(
        "federation: peer registered name=%s compat_mode=%s",
        request.name, request.compat_mode,
    )
    return _to_peer(row)


@router.get("/peers", response_model=FederationPeerListResponse)
async def list_peers(_: UserContext = Depends(require_root)):
    backend = backend_or_503()
    async with backend.transactional() as tx:
        rows = await backend.federation.list_peers(tx)
    peers = [_to_peer(r) for r in rows]
    return FederationPeerListResponse(count=len(peers), peers=peers)


@router.get("/peers/{peer_id}", response_model=FederationPeer)
async def get_peer(peer_id: str, _: UserContext = Depends(require_root)):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    backend = backend_or_503()
    async with backend.transactional() as tx:
        row = await backend.federation.get_peer(tx, peer_id)
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
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "base_url" in updates:
        await _validate_peer_base_url(updates["base_url"])
    # #168: compat_mode is now Optional[Literal["strict",
    # "permissive"]] in the update model — Pydantic auto-422s on
    # invalid values before we get here.
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
    backend = backend_or_503()
    async with backend.transactional() as tx:
        row = await backend.federation.update_peer(tx, peer_id, updates)
    if not row:
        raise HTTPException(status_code=404, detail="peer not found")
    return _to_peer(row)


@router.delete("/peers/{peer_id}", status_code=204)
async def delete_peer(peer_id: str, _: UserContext = Depends(require_root)):
    peer_id = parse_uuid_or_404(peer_id, "peer")
    backend = backend_or_503()
    async with backend.transactional() as tx:
        deleted = await backend.federation.delete_peer(tx, peer_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="peer not found")


@router.post("/peers/{peer_id}/sync", response_model=FederationSyncTriggerResponse)
async def trigger_sync(
    peer_id: str,
    _: UserContext = Depends(require_root),
):
    """Run a sync against a peer right now (blocks on completion)."""
    peer_id = parse_uuid_or_404(peer_id, "peer")
    backend = backend_or_503()
    try:
        pulled, new, updated = await _fed.sync_peer(backend, peer_id)
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
    backend = backend_or_503()
    async with backend.transactional() as tx:
        rows = await backend.federation.fetch_sync_log(tx, peer_id, limit)
    entries = [
        FederationSyncLogEntry(
            id=r["id"],
            started_at=_iso_value(r["started_at"]) or "",
            finished_at=_iso_value(r["finished_at"]),
            memories_pulled=r["memories_pulled"],
            memories_new=r["memories_new"],
            memories_updated=r["memories_updated"],
            error=r["error"],
            cursor_before=_iso_value(r["cursor_before"]),
            cursor_after=_iso_value(r["cursor_after"]),
        )
        for r in rows
    ]
    return FederationSyncLogResponse(count=len(entries), entries=entries)


@router.get("/status", response_model=FederationStatusResponse)
async def federation_status(_: UserContext = Depends(require_root)):
    backend = backend_or_503()
    async with backend.transactional() as tx:
        rows = await backend.federation.list_peers(tx)
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
    backend = backend_or_503()

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

    try:
        async with backend.transactional() as tx:
            rows = await backend.federation.feed_query(
                tx,
                since_updated=since_ts,
                since_id=since_id,
                namespaces=namespaces,
                categories=categories,
                limit=limit + 1,
                prefer_compressed=prefer_compressed,
            )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    has_more = len(rows) > limit
    rows = rows[:limit]

    memories = [_feed_item_from_row(r, include_compressed=prefer_compressed) for r in rows]
    if rows and rows[-1]["updated"]:
        next_cursor = _fed._encode_feed_cursor(_cursor_datetime(rows[-1]["updated"]), rows[-1]["id"])
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
    backend = backend_or_503()

    namespaces = [s.strip() for s in namespace.split(",") if s.strip()] if namespace else []
    categories = [s.strip() for s in category.split(",") if s.strip()] if category else []

    try:
        async with backend.transactional() as tx:
            row = await backend.federation.get_feed_memory(
                tx,
                memory_id,
                namespaces=namespaces,
                categories=categories,
            )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return _memory_item_from_row(row)

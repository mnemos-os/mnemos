"""MNEMOS v1 admin endpoints — user and API key management (root only)."""
import hashlib
import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.core.config import get_settings
from mnemos.core.security import is_root
from mnemos.domain.persephone.runner import (
    archive_memory as _archive_memory,
    restore_memory as _restore_memory,
    sweep_for_archival as _sweep_for_archival,
)
from mnemos.domain.models import (
    ApiKeyCreateRequest,
    ApiKeyResponse,
    DeletionRequestCreate,
    DeletionRequestItem,
    DeletionRequestListResponse,
    OAuthIdentity,
    OAuthIdentityListResponse,
    OAuthProviderAdmin,
    OAuthProviderAdminListResponse,
    OAuthProviderCreateRequest,
    OAuthProviderUpdateRequest,
    UserCreateRequest,
    UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Users ─────────────────────────────────────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    request: UserCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Create a new user. id must be unique."""
    require_postgres_pool_or_503(route_label="POST /admin/users")
    if request.role not in ("user", "root", "federation"):
        raise HTTPException(
            status_code=422,
            detail="role must be 'user', 'root', or 'federation'",
        )
    async with _lc.get_pool_manager().acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE id=$1", request.id)
        if existing:
            raise HTTPException(status_code=409, detail=f"User '{request.id}' already exists")
        row = await conn.fetchrow(
            "INSERT INTO users (id, display_name, email, role, namespace) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, display_name, email, role, namespace, created_at",
            request.id, request.display_name, request.email, request.role,
            request.namespace,
        )
    return UserResponse(
        id=row["id"],
        display_name=row["display_name"],
        email=row["email"],
        role=row["role"],
        namespace=row["namespace"],
        created_at=row["created_at"].isoformat(),
    )


@router.get("/users", response_model=List[UserResponse])
async def list_users(_: UserContext = Depends(require_root)):
    """List all users."""
    require_postgres_pool_or_503(route_label="GET /admin/users")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, display_name, email, role, namespace, created_at "
            "FROM users ORDER BY created_at"
        )
    return [
        UserResponse(
            id=r["id"],
            display_name=r["display_name"],
            email=r["email"],
            role=r["role"],
            namespace=r["namespace"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


# ── API Keys ──────────────────────────────────────────────────────────────────

@router.post("/users/{user_id}/apikeys", response_model=ApiKeyResponse, status_code=201)
async def create_api_key(
    user_id: str,
    request: ApiKeyCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Generate a new API key for user_id. Raw key is returned once and never stored."""
    require_postgres_pool_or_503(route_label="POST /admin/users/{user_id}/apikeys")

    async with _lc.get_pool_manager().acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        key_count = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=$1 AND NOT revoked", user_id
        )
        if key_count >= 10:
            raise HTTPException(
                status_code=422,
                detail="Maximum of 10 active API keys per user",
            )

        raw_key = secrets.token_hex(32)       # 64 hex chars = 256 bits
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]              # shown in listings for identification

        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, label) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, user_id, key_prefix, label, created_at, last_used, revoked",
            user_id, key_hash, key_prefix, request.label,
        )

    logger.info(f"[ADMIN] Created API key prefix={key_prefix} for user={user_id}")
    return ApiKeyResponse(
        id=str(row["id"]),
        user_id=row["user_id"],
        key_prefix=row["key_prefix"],
        label=row["label"],
        created_at=row["created_at"].isoformat(),
        last_used=row["last_used"].isoformat() if row["last_used"] else None,
        revoked=row["revoked"],
        raw_key=raw_key,  # only returned here; never stored, never returned again
    )


@router.get("/users/{user_id}/apikeys", response_model=List[ApiKeyResponse])
async def list_api_keys(
    user_id: str,
    _: UserContext = Depends(require_root),
):
    """List API keys for user_id (no raw key in response)."""
    require_postgres_pool_or_503(route_label="GET /admin/users/{user_id}/apikeys")
    async with _lc.get_pool_manager().acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        rows = await conn.fetch(
            "SELECT id, user_id, key_prefix, label, created_at, last_used, revoked "
            "FROM api_keys WHERE user_id=$1 ORDER BY created_at",
            user_id,
        )
    return [
        ApiKeyResponse(
            id=str(r["id"]),
            user_id=r["user_id"],
            key_prefix=r["key_prefix"],
            label=r["label"],
            created_at=r["created_at"].isoformat(),
            last_used=r["last_used"].isoformat() if r["last_used"] else None,
            revoked=r["revoked"],
        )
        for r in rows
    ]


@router.delete("/apikeys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    _: UserContext = Depends(require_root),
):
    """Revoke an API key by ID (soft-delete: sets revoked=true)."""
    require_postgres_pool_or_503(route_label="DELETE /admin/apikeys/{key_id}")
    async with _lc.get_pool_manager().acquire() as conn:
        result = await conn.execute(
            "UPDATE api_keys SET revoked=true WHERE id=$1::uuid AND NOT revoked",
            key_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    logger.info(f"[ADMIN] Revoked API key id={key_id}")


# ── OAuth provider management (root only) ────────────────────────────────────


def _to_provider_admin(row) -> OAuthProviderAdmin:
    return OAuthProviderAdmin(
        name=row["name"],
        display_name=row["display_name"],
        kind=row["kind"],
        issuer_url=row["issuer_url"],
        client_id=row["client_id"],
        client_secret_set=bool(row["client_secret"]),
        scope=row["scope"],
        authorize_url=row["authorize_url"],
        token_url=row["token_url"],
        userinfo_url=row["userinfo_url"],
        enabled=row["enabled"],
        created=row["created"].isoformat(),
        updated=row["updated"].isoformat(),
    )


@router.post("/oauth/providers", response_model=OAuthProviderAdmin, status_code=201)
async def create_oauth_provider(
    request: OAuthProviderCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Register a new OAuth provider (root only)."""
    require_postgres_pool_or_503(route_label="POST /admin/oauth/providers")
    if request.kind not in ("oidc", "oauth2"):
        raise HTTPException(status_code=422, detail="kind must be 'oidc' or 'oauth2'")
    if request.kind == "oidc" and not request.issuer_url:
        raise HTTPException(status_code=422, detail="issuer_url required when kind='oidc'")
    if request.kind == "oauth2" and not (request.authorize_url and request.token_url):
        raise HTTPException(
            status_code=422,
            detail="authorize_url and token_url required when kind='oauth2'",
        )
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO oauth_providers
              (name, display_name, kind, issuer_url, client_id, client_secret,
               scope, authorize_url, token_url, userinfo_url, enabled)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            request.name, request.display_name, request.kind, request.issuer_url,
            request.client_id, request.client_secret, request.scope,
            request.authorize_url, request.token_url, request.userinfo_url,
            request.enabled,
        )
    return _to_provider_admin(row)


@router.get("/oauth/providers", response_model=OAuthProviderAdminListResponse)
async def list_oauth_providers(_: UserContext = Depends(require_root)):
    require_postgres_pool_or_503(route_label="GET /admin/oauth/providers")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM oauth_providers ORDER BY name")
    items = [_to_provider_admin(r) for r in rows]
    return OAuthProviderAdminListResponse(count=len(items), providers=items)


@router.patch("/oauth/providers/{name}", response_model=OAuthProviderAdmin)
async def update_oauth_provider(
    name: str,
    request: OAuthProviderUpdateRequest,
    _: UserContext = Depends(require_root),
):
    require_postgres_pool_or_503(route_label="PATCH /admin/oauth/providers/{name}")
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE oauth_providers SET {', '.join(set_clauses)} "
            f"WHERE name=$1 RETURNING *",
            name, *updates.values(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Provider not found")
    return _to_provider_admin(row)


@router.delete("/oauth/providers/{name}", status_code=204)
async def delete_oauth_provider(
    name: str,
    _: UserContext = Depends(require_root),
):
    require_postgres_pool_or_503(route_label="DELETE /admin/oauth/providers/{name}")
    async with _lc.get_pool_manager().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM oauth_providers WHERE name=$1", name,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Provider not found")


@router.get("/oauth/identities", response_model=OAuthIdentityListResponse)
async def list_oauth_identities(
    _: UserContext = Depends(require_root),
    user_id: str = None,
):
    """List OAuth identities. Filter by user_id optional."""
    require_postgres_pool_or_503(route_label="GET /admin/oauth/identities")
    async with _lc.get_pool_manager().acquire() as conn:
        if user_id:
            rows = await conn.fetch(
                "SELECT id::text, user_id, provider, external_id, email, "
                "       display_name, last_login_at, created "
                "FROM oauth_identities WHERE user_id=$1 ORDER BY created DESC",
                user_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT id::text, user_id, provider, external_id, email, "
                "       display_name, last_login_at, created "
                "FROM oauth_identities ORDER BY created DESC LIMIT 100",
            )
    items = [
        OAuthIdentity(
            id=r["id"],
            user_id=r["user_id"],
            provider=r["provider"],
            external_id=r["external_id"],
            email=r["email"],
            display_name=r["display_name"],
            last_login_at=r["last_login_at"].isoformat() if r["last_login_at"] else None,
            created=r["created"].isoformat(),
        )
        for r in rows
    ]
    return OAuthIdentityListResponse(count=len(items), identities=items)


# ── v3.1 compression queue admin ─────────────────────────────────────────────
#
# The v3.1 compression contest reads from memory_compression_queue. Without a
# way to put rows into that queue, the whole pipeline is disconnected from
# the application layer — operators would need manual SQL. These endpoints
# give root users the minimum surface to drive the contest: enqueue
# specific memories, or enqueue every memory that doesn't yet have a
# compressed variant. Per-memory enqueue on write is v3.2 hot-path work.


_VALID_REASONS = {"on_write", "manual", "scheduled", "reprocess"}
_VALID_PROFILES = {"balanced", "quality_first", "speed_first", "custom"}


async def _invalidate_memory_read_caches() -> None:
    if not _lc._cache:
        return
    try:
        await _lc._cache.delete("stats:global:v2")
        try:
            async for key in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                await _lc._cache.delete(key)
        except Exception:
            pass
    except Exception:
        pass


class CompressionEnqueueRequest(BaseModel):
    memory_ids: List[str] = Field(
        ...,
        description="Memory IDs to enqueue. Each row becomes a pending task "
                    "in memory_compression_queue; the distillation worker drains "
                    "them on its next tick.",
        min_length=1,
        max_length=1000,
    )
    reason: str = Field(
        default="manual",
        description="Queue row reason. One of: on_write | manual | scheduled | reprocess",
    )
    scoring_profile: str = Field(
        default="balanced",
        description="Scoring profile for this batch. One of: "
                    "balanced | quality_first | speed_first | custom",
    )
    priority: int = Field(default=0, description="Higher = drained sooner")


class CompressionEnqueueResponse(BaseModel):
    enqueued: int
    skipped_unknown: int
    memory_ids: List[str]


class PersephoneSweepRequest(BaseModel):
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace to sweep. Defaults to MNEMOS_PERSEPHONE_NAMESPACE.",
    )
    archive_after_days: Optional[int] = Field(
        default=None,
        ge=1,
        description="Cold threshold in days. Defaults to MNEMOS_PERSEPHONE_ARCHIVE_AFTER_DAYS.",
    )
    batch_size: Optional[int] = Field(
        default=None,
        ge=1,
        le=10000,
        description="Maximum memories to archive in this sweep.",
    )


class PersephoneSweepResponse(BaseModel):
    archived: int
    namespace: str
    archive_after_days: int
    batch_size: int


class PersephoneMemoryResponse(BaseModel):
    memory_id: str
    archived: bool
    restored: bool = False


class PersephoneStatusResponse(BaseModel):
    enabled: bool
    archived_count: int
    last_run_at: Optional[str] = None
    oldest_unrecalled: Optional[str] = None
    namespace: Optional[str] = None


def _require_persephone_enabled() -> None:
    if not get_settings().persephone.enabled:
        raise HTTPException(
            status_code=409,
            detail="PERSEPHONE archival is disabled; set MNEMOS_PERSEPHONE_ENABLED=true",
        )


@router.post("/compression/enqueue", response_model=CompressionEnqueueResponse, status_code=201)
async def compression_enqueue(
    request: CompressionEnqueueRequest,
    _: UserContext = Depends(require_root),
):
    """Enqueue specific memories into memory_compression_queue.

    Memories that don't exist in `memories` are silently skipped
    (counted in `skipped_unknown`); this lets operators feed a mixed
    batch without pre-validating every ID. Enqueuing the same memory
    twice creates two pending rows — both run, the last-written winner
    supersedes on the variant. Operators who want dedupe should check
    for existing pending rows first.
    """
    require_postgres_pool_or_503(route_label="POST /admin/compression/enqueue")
    if request.reason not in _VALID_REASONS:
        raise HTTPException(
            status_code=422,
            detail=f"reason must be one of {sorted(_VALID_REASONS)}",
        )
    if request.scoring_profile not in _VALID_PROFILES:
        raise HTTPException(
            status_code=422,
            detail=f"scoring_profile must be one of {sorted(_VALID_PROFILES)}",
        )

    async with _lc.get_pool_manager().acquire() as conn:
        async with conn.transaction():
            # Pull (id, owner_id) so the queue row carries the memory's
            # REAL owner instead of a blanket 'default'. On multi-user
            # installs this stamped ownership flows into
            # memory_compression_candidates and memory_compressed_variants
            # and must reflect the underlying memory. Single-user installs
            # (memories.owner_id DEFAULT 'default') keep working unchanged.
            known = await conn.fetch(
                "SELECT id, owner_id FROM memories "
                "WHERE id = ANY($1::text[]) AND deleted_at IS NULL",
                request.memory_ids,
            )
            owner_by_id = {r["id"]: r["owner_id"] for r in known}
            enqueued_ids: list[str] = []
            for mid in request.memory_ids:
                if mid not in owner_by_id:
                    continue
                await conn.execute(
                    "INSERT INTO memory_compression_queue "
                    "(memory_id, owner_id, reason, priority, scoring_profile) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    mid, owner_by_id[mid], request.reason, request.priority,
                    request.scoring_profile,
                )
                enqueued_ids.append(mid)

    return CompressionEnqueueResponse(
        enqueued=len(enqueued_ids),
        skipped_unknown=len(request.memory_ids) - len(enqueued_ids),
        memory_ids=enqueued_ids,
    )


class CompressionEnqueueAllRequest(BaseModel):
    reason: str = Field(
        default="manual",
        description="Reason stamped on every queued row.",
    )
    scoring_profile: str = Field(default="balanced")
    priority: int = Field(default=0)
    category: Optional[str] = Field(
        default=None,
        description="Optional: only enqueue memories in this category.",
    )
    only_uncompressed: bool = Field(
        default=True,
        description="When True (default), skip memories that already have a "
                    "row in memory_compressed_variants. Flip to False to "
                    "force re-running the contest on every matching memory.",
    )
    limit: int = Field(
        default=500,
        ge=1,
        le=10000,
        description="Cap on how many memories this call enqueues. Default 500; "
                    "max 10,000. Run the endpoint repeatedly to drain a larger "
                    "corpus.",
    )


class CompressionEnqueueAllResponse(BaseModel):
    enqueued: int
    reason: str


@router.post("/compression/enqueue-all", response_model=CompressionEnqueueAllResponse, status_code=201)
async def compression_enqueue_all(
    request: CompressionEnqueueAllRequest,
    _: UserContext = Depends(require_root),
):
    """Bulk-enqueue matching memories.

    Default behavior: enqueue up to 500 memories that don't yet have a
    compressed variant. Operators who want to re-run the contest over
    every memory (e.g., after flipping scoring_profile defaults, or
    after updating an engine's prompt) set only_uncompressed=False and
    raise limit — but run the endpoint repeatedly rather than trying to
    enqueue the full corpus in one call.
    """
    require_postgres_pool_or_503(route_label="POST /admin/compression/enqueue-all")
    if request.reason not in _VALID_REASONS:
        raise HTTPException(
            status_code=422,
            detail=f"reason must be one of {sorted(_VALID_REASONS)}",
        )
    if request.scoring_profile not in _VALID_PROFILES:
        raise HTTPException(
            status_code=422,
            detail=f"scoring_profile must be one of {sorted(_VALID_PROFILES)}",
        )

    # Build WHERE clause incrementally. Avoid f-string injection by binding
    # every user-controlled value via asyncpg parameters.
    where_parts: list[str] = ["m.deleted_at IS NULL"]
    params: list = []
    if request.only_uncompressed:
        where_parts.append(
            "NOT EXISTS (SELECT 1 FROM memory_compressed_variants v WHERE v.memory_id = m.id)"
        )
    if request.category is not None:
        params.append(request.category)
        where_parts.append(f"m.category = ${len(params)}")
    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Priority, reason, scoring_profile, limit — bind next.
    params.extend([request.reason, request.priority, request.scoring_profile, request.limit])
    reason_idx = len(params) - 3
    priority_idx = len(params) - 2
    profile_idx = len(params) - 1
    limit_idx = len(params)

    # owner_id flows from memories.owner_id (not a blanket 'default') so
    # multi-user installs get truthful ownership metadata on every queue
    # row + downstream contest candidate + variant.
    sql = (
        "INSERT INTO memory_compression_queue "
        "(memory_id, owner_id, reason, priority, scoring_profile) "
        "SELECT m.id, m.owner_id, "
        f"${reason_idx}, ${priority_idx}, ${profile_idx} "
        f"FROM memories m{where_sql} "
        "ORDER BY LENGTH(m.content) DESC "
        f"LIMIT ${limit_idx}"
    )

    async with _lc.get_pool_manager().acquire() as conn:
        result = await conn.execute(sql, *params)
        # asyncpg returns "INSERT 0 <n>" — parse the row count
        try:
            n = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            n = 0

    return CompressionEnqueueAllResponse(enqueued=n, reason=request.reason)


# ── PERSEPHONE archival admin ────────────────────────────────────────────────

@router.post("/persephone/sweep", response_model=PersephoneSweepResponse)
async def persephone_sweep(
    request: PersephoneSweepRequest,
    _: UserContext = Depends(require_root),
):
    """Run one namespace-scoped PERSEPHONE archival sweep."""
    require_postgres_pool_or_503(route_label="POST /admin/persephone/sweep")
    _require_persephone_enabled()
    settings = get_settings().persephone
    namespace = request.namespace or settings.namespace
    archive_after_days = request.archive_after_days or settings.archive_after_days
    batch_size = request.batch_size or settings.batch_size

    archived = await _sweep_for_archival(
        _lc._pool,
        namespace=namespace,
        archive_after_days=archive_after_days,
        batch_size=batch_size,
    )
    if archived:
        await _invalidate_memory_read_caches()
    return PersephoneSweepResponse(
        archived=archived,
        namespace=namespace,
        archive_after_days=archive_after_days,
        batch_size=batch_size,
    )


@router.post("/persephone/archive/{memory_id}", response_model=PersephoneMemoryResponse)
async def persephone_archive_memory(
    memory_id: str,
    user: UserContext = Depends(require_root),
):
    """Archive a specific memory. Root-only operator override."""
    require_postgres_pool_or_503(route_label="POST /admin/persephone/archive/{memory_id}")
    _require_persephone_enabled()
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            await _archive_memory(conn, memory_id, user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _invalidate_memory_read_caches()
    return PersephoneMemoryResponse(memory_id=memory_id, archived=True)


@router.post("/persephone/restore/{memory_id}", response_model=PersephoneMemoryResponse)
async def persephone_restore_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Restore an archived memory. Allowed for root or the memory owner."""
    require_postgres_pool_or_503(route_label="POST /admin/persephone/restore/{memory_id}")
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, owner_id, namespace, archived_at
              FROM memories
             WHERE id = $1
               AND deleted_at IS NULL
            """,
            memory_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        if row["archived_at"] is None:
            raise HTTPException(status_code=409, detail="Memory is not archived")
        if not (is_root(user) or row["owner_id"] == user.user_id):
            raise HTTPException(
                status_code=403,
                detail="restore requires root or memory owner",
            )
        try:
            await _restore_memory(conn, memory_id, user.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _invalidate_memory_read_caches()
    return PersephoneMemoryResponse(
        memory_id=memory_id,
        archived=False,
        restored=True,
    )


@router.get("/persephone/status", response_model=PersephoneStatusResponse)
async def persephone_status(
    _: UserContext = Depends(require_root),
    namespace: Optional[str] = None,
):
    """Return PERSEPHONE archive totals and cold-set age signal."""
    require_postgres_pool_or_503(route_label="GET /admin/persephone/status")
    clauses: list[str] = []
    args: list = []
    if namespace is not None:
        args.append(namespace)
        clauses.append(f"m.namespace = ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    oldest_clauses = [
        "deleted_at IS NULL",
        "archived_at IS NULL",
        "consolidated_into IS NULL",
    ]
    oldest_args: list = []
    if namespace is not None:
        oldest_args.append(namespace)
        oldest_clauses.append(f"namespace = ${len(oldest_args)}")

    async with _lc.get_pool_manager().acquire() as conn:
        archive_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS archived_count,
                   MAX(a.archived_at) AS last_run_at
              FROM memory_archive a
              JOIN memories m ON m.id = a.id
              {where}
            """,
            *args,
        )
        oldest_unrecalled = await conn.fetchval(
            f"""
            SELECT MIN(COALESCE(last_recalled_at, created))
              FROM memories
             WHERE {' AND '.join(oldest_clauses)}
            """,
            *oldest_args,
        )

    last_run_at = archive_row["last_run_at"] if archive_row else None
    return PersephoneStatusResponse(
        enabled=get_settings().persephone.enabled,
        archived_count=int(archive_row["archived_count"] or 0) if archive_row else 0,
        last_run_at=last_run_at.isoformat() if last_run_at else None,
        oldest_unrecalled=oldest_unrecalled.isoformat() if oldest_unrecalled else None,
        namespace=namespace,
    )


# ── GRAEAE provider manifest ──────────────────────────────────────────────────

@router.post("/graeae/reload-providers")
async def reload_graeae_providers(_: UserContext = Depends(require_root)):
    """Refresh the GRAEAE muse manifest from model_registry.

    Lets the daily provider /v1/models cron rotate model_ids in-process
    without restarting the container. Returns a dict of changes.
    """
    require_postgres_pool_or_503(route_label="POST /admin/graeae/reload-providers")
    from mnemos.domain.graeae.engine import get_graeae_engine
    changes = await get_graeae_engine().reload_from_registry(_lc._pool)
    return {"changes": changes, "providers": {
        n: {"model": cfg["model"], "weight": cfg["weight"]}
        for n, cfg in get_graeae_engine().providers.items()
    }}


# ── GDPR right-to-be-forgotten ────────────────────────────────────────────────
#
# Scaffold for a 3-step lifecycle: requested → confirmed →
# sweep_verifying → soft_deleted → (hard_deleted | restored). The admin endpoints
# below cover the lifecycle's request-side: creating, listing,
# inspecting, confirming, and cancelling a request. The actual
# soft-delete sweep is performed by a worker (round-78+) that
# scans for ``status = 'confirmed'`` rows. The hard-delete
# transition runs after ``restore_by`` has passed.
#
# The endpoints are root-only. ``deletion_requests`` rows
# survive the wipe — they're the audit-bearing breadcrumb that
# proves a deletion happened. Schema details are in
# ``db/migrations_v4_2_deletion_requests.sql``.


def _row_to_deletion_request(row) -> DeletionRequestItem:
    """Translate a ``deletion_requests`` row to its API
    representation. ``id`` and ``UUID``-typed timestamps come
    back from asyncpg as native types; the response model uses
    ISO strings.
    """
    def _ts(value):
        return value.isoformat() if value is not None else None

    return DeletionRequestItem(
        id=str(row["id"]),
        target_user_id=row["target_user_id"],
        target_namespace=row["target_namespace"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"].isoformat(),
        confirmed_at=_ts(row["confirmed_at"]),
        soft_deleted_at=_ts(row["soft_deleted_at"]),
        restore_by=_ts(row["restore_by"]),
        restored_at=_ts(row["restored_at"]),
        hard_deleted_at=_ts(row["hard_deleted_at"]),
        status=row["status"],
        notes=row.get("notes"),
    )


def _normalize_deletion_target(
    target_user_id: str,
    target_namespace: Optional[str],
) -> tuple[str, Optional[str]]:
    """Strip + validate request inputs.

    Codex review of round-77 caught two correctness gaps:

    * Whitespace-only ``target_user_id`` (e.g., ``"   "``)
      passed the falsy check and persisted as a real
      identifier nothing would later match.
    * Empty-string ``target_namespace`` was stored as
      ``""`` (a real namespace) instead of NULL — the
      docs say NULL means "wipe all namespaces", so
      ``""`` collapses the two scopes into an unsafe
      lookalike.

    The COALESCE sentinel ``'*'`` is reserved for the
    partial-unique-index encoding; reject it as an
    explicit namespace too so operators can't accidentally
    bypass the all-namespaces uniqueness gate.
    """
    # ``str.strip()`` trims all Python whitespace including
    # Unicode characters (NBSP, em-space, narrow no-break,
    # ideographic space, etc.). The DB's
    # ``mnemos_is_blank_namespace`` function matches the same
    # set so API and DB agree on what "blank" means. Codex
    # review-4 of round-80 caught the prior implementations
    # that silently accepted Unicode-whitespace namespaces.
    user = (target_user_id or "").strip()
    if not user:
        raise HTTPException(
            status_code=422,
            detail="target_user_id is required and must not be blank",
        )

    if target_namespace is None:
        ns = None
    else:
        stripped = target_namespace.strip()
        ns = stripped or None  # "" / whitespace → None (all-namespaces).

    if ns == "*":
        # Sentinel collides with the active-row unique-index
        # COALESCE encoding. Refuse explicit '*' so a request
        # for the literal namespace ``"*"`` can't masquerade
        # as the all-namespaces scope.
        raise HTTPException(
            status_code=422,
            detail=(
                "target_namespace='*' is reserved by the "
                "deletion_requests active-row unique index — "
                "use null/omit to mean 'all namespaces'."
            ),
        )

    return user, ns


@router.post(
    "/deletion-requests",
    response_model=DeletionRequestItem,
    status_code=201,
)
async def create_deletion_request(
    request: DeletionRequestCreate,
    user: UserContext = Depends(require_root),
):
    """Record a GDPR right-to-be-forgotten request.

    The endpoint is root-only and does NOT perform any wipe
    itself — it only records the request in
    ``deletion_requests``. The wipe is gated behind a separate
    confirmation step (``POST /admin/deletion-requests/{id}/
    confirm``) so a typo'd ``target_user_id`` doesn't trigger
    an irreversible cascade.

    Returns 409 if any existing non-terminal request COVERS
    OR IS COVERED BY the new request's scope. Concretely:

      * Same user + same explicit namespace → exact overlap.
      * Same user + new request's namespace IS NULL (all-
        namespaces) AND any existing namespace-specific
        request exists → containment overlap.
      * Same user + new request has an explicit namespace AND
        an existing all-namespaces (NULL) request exists →
        reverse containment overlap.

    The partial unique index alone catches only exact-pair
    overlaps; the SELECT guard inside an advisory-lock
    transaction catches the NULL-vs-specific containment
    cases. Codex review of round-77 caught this gap.
    """
    require_postgres_pool_or_503(route_label="POST /admin/deletion-requests")

    target_user_id, target_namespace = _normalize_deletion_target(
        request.target_user_id, request.target_namespace
    )
    notes = (request.notes or "").strip() or None

    # Stable signed-int64 advisory-lock key derived from the
    # target user_id. Serializes concurrent CREATEs for the
    # same user so the SELECT-guard + INSERT pair is atomic
    # without holding a row lock on every active request.
    import hashlib as _hashlib

    digest = _hashlib.blake2b(
        target_user_id.encode("utf-8"), digest_size=8
    ).digest()
    lock_key_unsigned = int.from_bytes(digest, "big", signed=False)
    # pg_advisory_xact_lock takes a signed bigint. Map the
    # 64-bit unsigned digest into the signed range.
    lock_key = lock_key_unsigned - (1 << 63)

    async with _lc.get_pool_manager().acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    lock_key,
                )

                # Overlap check: any existing active row with
                # matching user that covers or is covered by
                # the new scope. ``mnemos_is_blank_namespace``
                # (defined in the round-81 cleanup migration)
                # matches Python's ``str.strip()`` semantics —
                # ASCII whitespace AND Unicode whitespace
                # (NBSP, em-space, narrow no-break, etc.). A
                # legacy round-77 row with whitespace-only
                # ``target_namespace`` is treated as the
                # all-namespaces scope at query time, even
                # before the round-81 migration normalizes the
                # row. Codex review-3 of round-79 + review-4 of
                # round-80 walked this gap progressively:
                # ``BTRIM`` only trimmed spaces; POSIX
                # ``[[:space:]]`` only matched ASCII; the SQL
                # function now enumerates the full Python
                # whitespace set so API + DB agree.
                overlap = await conn.fetchrow(
                    """
                    SELECT id, target_namespace, status
                      FROM deletion_requests
                     WHERE target_user_id = $1
                       AND status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted')
                       AND (
                            mnemos_is_blank_namespace(target_namespace)
                         OR $2::text IS NULL
                         OR target_namespace = $2::text
                       )
                     LIMIT 1
                    """,
                    target_user_id,
                    target_namespace,
                )
                if overlap is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"deletion request {overlap['id']} (status="
                            f"{overlap['status']!r}, target_namespace="
                            f"{overlap['target_namespace']!r}) overlaps "
                            f"the requested scope. Cancel or progress "
                            f"the existing request before creating a "
                            f"new one."
                        ),
                    )

                row = await conn.fetchrow(
                    """
                    INSERT INTO deletion_requests
                      (target_user_id, target_namespace, requested_by, notes)
                    VALUES ($1, $2, $3, $4)
                    RETURNING *
                    """,
                    target_user_id,
                    target_namespace,
                    user.user_id,
                    notes,
                )
        except HTTPException:
            raise
        except Exception as e:
            # Defense-in-depth: the partial unique index
            # ``deletion_requests_active_unique_idx`` still
            # catches an exact-pair race the advisory lock +
            # SELECT-guard above shouldn't admit. Surface that
            # as 409 in case the index ever fires.
            import asyncpg as _asyncpg

            if isinstance(e, _asyncpg.UniqueViolationError):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A non-terminal deletion request already "
                        f"exists for target_user_id={target_user_id!r} "
                        f"target_namespace={target_namespace!r}."
                    ),
                )
            raise

    logger.info(
        "[ADMIN] Created deletion request %s for target_user_id=%s "
        "target_namespace=%s by %s",
        row["id"], row["target_user_id"], row["target_namespace"],
        row["requested_by"],
    )
    return _row_to_deletion_request(row)


@router.get(
    "/deletion-requests",
    response_model=DeletionRequestListResponse,
)
async def list_deletion_requests(
    _: UserContext = Depends(require_root),
    status: Optional[str] = None,
    target_user_id: Optional[str] = None,
    limit: int = 100,
):
    """List deletion requests, optionally filtered by status
    and/or target_user_id. Newest first."""
    require_postgres_pool_or_503(route_label="GET /admin/deletion-requests")
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 1000",
        )
    clauses: List[str] = []
    args: list = []
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    if target_user_id:
        args.append(target_user_id)
        clauses.append(f"target_user_id = ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    sql = (
        "SELECT * FROM deletion_requests"
        f"{where} ORDER BY requested_at DESC LIMIT ${len(args)}"
    )
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(sql, *args)
    items = [_row_to_deletion_request(r) for r in rows]
    return DeletionRequestListResponse(count=len(items), requests=items)


@router.get(
    "/deletion-requests/{request_id}",
    response_model=DeletionRequestItem,
)
async def get_deletion_request(
    request_id: str,
    _: UserContext = Depends(require_root),
):
    require_postgres_pool_or_503(
        route_label="GET /admin/deletion-requests/{request_id}"
    )
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deletion_requests WHERE id = $1::uuid",
            request_id,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"deletion request {request_id} not found",
        )
    return _row_to_deletion_request(row)


@router.post(
    "/deletion-requests/{request_id}/confirm",
    response_model=DeletionRequestItem,
)
async def confirm_deletion_request(
    request_id: str,
    _: UserContext = Depends(require_root),
):
    """Confirm a pending deletion request.

    Transitions ``status='requested'`` to ``status='confirmed'``
    and sets ``confirmed_at = now()``. The wipe worker
    (round-78+) consumes confirmed rows.

    Idempotent on already-confirmed rows. Refuses to confirm
    rows in any other state — operators must cancel and
    re-create if they want to revert a state past 'confirmed'.
    """
    require_postgres_pool_or_503(
        route_label="POST /admin/deletion-requests/{request_id}/confirm"
    )
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'confirmed',
                   confirmed_at = COALESCE(confirmed_at, NOW())
             WHERE id = $1::uuid
               AND status IN ('requested', 'confirmed')
            RETURNING *
            """,
            request_id,
        )
    if row is None:
        # Either not found or in a non-confirmable state.
        async with _lc.get_pool_manager().acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT status FROM deletion_requests WHERE id = $1::uuid",
                request_id,
            )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"deletion request {request_id} not found",
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"deletion request {request_id} is in state "
                f"{existing['status']!r}; only 'requested' or "
                f"'confirmed' rows can be confirmed"
            ),
        )
    logger.info(
        "[ADMIN] Confirmed deletion request %s (target_user_id=%s)",
        row["id"], row["target_user_id"],
    )
    return _row_to_deletion_request(row)


@router.post(
    "/deletion-requests/{request_id}/cancel",
    response_model=DeletionRequestItem,
)
async def cancel_deletion_request(
    request_id: str,
    _: UserContext = Depends(require_root),
):
    """Cancel a deletion request before any wipe has executed.

    Refuses to cancel ``soft_deleted`` / ``hard_deleted`` rows
    — those have already destroyed (or partially destroyed)
    data. Operators can still cancel ``requested`` and
    ``confirmed`` rows that haven't reached the worker yet.
    """
    require_postgres_pool_or_503(
        route_label="POST /admin/deletion-requests/{request_id}/cancel"
    )
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'cancelled'
             WHERE id = $1::uuid
               AND status IN ('requested', 'confirmed')
            RETURNING *
            """,
            request_id,
        )
    if row is None:
        async with _lc.get_pool_manager().acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT status FROM deletion_requests WHERE id = $1::uuid",
                request_id,
            )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"deletion request {request_id} not found",
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"deletion request {request_id} is in state "
                f"{existing['status']!r}; only 'requested' or "
                f"'confirmed' rows can be cancelled"
            ),
        )
    logger.info(
        "[ADMIN] Cancelled deletion request %s (target_user_id=%s)",
        row["id"], row["target_user_id"],
    )
    return _row_to_deletion_request(row)


@router.post(
    "/deletion-requests/{request_id}/restore",
    response_model=DeletionRequestItem,
)
async def restore_deletion_request(
    request_id: str,
    _: UserContext = Depends(require_root),
):
    """Restore a soft-deleted deletion request during its grace window."""
    require_postgres_pool_or_503(
        route_label="POST /admin/deletion-requests/{request_id}/restore"
    )
    from mnemos.workers.deletion_request_worker import (
        invalidate_deletion_scope_caches,
        restore_soft_deleted_target,
    )

    restored_target: tuple[str, str | None] | None = None
    async with _lc.get_pool_manager().transactional() as conn:
        existing = await conn.fetchrow(
            """
            SELECT *
              FROM deletion_requests
             WHERE id = $1::uuid
             FOR UPDATE
            """,
            request_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"deletion request {request_id} not found",
            )
        if existing["status"] != "soft_deleted":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deletion request {request_id} is in state "
                    f"{existing['status']!r}; only 'soft_deleted' "
                    f"rows can be restored"
                ),
            )
        if existing["restore_by"] is None or existing["soft_deleted_at"] is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deletion request {request_id} is missing "
                    "restore metadata"
                ),
            )
        restore_window_expired = await conn.fetchval(
            "SELECT $1::timestamptz <= NOW()",
            existing["restore_by"],
        )
        if restore_window_expired:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deletion request {request_id} restore window "
                    f"expired at {existing['restore_by'].isoformat()}"
                ),
            )

        await restore_soft_deleted_target(
            conn,
            existing["target_user_id"],
            existing["target_namespace"],
            existing["soft_deleted_at"],
            invalidate_cache=False,
        )
        restored_target = (existing["target_user_id"], existing["target_namespace"])
        row = await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'restored',
                   restored_at = NOW()
             WHERE id = $1::uuid
               AND status = 'soft_deleted'
            RETURNING *
            """,
            request_id,
        )

    if restored_target is not None:
        await invalidate_deletion_scope_caches(*restored_target)

    logger.info(
        "[ADMIN] Restored deletion request %s (target_user_id=%s)",
        row["id"], row["target_user_id"],
    )
    return _row_to_deletion_request(row)


@router.post(
    "/deletion-requests/{request_id}/force-purge",
    response_model=DeletionRequestItem,
)
async def force_purge_deletion_request(
    request_id: str,
    _: UserContext = Depends(require_root),
):
    """Immediately hard-delete a soft-deleted request.

    Root-only operator override for urgent legal requests. It bypasses
    the ``restore_by`` grace-window check but still requires the row to
    be in ``status='soft_deleted'`` so requested/confirmed/restored
    lifecycle states cannot be purged accidentally.
    """
    require_postgres_pool_or_503(
        route_label="POST /admin/deletion-requests/{request_id}/force-purge"
    )
    from mnemos.workers.deletion_request_worker import (
        hard_delete_target,
        invalidate_deletion_scope_caches,
    )

    purged_target: tuple[str, str | None] | None = None
    async with _lc.get_pool_manager().transactional() as conn:
        existing = await conn.fetchrow(
            """
            SELECT *
              FROM deletion_requests
             WHERE id = $1::uuid
             FOR UPDATE
            """,
            request_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"deletion request {request_id} not found",
            )
        if existing["status"] != "soft_deleted":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deletion request {request_id} is in state "
                    f"{existing['status']!r}; only 'soft_deleted' "
                    f"rows can be force-purged"
                ),
            )

        await hard_delete_target(
            conn,
            existing["target_user_id"],
            existing["target_namespace"],
            invalidate_cache=False,
        )
        purged_target = (existing["target_user_id"], existing["target_namespace"])
        row = await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'hard_deleted',
                   hard_deleted_at = NOW()
             WHERE id = $1::uuid
               AND status = 'soft_deleted'
            RETURNING *
            """,
            request_id,
        )
        if row is None:
            raise RuntimeError(
                f"deletion request {request_id} disappeared before hard-delete transition"
            )

    if purged_target is not None:
        await invalidate_deletion_scope_caches(*purged_target)

    logger.info(
        "[ADMIN] Force-purged deletion request %s (target_user_id=%s)",
        row["id"], row["target_user_id"],
    )
    return _row_to_deletion_request(row)

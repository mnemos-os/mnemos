"""MNEMOS v1 admin endpoints — user and API key management (root only)."""
import hashlib
import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.domain.models import (
    ApiKeyCreateRequest,
    ApiKeyResponse,
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
                "SELECT id, owner_id FROM memories WHERE id = ANY($1::text[])",
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
    where_parts: list[str] = []
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

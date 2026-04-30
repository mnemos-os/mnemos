"""Replacement body for api/auth.py with session-cookie support.

Drop-in replacement. Existing Bearer flow preserved; session-cookie flow
added as a secondary path. Auth-disabled mode unchanged.
"""
import hashlib
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from mnemos.core.auth_context import UserContext

logger = logging.getLogger(__name__)

_auth_enabled: bool = False
_default_namespace: str = "default"
_personal_user_id: str = "default"

PERSONAL_SINGLETON: "Optional[UserContext]" = None   # set by configure_auth(); None before startup

_bearer = HTTPBearer(auto_error=False)


def configure_auth(config: dict | None = None) -> None:
    """Called once at startup from lifecycle lifespan."""
    from mnemos.core.config import get_settings

    global _auth_enabled, _default_namespace, _personal_user_id, PERSONAL_SINGLETON
    settings = get_settings().auth
    if config is None:
        config = {}
        _auth_enabled = settings.enabled
    else:
        _auth_enabled = config.get("enabled", settings.enabled)
    _default_namespace = config.get("default_namespace", settings.default_namespace)
    _personal_user_id = config.get("personal_user_id", settings.personal_user_id)
    PERSONAL_SINGLETON = UserContext(
        user_id=_personal_user_id,
        group_ids=[],
        role="root",
        namespace=_default_namespace,
        authenticated=False,
    )
    logger.info(
        f"Auth configured: enabled={_auth_enabled}, "
        f"namespace={_default_namespace}, personal_user={_personal_user_id}"
    )


async def _update_last_used(pool, key_id: str) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE api_keys SET last_used=NOW() WHERE id=$1", key_id
            )
    except Exception as e:
        logger.warning(f"[AUTH] Failed to update last_used for key {key_id}: {e}")


async def _user_context_from_id(pool, user_id: str, authenticated: bool) -> "UserContext":
    """Build a UserContext for a resolved user_id (role + groups +
    namespace from DB).

    `namespace` is a per-user column on the `users` table. The config
    default is only used for the auth-disabled / personal singleton path.
    """
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT role, namespace FROM users WHERE id=$1", user_id,
        )
        group_rows = await conn.fetch(
            "SELECT group_id FROM user_groups WHERE user_id=$1", user_id,
        )
    if user_row is None:
        # Session references a user that no longer exists — treat as unauthenticated.
        raise HTTPException(status_code=401, detail="User no longer exists")
    return UserContext(
        user_id=user_id,
        group_ids=[r["group_id"] for r in group_rows],
        role=user_row["role"],
        namespace=user_row["namespace"],
        authenticated=authenticated,
    )


async def get_current_user(
    request: Request,
    credentials=Depends(_bearer),
) -> UserContext:
    """Auth dependency — Bearer token first, session cookie second."""
    if not _auth_enabled:
        if PERSONAL_SINGLETON is None:
            raise HTTPException(status_code=503, detail="Auth not yet configured — startup incomplete")
        return PERSONAL_SINGLETON

    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # 1. API key (Bearer) — existing behaviour.
    if credentials is not None:
        raw_key = credentials.credentials
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ak.id, ak.user_id, ak.revoked, u.role, u.namespace "
                "FROM api_keys ak JOIN users u ON u.id = ak.user_id "
                "WHERE ak.key_hash = $1",
                key_hash,
            )
        if row is None or row["revoked"]:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        from mnemos.core.lifecycle import _schedule_background
        _schedule_background(_update_last_used(pool, str(row["id"])))

        async with pool.acquire() as conn:
            group_rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1", row["user_id"]
            )
        return UserContext(
            user_id=row["user_id"],
            group_ids=[r["group_id"] for r in group_rows],
            role=row["role"],
            namespace=row["namespace"],
            authenticated=True,
        )

    # 2. Session cookie — new v3.0.0 path (only checked when no Bearer).
    from mnemos.core.oauth import SESSION_COOKIE_NAME, resolve_session
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        async with pool.acquire() as conn:
            resolved = await resolve_session(conn, cookie_value)
        if resolved is not None:
            user_id, _identity_id = resolved
            return await _user_context_from_id(pool, user_id, authenticated=True)

    # 3. No credentials.
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_root(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency — raises 403 if caller is not root."""
    if user.role != "root":
        raise HTTPException(status_code=403, detail="Root access required")
    return user

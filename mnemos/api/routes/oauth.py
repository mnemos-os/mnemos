"""OAuth / OIDC endpoints — user-facing login flow.

Mounts under /auth/oauth/*. These endpoints do NOT require authentication:
they establish it. Admin-side provider management is in api/handlers/oauth_admin.py.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core import oauth as _oauth
from mnemos.domain.models import (
    OAuthIdentity,
    OAuthLogoutResponse,
    OAuthMeResponse,
    OAuthProviderListResponse,
    OAuthProviderPublic,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


# ── Public provider list (no auth) ────────────────────────────────────────────


@router.get("/providers", response_model=OAuthProviderListResponse)
async def list_providers_public():
    """List enabled providers for a login UI. No secrets returned."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, display_name, kind, enabled "
            "FROM oauth_providers WHERE enabled=TRUE "
            "ORDER BY display_name"
        )
    providers = [
        OAuthProviderPublic(
            name=r["name"], display_name=r["display_name"],
            kind=r["kind"], enabled=r["enabled"],
        )
        for r in rows
    ]
    return OAuthProviderListResponse(count=len(providers), providers=providers)


# ── Login + callback ──────────────────────────────────────────────────────────


async def _load_provider(name: str):
    """Fetch an enabled provider row, else 404."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, kind, issuer_url, client_id, client_secret, scope, "
            "       authorize_url, token_url, userinfo_url, enabled "
            "FROM oauth_providers WHERE name=$1",
            name,
        )
    if not row or not row["enabled"]:
        raise HTTPException(status_code=404, detail=f"OAuth provider '{name}' not found or disabled")
    return row


@router.get("/{provider}/login")
async def oauth_login(provider: str, request: Request):
    """Start an OAuth authorization-code flow. Redirects to the provider."""
    provider_row = await _load_provider(provider)
    redirect_uri = str(request.url_for("oauth_callback", provider=provider))
    try:
        return await _oauth.start_login(request, provider_row, redirect_uri)
    except Exception as e:
        logger.exception("oauth login start failed for provider=%s", provider)
        raise HTTPException(status_code=502, detail=f"OAuth provider error: {e}")


@router.get("/{provider}/callback", name="oauth_callback")
async def oauth_callback(provider: str, request: Request):
    """Provider redirect target. Exchanges code, provisions user, sets cookie."""
    provider_row = await _load_provider(provider)

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        try:
            user_id, identity_id, claims = await _oauth.finish_login(
                request, provider_row, conn,
            )
        except Exception as e:
            logger.exception("oauth callback failed for provider=%s", provider)
            raise HTTPException(status_code=502, detail=f"OAuth callback error: {e}")

        session_id = await _oauth.create_session(conn, user_id, identity_id, request)

    # Where to send the browser now.
    post_login_redirect = request.query_params.get("next") or "/"
    # Open-redirect defense: only allow local absolute paths. Reject:
    #   - anything not starting with "/" (absolute URLs, javascript:, etc.)
    #   - protocol-relative targets like "//evil.com" (browsers treat as URL)
    #   - backslash variants "/\evil.com" (some browsers normalize)
    if (
        not post_login_redirect.startswith("/")
        or post_login_redirect.startswith("//")
        or post_login_redirect.startswith("/\\")
    ):
        post_login_redirect = "/"

    # Determine whether to set the Secure flag. Behind a TLS-terminating proxy
    # `request.url.scheme` is "http" even when the client is on HTTPS — trust
    # X-Forwarded-Proto when OAUTH_TRUST_PROXY is set (and the proxy is
    # configured to rewrite the header).
    import os as _os
    _trust_proxy = _os.getenv("OAUTH_TRUST_PROXY", "false").lower() == "true"
    _xfp = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    is_https = (
        request.url.scheme == "https"
        or (_trust_proxy and _xfp == "https")
        or _os.getenv("MNEMOS_SESSION_HTTPS_ONLY", "").lower() in ("1", "true")
    )
    response: RedirectResponse = RedirectResponse(url=post_login_redirect, status_code=303)
    response.set_cookie(
        key=_oauth.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=_oauth.SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    logger.info(
        "oauth: session created user_id=%s provider=%s identity=%s",
        user_id, provider, identity_id,
    )
    return response


# ── Logout ────────────────────────────────────────────────────────────────────


@router.post("/logout", response_model=OAuthLogoutResponse)
async def oauth_logout(
    request: Request,
    all_devices: bool = False,
    user: UserContext = Depends(get_current_user),
):
    """Invalidate the current session cookie (or all sessions for the user)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    sessions_revoked = 0
    async with _lc._pool.acquire() as conn:
        if all_devices:
            sessions_revoked = await _oauth.revoke_all_sessions(conn, user.user_id)
        else:
            cookie_session = request.cookies.get(_oauth.SESSION_COOKIE_NAME)
            if cookie_session:
                ok = await _oauth.revoke_session(conn, cookie_session)
                sessions_revoked = 1 if ok else 0

    response = JSONResponse(
        content={"logged_out": True, "sessions_revoked": sessions_revoked}
    )
    response.delete_cookie(_oauth.SESSION_COOKIE_NAME, path="/")
    return response


# ── Me ────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=OAuthMeResponse)
async def oauth_me(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Who am I? Works with either auth method."""
    identity: Optional[OAuthIdentity] = None

    # If authenticated via session cookie, hydrate the most-recent identity.
    cookie_session = request.cookies.get(_oauth.SESSION_COOKIE_NAME)
    auth_method = "personal" if not user.authenticated else "api_key"

    if cookie_session and _lc._pool:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT identity_id::text AS identity_id FROM oauth_sessions "
                "WHERE session_id=$1 AND NOT revoked",
                cookie_session,
            )
            if row and row["identity_id"]:
                auth_method = "session"
                ident = await conn.fetchrow(
                    "SELECT id::text AS id, user_id, provider, external_id, "
                    "       email, display_name, last_login_at, created "
                    "FROM oauth_identities WHERE id=$1::uuid",
                    row["identity_id"],
                )
                if ident:
                    identity = OAuthIdentity(
                        id=ident["id"],
                        user_id=ident["user_id"],
                        provider=ident["provider"],
                        external_id=ident["external_id"],
                        email=ident["email"],
                        display_name=ident["display_name"],
                        last_login_at=ident["last_login_at"].isoformat() if ident["last_login_at"] else None,
                        created=ident["created"].isoformat(),
                    )

    return OAuthMeResponse(
        user_id=user.user_id,
        role=user.role,
        namespace=user.namespace,
        authenticated=user.authenticated,
        auth_method=auth_method,
        identity=identity,
    )

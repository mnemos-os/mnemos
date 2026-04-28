"""OAuth / OIDC flow core — authlib wrapper + session helpers.

Public API used by mnemos.api.routes.oauth and mnemos.api.dependencies:
  - build_client(provider_row) -> authlib OAuth client
  - start_login(request, provider_row) -> redirect URL
  - finish_login(request, provider_row, conn) -> (user_id, identity_id)
  - provision_or_link_user(conn, provider_name, claims) -> user_id, identity_id
  - create_session(conn, user_id, identity_id, request) -> session_id
  - resolve_session(conn, session_id) -> (user_id, identity_id) or None
  - revoke_session(conn, session_id) -> bool
  - revoke_all_sessions(conn, user_id) -> int
  - gc_expired_sessions(pool) -> int

Design notes
------------
- Session cookie is a 256-bit random token (43 chars url-safe base64). Stored
  server-side in `oauth_sessions`; cookie carries just the id. Revocation is
  a UPDATE; no blocklist needed.
- PKCE is enabled by default for the authorization-code flow.
- When `kind == 'oidc'`, authlib handles discovery via `server_metadata_url`.
- When `kind == 'oauth2'`, the admin must supply authorize_url/token_url/userinfo_url.
- User provisioning rule: look up `(provider, external_id)` first; if present
  reuse user. Otherwise, if claims include an email that matches an existing
  user's email, link the identity to that user. Otherwise create a new user
  with id = `{provider}:{external_id}` sanitized.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import asyncpg
from fastapi import Request
from starlette.responses import RedirectResponse

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "mnemos_session"
SESSION_TTL = timedelta(days=30)
SESSION_COOKIE_MAX_AGE = int(SESSION_TTL.total_seconds())


# ── authlib client ────────────────────────────────────────────────────────────


def _build_oauth_registry():
    """Lazily imported authlib registry (avoid import cost when OAuth disabled)."""
    from authlib.integrations.starlette_client import OAuth
    return OAuth()


async def build_client(provider_row) -> Any:
    """Return an authlib client for the given provider row."""
    oauth = _build_oauth_registry()
    kwargs: Dict[str, Any] = {
        "client_id": provider_row["client_id"],
        "client_secret": provider_row["client_secret"],
        "client_kwargs": {"scope": provider_row["scope"]},
    }
    if provider_row["kind"] == "oidc":
        if not provider_row["issuer_url"]:
            raise ValueError(f"provider {provider_row['name']} has kind=oidc but no issuer_url")
        # authlib's OIDC discovery uses server_metadata_url
        kwargs["server_metadata_url"] = _discovery_url(provider_row["issuer_url"])
    else:
        kwargs["authorize_url"] = provider_row["authorize_url"]
        kwargs["access_token_url"] = provider_row["token_url"]
        if provider_row.get("userinfo_url"):
            kwargs["userinfo_endpoint"] = provider_row["userinfo_url"]

    oauth.register(name=provider_row["name"], **kwargs)
    return oauth.create_client(provider_row["name"])


def _discovery_url(issuer: str) -> str:
    """Construct the OIDC discovery URL from an issuer (handles trailing slash)."""
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


# ── Login flow ────────────────────────────────────────────────────────────────


async def start_login(request: Request, provider_row, redirect_uri: str) -> RedirectResponse:
    """Kick off the authorization-code flow. Returns a RedirectResponse to the provider."""
    client = await build_client(provider_row)
    return await client.authorize_redirect(request, redirect_uri)


async def finish_login(
    request: Request,
    provider_row,
    conn: asyncpg.Connection,
) -> Tuple[str, str, Dict[str, Any]]:
    """Complete the callback, return (user_id, identity_id, claims).

    `claims` is the verified ID-token payload (OIDC) or userinfo response.
    """
    client = await build_client(provider_row)
    token = await client.authorize_access_token(request)

    claims: Dict[str, Any] = {}
    # Prefer parsed OIDC id_token if present
    if "id_token" in token:
        try:
            claims = dict(token.get("userinfo") or await client.parse_id_token(request, token))
        except Exception:
            pass
    if not claims:
        # Fall back to userinfo endpoint
        try:
            resp = await client.userinfo(token=token)
            claims = dict(resp)
        except Exception as e:
            logger.warning("userinfo fetch failed for %s: %s", provider_row["name"], e)
            claims = {}

    external_id = _extract_external_id(provider_row["name"], claims)
    if not external_id:
        raise ValueError(
            f"provider {provider_row['name']} returned no usable external id in claims: "
            f"{list(claims.keys())}"
        )

    user_id, identity_id = await provision_or_link_user(
        conn, provider_row["name"], external_id, claims,
    )
    return user_id, identity_id, claims


def _extract_external_id(provider_name: str, claims: Dict[str, Any]) -> Optional[str]:
    """Per-provider external-id extraction with sensible fallbacks."""
    # OIDC standard claim
    if claims.get("sub"):
        return str(claims["sub"])
    # GitHub uses 'id' at userinfo
    if provider_name == "github" and claims.get("id"):
        return str(claims["id"])
    # Azure: 'oid' (object ID) is more stable than sub across tenants
    if provider_name.startswith("azure") and claims.get("oid"):
        return str(claims["oid"])
    # Generic email fallback (last resort)
    if claims.get("email"):
        return f"email:{claims['email']}"
    return None


# ── User provisioning ────────────────────────────────────────────────────────


_USER_ID_SAFE = re.compile(r"[^a-zA-Z0-9._:-]+")


def _mint_user_id(provider: str, external_id: str) -> str:
    slug = _USER_ID_SAFE.sub("", f"{provider}:{external_id}")
    return slug[:64] or f"{provider}:{secrets.token_hex(6)}"


async def provision_or_link_user(
    conn: asyncpg.Connection,
    provider: str,
    external_id: str,
    claims: Dict[str, Any],
) -> Tuple[str, str]:
    """Find-or-create a user for this identity. Return (user_id, identity_id str)."""
    import json as _json

    # 1. Exact identity match — this provider has seen this external_id before.
    existing = await conn.fetchrow(
        "SELECT id, user_id FROM oauth_identities WHERE provider=$1 AND external_id=$2",
        provider, external_id,
    )
    if existing:
        await conn.execute(
            "UPDATE oauth_identities SET last_login_at=NOW(), raw_claims=$2::jsonb "
            "WHERE id=$1",
            existing["id"], _json.dumps(claims),
        )
        return existing["user_id"], str(existing["id"])

    email = claims.get("email")
    display_name = claims.get("name") or claims.get("preferred_username")
    # OIDC standard: email_verified indicates the provider has attested the
    # email address. Providers that don't set it (or set false) cannot be used
    # to link a new identity to a pre-existing account — otherwise an attacker
    # who controls a permissive provider can claim any user by guessing email.
    #
    # Type handling: some OIDC providers emit the claim as a JSON string
    # ("true" / "false") rather than a bool. A naive bool("false") returns
    # True and would bypass the guard. Accept only the documented truthy
    # forms; anything else — including missing, None, 0, empty string,
    # string "false" — is treated as unverified.
    _ev = claims.get("email_verified")
    if isinstance(_ev, bool):
        email_verified = _ev
    elif isinstance(_ev, str):
        email_verified = _ev.strip().lower() == "true"
    else:
        email_verified = False

    # 2. Email match — link this new identity to an existing user with matching
    #    email, BUT only when the provider attests the email is verified.
    user_id: Optional[str] = None
    if email and email_verified:
        link_target = await conn.fetchrow(
            "SELECT id FROM users WHERE email=$1", email,
        )
        if link_target:
            user_id = link_target["id"]
            logger.info(
                "oauth: linking verified email %s to existing user_id=%s via provider=%s",
                email, user_id, provider,
            )
    elif email and not email_verified:
        logger.warning(
            "oauth: email=%s present but email_verified is false/missing for "
            "provider=%s — minting fresh user instead of linking",
            email, provider,
        )

    # 3. Create a new user if no match.
    if user_id is None:
        user_id = _mint_user_id(provider, external_id)
        await conn.execute(
            "INSERT INTO users (id, display_name, email, role) "
            "VALUES ($1, $2, $3, 'user') "
            "ON CONFLICT (id) DO NOTHING",
            user_id, display_name, email,
        )

    identity_id = await conn.fetchval(
        """
        INSERT INTO oauth_identities
          (user_id, provider, external_id, email, display_name, raw_claims, last_login_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
        RETURNING id
        """,
        user_id, provider, external_id, email, display_name, _json.dumps(claims),
    )
    logger.info(
        "oauth: provisioned user_id=%s via provider=%s external_id=%s",
        user_id, provider, external_id,
    )
    return user_id, str(identity_id)


# ── Session management ───────────────────────────────────────────────────────


async def create_session(
    conn: asyncpg.Connection,
    user_id: str,
    identity_id: Optional[str],
    request: Request,
) -> str:
    """Insert a session row and return its id (the cookie value)."""
    session_id = secrets.token_urlsafe(48)   # ~64 url-safe chars
    expires = datetime.now(timezone.utc) + SESSION_TTL
    user_agent = request.headers.get("user-agent", "")[:500]
    ip = request.client.host if request.client else None

    await conn.execute(
        """
        INSERT INTO oauth_sessions
          (session_id, user_id, identity_id, expires_at, user_agent, ip_address)
        VALUES ($1, $2, $3::uuid, $4, $5, $6::inet)
        """,
        session_id, user_id, identity_id, expires, user_agent, ip,
    )
    return session_id


async def resolve_session(
    conn: asyncpg.Connection,
    session_id: str,
) -> Optional[Tuple[str, Optional[str]]]:
    """Return (user_id, identity_id) if the session is valid, else None.

    Touches last_used_at on hit. Expired and revoked sessions resolve to None.
    """
    row = await conn.fetchrow(
        """
        SELECT user_id, identity_id::text AS identity_id, expires_at, revoked
        FROM oauth_sessions WHERE session_id=$1
        """,
        session_id,
    )
    if not row:
        return None
    if row["revoked"]:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc):
        return None
    await conn.execute(
        "UPDATE oauth_sessions SET last_used_at=NOW() WHERE session_id=$1",
        session_id,
    )
    return row["user_id"], row["identity_id"]


async def revoke_session(conn: asyncpg.Connection, session_id: str) -> bool:
    result = await conn.execute(
        "UPDATE oauth_sessions SET revoked=TRUE, revoked_at=NOW() "
        "WHERE session_id=$1 AND NOT revoked",
        session_id,
    )
    return result != "UPDATE 0"


async def revoke_all_sessions(conn: asyncpg.Connection, user_id: str) -> int:
    result = await conn.execute(
        "UPDATE oauth_sessions SET revoked=TRUE, revoked_at=NOW() "
        "WHERE user_id=$1 AND NOT revoked",
        user_id,
    )
    # result looks like "UPDATE N"
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


async def gc_expired_sessions(pool: asyncpg.Pool) -> int:
    """Delete expired/revoked-long-ago sessions. Returns rows deleted."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM oauth_sessions "
            "WHERE expires_at < NOW() - INTERVAL '7 days' "
            "   OR (revoked AND revoked_at < NOW() - INTERVAL '30 days')"
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

"""Webhook subscription CRUD — /v1/webhooks.

Outbound notifications on memory and consultation events. Delivery is handled
by `mnemos.webhooks.dispatcher`; this handler is CRUD only.
"""
import asyncio
import ipaddress
import logging
import os
import secrets
import socket
import uuid as _uuid
from typing import List, Union
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException


def _parse_uuid_or_404(value: str, what: str = "resource") -> str:
    """Validate a UUID path parameter. Raises 404 on malformed input so we
    don't surface internal driver errors (`InvalidTextRepresentation`) as 500s."""
    try:
        _uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.domain.models import (
    VALID_WEBHOOK_EVENTS,
    WebhookCreateRequest,
    WebhookCreateResponse,
    WebhookDelivery,
    WebhookDeliveryListResponse,
    WebhookItem,
    WebhookListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_events(events: List[str]) -> None:
    if not events:
        raise HTTPException(status_code=422, detail="events must not be empty")
    bad = [e for e in events if e not in VALID_WEBHOOK_EVENTS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"unknown events: {bad}. valid events: {sorted(VALID_WEBHOOK_EVENTS)}",
        )


_WEBHOOK_ALLOW_PRIVATE = os.getenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "false").lower() == "true"

# Cloud-provider instance-metadata hostnames we always refuse, even when
# WEBHOOK_ALLOW_PRIVATE_HOSTS=true. Includes the link-local IP literals as a
# belt check (they're also caught by the is_link_local / is_private tests).
_BLOCKED_METADATA_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "metadata.tencentyun.com",
    "100-100-100-200.cn-hangzhou.ecs.aliyuncs.com",
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
    "fe80::a9fe:a9fe",
})

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _is_blocked_ip(ip: _IPAddress) -> bool:
    """SSRF defense: block loopback, private, link-local, multicast, reserved."""
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_addrs(host: str) -> List[str]:
    """Resolve host asynchronously (non-blocking for the event loop).

    Uses the running loop's getaddrinfo which wraps the system resolver in a
    thread-executor so a slow DNS server doesn't freeze the ASGI worker.
    """
    loop = asyncio.get_event_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def validate_webhook_url(url: str) -> None:
    """Validate a webhook URL: scheme + host not pointing at internal services.

    Called at both subscription create time and dispatch time. Raises
    HTTPException(422) on bad input. Set WEBHOOK_ALLOW_PRIVATE_HOSTS=true to
    permit private/loopback targets (useful for local testing; unsafe in
    production).

    Caveat — DNS rebinding: between this validation and the subsequent
    httpx connect, a hostile DNS server could switch the record from a
    public address to an internal one. For genuinely untrusted subscription
    creators, enforce server-side TLS pinning to a known egress proxy, or
    pass the pre-resolved IP to httpx and keep the Host header. This
    validator is a strong filter, not a complete mitigation on its own.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=422, detail="url must include a host")

    if host.lower() in _BLOCKED_METADATA_HOSTS:
        raise HTTPException(status_code=422, detail="url host is not permitted")

    if _WEBHOOK_ALLOW_PRIVATE:
        return

    # If host is already an IP literal, check it directly. Otherwise resolve
    # (asynchronously) and check every returned address family.
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")
        return
    except ValueError:
        pass  # not a literal IP — resolve DNS

    try:
        addrs = await _resolve_addrs(host)
    except (socket.gaierror, OSError):
        raise HTTPException(status_code=422, detail="url host could not be resolved")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")


# Kept as `_validate_url` alias for callers inside this module.
_validate_url = validate_webhook_url


def _to_item(row) -> WebhookItem:
    return WebhookItem(
        id=str(row["id"]),
        url=row["url"],
        events=list(row["events"]),
        description=row["description"],
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        created=row["created"].isoformat(),
        revoked=row["revoked"],
        revoked_at=row["revoked_at"].isoformat() if row["revoked_at"] else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=WebhookCreateResponse, status_code=201)
async def create_webhook(
    request: WebhookCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a webhook subscription. Returns the HMAC secret exactly once."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    await _validate_url(request.url)
    _validate_events(request.events)

    secret = secrets.token_urlsafe(32)

    # v3.2 Tier 3: non-root cannot create a webhook in a namespace
    # other than their own. Root may pass request.namespace for
    # cross-tenant support.
    if request.namespace and request.namespace != user.namespace:
        if user.role != "root":
            raise HTTPException(
                status_code=403,
                detail="cross-namespace webhook create requires root",
            )
    namespace = request.namespace or user.namespace or "default"

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_subscriptions
              (url, events, secret, description, owner_id, namespace)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, url, events, description, owner_id, namespace, created, revoked
            """,
            request.url,
            request.events,
            secret,
            request.description,
            user.user_id,
            namespace,
        )

    logger.info(
        "webhook created id=%s owner=%s events=%s",
        row["id"], user.user_id, list(row["events"]),
    )

    return WebhookCreateResponse(
        id=str(row["id"]),
        url=row["url"],
        events=list(row["events"]),
        description=row["description"],
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        created=row["created"].isoformat(),
        revoked=row["revoked"],
        secret=secret,
    )


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    user: UserContext = Depends(get_current_user),
    include_revoked: bool = False,
):
    """List the caller's webhook subscriptions. Secrets are never returned."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # v3.2 Tier 3: scope by owner_id + namespace. Root sees all
    # (no owner / namespace filter) so ops can audit cross-tenant.
    is_root = user.role == "root"

    async with _lc._pool.acquire() as conn:
        if is_root:
            where = "" if include_revoked else "WHERE NOT revoked"
            rows = await conn.fetch(
                f"""
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                {where}
                ORDER BY created DESC
                """,
            )
        elif include_revoked:
            rows = await conn.fetch(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE owner_id = $1 AND namespace = $2
                ORDER BY created DESC
                """,
                user.user_id, user.namespace,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE owner_id = $1 AND namespace = $2 AND NOT revoked
                ORDER BY created DESC
                """,
                user.user_id, user.namespace,
            )

    return WebhookListResponse(
        count=len(rows), webhooks=[_to_item(r) for r in rows]
    )


@router.get("/{webhook_id}", response_model=WebhookItem)
async def get_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
):
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # v3.2 Tier 3: non-root must match owner AND namespace.
    # Root reads any webhook.
    is_root = user.role == "root"

    async with _lc._pool.acquire() as conn:
        if is_root:
            row = await conn.fetchrow(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE id = $1::uuid
                """,
                webhook_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3
                """,
                webhook_id, user.user_id, user.namespace,
            )
    if not row:
        raise HTTPException(status_code=404, detail="webhook not found")
    return _to_item(row)


@router.delete("/{webhook_id}", status_code=204)
async def revoke_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Soft-delete: marks the subscription revoked. Delivery log preserved."""
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # v3.2 Tier 3: non-root must match owner AND namespace. Root
    # can revoke any webhook.
    is_root = user.role == "root"

    async with _lc._pool.acquire() as conn:
        if is_root:
            row = await conn.fetchrow(
                """
                UPDATE webhook_subscriptions
                SET revoked = TRUE, revoked_at = NOW()
                WHERE id = $1::uuid AND NOT revoked
                RETURNING id
                """,
                webhook_id,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE webhook_subscriptions
                SET revoked = TRUE, revoked_at = NOW()
                WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3 AND NOT revoked
                RETURNING id
                """,
                webhook_id, user.user_id, user.namespace,
            )
    if not row:
        raise HTTPException(
            status_code=404, detail="webhook not found or already revoked"
        )
    logger.info("webhook revoked id=%s owner=%s", webhook_id, user.user_id)


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_deliveries(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
    limit: int = 50,
):
    """List recent delivery attempts for a subscription."""
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # v3.2 Tier 3: subscription must belong to caller's owner AND
    # namespace. Root bypasses both.
    is_root = user.role == "root"
    async with _lc._pool.acquire() as conn:
        if is_root:
            sub = await conn.fetchrow(
                "SELECT id FROM webhook_subscriptions WHERE id=$1::uuid",
                webhook_id,
            )
        else:
            sub = await conn.fetchrow(
                "SELECT id FROM webhook_subscriptions "
                "WHERE id=$1::uuid AND owner_id=$2 AND namespace=$3",
                webhook_id, user.user_id, user.namespace,
            )
        if not sub:
            raise HTTPException(status_code=404, detail="webhook not found")
        rows = await conn.fetch(
            """
            SELECT id, subscription_id, event_type, attempt_num, status,
                   superseded,
                   response_status, response_body, error,
                   scheduled_at, delivered_at, created
            FROM webhook_deliveries
            WHERE subscription_id = $1::uuid
            ORDER BY created DESC
            LIMIT $2
            """,
            webhook_id, limit,
        )

    deliveries = [
        WebhookDelivery(
            id=str(r["id"]),
            subscription_id=str(r["subscription_id"]),
            event_type=r["event_type"],
            attempt_num=r["attempt_num"],
            status=r["status"],
            superseded=r["superseded"],
            response_status=r["response_status"],
            response_body=r["response_body"],
            error=r["error"],
            scheduled_at=r["scheduled_at"].isoformat(),
            delivered_at=r["delivered_at"].isoformat() if r["delivered_at"] else None,
            created=r["created"].isoformat(),
        )
        for r in rows
    ]
    return WebhookDeliveryListResponse(count=len(deliveries), deliveries=deliveries)

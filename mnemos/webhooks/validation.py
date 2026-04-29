"""Webhook URL validation shared by CRUD routes and delivery workers."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import List, Union
from urllib.parse import urlparse

from fastapi import HTTPException

from mnemos.core.config import get_settings

_WEBHOOK_ALLOW_PRIVATE = get_settings().webhook.allow_private_hosts

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
    """Resolve host asynchronously so DNS cannot block the event loop."""
    loop = asyncio.get_event_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def validate_webhook_url(url: str) -> None:
    """Validate a webhook URL: scheme + host not pointing at internal services."""
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

    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")
        return
    except ValueError:
        pass

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

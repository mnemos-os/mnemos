"""Slice #200: pin that webhook URL validation enforces
``WEBHOOK_DNS_TIMEOUT`` on the async DNS resolution.

Surfaced by the deep cross-code codex audit at HEAD ``de13b51``
(mem_1778221719390_8cb1ba in MNEMOS, MED severity):

    mnemos/webhooks/validation.py:54 — DNS validation calls
    loop.getaddrinfo() without asyncio.wait_for, so slow DNS
    can stall the async webhook validation path.

The configured timeout (``WEBHOOK_DNS_TIMEOUT`` env via
``_WebhookSettings.dns_timeout``, default 10.0s) was used by
``_derive_lease_defaults`` for lease-budget calc but never
actually applied to the resolution. This test pins:

1. ``_resolve_addrs`` wraps ``loop.getaddrinfo`` in
   ``asyncio.wait_for(..., timeout=...)`` with the configured
   timeout.
2. ``validate_webhook_url`` translates the resulting
   ``asyncio.TimeoutError`` into a 422 ``HTTPException`` with a
   recognizable detail string.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mnemos.webhooks import validation as wv


@pytest.mark.asyncio
async def test_resolve_addrs_uses_wait_for_with_configured_timeout(monkeypatch):
    """Patch ``loop.getaddrinfo`` to hang forever and confirm the
    bound triggers an ``asyncio.TimeoutError`` within the
    configured timeout window."""

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    # Patch the loop.getaddrinfo. Pull the loop in the same way
    # _resolve_addrs does so the patch matches.
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _hang)

    # Pin the configured timeout to a tiny value so the test
    # finishes quickly. Patch the live settings object the module
    # imports lazily.
    class _W:
        dns_timeout = 0.05

    class _S:
        webhook = _W()

    monkeypatch.setattr(wv, "get_settings", lambda: _S())

    with pytest.raises(asyncio.TimeoutError):
        await wv._resolve_addrs("example.invalid")


@pytest.mark.asyncio
async def test_validate_webhook_url_translates_dns_timeout_to_422(
    monkeypatch,
):
    """When ``_resolve_addrs`` raises ``asyncio.TimeoutError``,
    ``validate_webhook_url`` must return a 422 with a recognizable
    detail; without the translation, a TimeoutError would propagate
    and the caller would 500 instead."""

    async def _timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(wv, "_resolve_addrs", _timeout)

    with pytest.raises(HTTPException) as excinfo:
        await wv.validate_webhook_url("https://slow-dns.example/")
    assert excinfo.value.status_code == 422
    assert "DNS" in (excinfo.value.detail or "") \
        or "timed out" in (excinfo.value.detail or "")


@pytest.mark.asyncio
async def test_resolve_addrs_returns_addrs_under_timeout_budget(
    monkeypatch,
):
    """Sanity-check: when ``getaddrinfo`` returns within budget,
    the wrapper still returns the resolved address list."""

    async def _fast(*_args, **_kwargs):
        # Mimic the getaddrinfo result tuple shape.
        return [(0, 0, 0, "", ("198.51.100.7", 0))]

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fast)

    class _W:
        dns_timeout = 5.0

    class _S:
        webhook = _W()

    monkeypatch.setattr(wv, "get_settings", lambda: _S())

    addrs = await wv._resolve_addrs("example.test")
    assert addrs == ["198.51.100.7"]

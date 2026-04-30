from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.webhooks import sender, validation


pytestmark = pytest.mark.asyncio


class _RejectingTransport:
    captured: tuple[str, str] | None = None

    def __init__(self, *, hostname: str, resolved_ip: str, **_kwargs):
        type(self).captured = (hostname, resolved_ip)

    async def handle_async_request(self, _request):
        import httpx

        raise httpx.ConnectError("stop before network")

    async def aclose(self):
        return None


async def test_webhook_delivery_uses_first_validated_ip(monkeypatch):
    resolutions = iter([["1.2.3.4"], ["127.0.0.1"]])

    async def resolve(_host):
        return next(resolutions)

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)
    monkeypatch.setattr(sender, "PinnedDNSAsyncHTTPTransport", _RejectingTransport)

    delivery = {
        "id": "delivery-1",
        "revoked": False,
        "url": "https://rebind.example/hook",
        "secret": "secret",
        "payload": "{}",
        "event_type": "memory.created",
        "subscription_id": "sub-1",
        "attempt_num": 1,
    }
    result = await sender._send_claimed_delivery_within_deadline(delivery, send_window_seconds=10)

    assert result.succeeded is False
    assert _RejectingTransport.captured == ("rebind.example", "1.2.3.4")


async def test_webhook_validation_rejects_metadata_ip(monkeypatch):
    async def resolve(_host):
        return ["169.254.169.254"]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://metadata.example/hook")

    assert exc.value.status_code == 422

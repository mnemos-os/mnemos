from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.api.routes import federation
from mnemos.core import config
from mnemos.webhooks import validation


pytestmark = pytest.mark.asyncio


def _reset_settings(monkeypatch, *, allow_private: bool = False):
    monkeypatch.setenv("FEDERATION_ALLOW_INSECURE", "true")
    if allow_private:
        monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE", "true")
    else:
        monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(config, "_settings", None)


async def test_peer_url_rejects_metadata_ip(monkeypatch):
    _reset_settings(monkeypatch)

    async def resolve(_host):
        return ["169.254.169.254"]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await federation._validate_peer_base_url("https://peer.example")

    assert exc.value.status_code == 422


async def test_peer_url_rejects_localhost_unless_private_allowed(monkeypatch):
    _reset_settings(monkeypatch)
    with pytest.raises(HTTPException):
        await federation._validate_peer_base_url("http://127.0.0.1:5002")

    _reset_settings(monkeypatch, allow_private=True)
    await federation._validate_peer_base_url("http://127.0.0.1:5002")

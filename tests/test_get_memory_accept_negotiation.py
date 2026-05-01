"""Integration tests for Accept-header content negotiation on
GET /v1/memories/{memory_id}.

The roadmap entry "Read-path routing on Accept headers" promises:

  * default / application/json / */*  → existing JSON MemoryItem
  * text/plain                         → prose narration body
  * application/x-apollo-dense         → raw winning-variant content

These tests drive the handler directly (matching the pattern in
test_narrate_endpoint.py — async-context pool mock + direct handler
call) so we cover the same dispatch matrix without spinning a real
backend up.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import PlainTextResponse

from mnemos.api.routes.memories import get_memory


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


def _mock_narrate_pool(monkeypatch, memory_row=None, variant_row=None):
    """Mock the narrate dispatch's pool acquisition (matches the
    pattern in test_narrate_endpoint.py)."""
    from mnemos.core import lifecycle

    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[memory_row, variant_row])
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncContext(mock_conn))
    monkeypatch.setattr(lifecycle, "_pool", mock_pool)
    return mock_pool, mock_conn


def _user(role="root", user_id="root", namespace="default"):
    u = MagicMock()
    u.role = role
    u.user_id = user_id
    u.namespace = namespace
    u.authenticated = True
    return u


def _request_with_accept(accept: str | None) -> MagicMock:
    req = MagicMock()
    headers = {}
    if accept is not None:
        headers["accept"] = accept
    req.headers = headers
    return req


# ── Accept: text/plain → narrated prose ────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_text_plain_returns_narrated_prose(monkeypatch):
    _mock_narrate_pool(
        monkeypatch,
        memory_row={"id": "m1", "content": "raw"},
        variant_row={
            "engine_id": "apollo",
            "engine_version": "0.2",
            "compressed_content": "AAPL:100@150.25/175.50:tech",
        },
    )
    resp = await get_memory(
        memory_id="m1",
        request=_request_with_accept("text/plain"),
        user=_user(),
    )
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "text/plain"
    body = resp.body.decode("utf-8")
    assert "AAPL" in body
    # Vary header set so caches don't conflate accept variants.
    assert resp.headers.get("vary", "").lower() == "accept"


@pytest.mark.asyncio
async def test_accept_text_plain_falls_back_to_raw_content_when_no_variant(monkeypatch):
    _mock_narrate_pool(
        monkeypatch,
        memory_row={"id": "m1", "content": "the raw memory body"},
        variant_row=None,
    )
    resp = await get_memory(
        memory_id="m1",
        request=_request_with_accept("text/plain"),
        user=_user(),
    )
    assert isinstance(resp, PlainTextResponse)
    assert resp.body.decode("utf-8") == "the raw memory body"


# ── Accept: application/x-apollo-dense → raw dense ─────────────────────────


@pytest.mark.asyncio
async def test_accept_dense_returns_winning_variant_verbatim(monkeypatch):
    _mock_narrate_pool(
        monkeypatch,
        memory_row={"id": "m1", "content": "raw"},
        variant_row={
            "engine_id": "apollo",
            "engine_version": "0.2",
            "compressed_content": "AAPL:100@150.25/175.50:tech",
        },
    )
    resp = await get_memory(
        memory_id="m1",
        request=_request_with_accept("application/x-apollo-dense"),
        user=_user(),
    )
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "application/x-apollo-dense"
    assert resp.body.decode("utf-8") == "AAPL:100@150.25/175.50:tech"


@pytest.mark.asyncio
async def test_accept_dense_falls_back_to_raw_when_no_variant(monkeypatch):
    _mock_narrate_pool(
        monkeypatch,
        memory_row={"id": "m1", "content": "fallback raw"},
        variant_row=None,
    )
    resp = await get_memory(
        memory_id="m1",
        request=_request_with_accept("application/x-apollo-dense"),
        user=_user(),
    )
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "application/x-apollo-dense"
    assert resp.body.decode("utf-8") == "fallback raw"


# ── 404 path: same shape across Accept values ──────────────────────────────


@pytest.mark.asyncio
async def test_accept_text_plain_404_when_memory_missing(monkeypatch):
    _mock_narrate_pool(monkeypatch, memory_row=None, variant_row=None)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_memory(
            memory_id="missing",
            request=_request_with_accept("text/plain"),
            user=_user(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_accept_dense_404_when_memory_missing(monkeypatch):
    _mock_narrate_pool(monkeypatch, memory_row=None, variant_row=None)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_memory(
            memory_id="missing",
            request=_request_with_accept("application/x-apollo-dense"),
            user=_user(),
        )
    assert exc.value.status_code == 404


# ── Default JSON path: Accept that doesn't match recognised types ──────────
#
# We verify the negotiation says "no narration" rather than running
# the full backend; the JSON branch needs a backend mock that's out
# of scope for this unit-level coverage. The negotiation function is
# already covered by test_content_negotiation.py.


@pytest.mark.asyncio
async def test_default_accept_routes_to_json_path(monkeypatch):
    """When Accept doesn't pick a recognised type, the JSON path
    runs (and fails with 503 because the backend is not mocked here
    — that's fine; the assertion is about which branch was taken)."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_memory(
            memory_id="m1",
            request=_request_with_accept("application/json"),
            user=_user(),
        )
    # 503 from _backend_or_503() proves we reached the JSON path
    # (and did NOT short-circuit through the narrate compute).
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_accept_routes_to_json_path(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_memory(
            memory_id="m1",
            request=_request_with_accept(None),
            user=_user(),
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_wildcard_accept_routes_to_json_path(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_memory(
            memory_id="m1",
            request=_request_with_accept("*/*"),
            user=_user(),
        )
    assert exc.value.status_code == 503

"""Integration tests for Accept-header content negotiation on
GET /v1/memories/{memory_id}.

The roadmap entry "Read-path routing on Accept headers" promises:

  * default / application/json / */*  → existing JSON MemoryItem
  * text/plain                         → prose narration body
  * application/x-apollo-dense         → raw winning-variant content

These tests drive the handler directly via the
``install_fake_backend`` pattern (matches ``test_namespace_
enforcement.py``). After v4.2.0a14 round-14 the variant lookup
goes through the persistence backend's compression repo, so SQLite
profiles work identically — no asyncpg-pool mock is needed.

Codex round-12 surfaced the regression that would have shipped
without this shape: a prior implementation routed text/plain / dense
through a narrower owner+namespace gate, so a memory the caller
could read as JSON (federated, world, group) would 404 under
text/plain.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.api.routes.memories import get_memory

from tests._fake_backend import install_fake_backend


def _user(role: str = "user", user_id: str = "alice", namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id=user_id, group_ids=[], role=role,
        namespace=namespace, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


def _request_with_accept(accept):
    req = MagicMock()
    headers = {} if accept is None else {"accept": accept}
    req.headers = headers
    return req


def _memory_row(memory_id: str = "m1", content: str = "raw memory body", **extra) -> dict:
    base = {
        "id": memory_id,
        "content": content,
        "category": "general",
        "subcategory": None,
        "created": None,
        "updated": None,
        "metadata": {},
        "quality_rating": None,
        "compressed_content": None,
        "verbatim_content": None,
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }
    base.update(extra)
    return base


def _install_backend(monkeypatch, *, memory_row, variant_row):
    """Wire a fake backend that returns the given memory + variant."""
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", memory_row)
    backend.compression.configure_return(
        "fetch_compressed_variant_by_memory_id", variant_row,
    )
    return backend


# ── Accept: text/plain → narrated prose ────────────────────────────────────


def test_accept_text_plain_returns_narrated_prose(monkeypatch):
    _install_backend(
        monkeypatch,
        memory_row=_memory_row(content="raw"),
        variant_row={
            "engine_id": "apollo",
            "engine_version": "0.2",
            "compressed_content": "AAPL:100@150.25/175.50:tech",
        },
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("text/plain"),
        user=_user(),
    ))
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "text/plain"
    body = resp.body.decode("utf-8")
    assert "AAPL" in body
    assert resp.headers.get("vary", "").lower() == "accept"


def test_accept_text_plain_falls_back_to_raw_content_when_no_variant(monkeypatch):
    _install_backend(
        monkeypatch,
        memory_row=_memory_row(content="the raw memory body"),
        variant_row=None,
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("text/plain"),
        user=_user(),
    ))
    assert isinstance(resp, PlainTextResponse)
    assert resp.body.decode("utf-8") == "the raw memory body"
    assert resp.headers.get("vary", "").lower() == "accept"


# ── Accept: application/x-apollo-dense → raw dense ─────────────────────────


def test_accept_dense_returns_winning_variant_verbatim(monkeypatch):
    _install_backend(
        monkeypatch,
        memory_row=_memory_row(content="raw"),
        variant_row={
            "engine_id": "apollo",
            "engine_version": "0.2",
            "compressed_content": "AAPL:100@150.25/175.50:tech",
        },
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/x-apollo-dense"),
        user=_user(),
    ))
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "application/x-apollo-dense"
    assert resp.body.decode("utf-8") == "AAPL:100@150.25/175.50:tech"
    assert resp.headers.get("vary", "").lower() == "accept"


def test_accept_dense_falls_back_to_raw_when_no_variant(monkeypatch):
    _install_backend(
        monkeypatch,
        memory_row=_memory_row(content="fallback raw"),
        variant_row=None,
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/x-apollo-dense"),
        user=_user(),
    ))
    assert isinstance(resp, PlainTextResponse)
    assert resp.media_type == "application/x-apollo-dense"
    assert resp.body.decode("utf-8") == "fallback raw"
    assert resp.headers.get("vary", "").lower() == "accept"


# ── 404 path: same shape across Accept values ──────────────────────────────


def test_accept_text_plain_404_when_memory_missing(monkeypatch):
    _install_backend(monkeypatch, memory_row=None, variant_row=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_memory(
            memory_id="missing",
            request=_request_with_accept("text/plain"),
            user=_user(),
        ))
    assert exc.value.status_code == 404


def test_accept_dense_404_when_memory_missing(monkeypatch):
    _install_backend(monkeypatch, memory_row=None, variant_row=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_memory(
            memory_id="missing",
            request=_request_with_accept("application/x-apollo-dense"),
            user=_user(),
        ))
    assert exc.value.status_code == 404


# ── Default JSON path: JSONResponse + Vary: Accept ─────────────────────────


def test_default_accept_returns_json_with_vary_accept(monkeypatch):
    _install_backend(monkeypatch, memory_row=_memory_row(), variant_row=None)
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"], "content": r["content"]},
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/json"),
        user=_user(),
    ))
    assert isinstance(resp, JSONResponse)
    assert resp.headers.get("vary", "").lower() == "accept"
    assert resp.media_type == "application/json"


def test_missing_accept_returns_json_with_vary_accept(monkeypatch):
    _install_backend(monkeypatch, memory_row=_memory_row(), variant_row=None)
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"], "content": r["content"]},
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept(None),
        user=_user(),
    ))
    assert isinstance(resp, JSONResponse)
    assert resp.headers.get("vary", "").lower() == "accept"


def test_wildcard_accept_returns_json_with_vary_accept(monkeypatch):
    _install_backend(monkeypatch, memory_row=_memory_row(), variant_row=None)
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"], "content": r["content"]},
    )

    resp = asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("*/*"),
        user=_user(),
    ))
    assert isinstance(resp, JSONResponse)
    assert resp.headers.get("vary", "").lower() == "accept"


def test_default_accept_404_when_memory_missing(monkeypatch):
    _install_backend(monkeypatch, memory_row=None, variant_row=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_memory(
            memory_id="missing",
            request=_request_with_accept("application/json"),
            user=_user(),
        ))
    assert exc.value.status_code == 404


# ── Visibility contract: same VisibilityFilter across all Accept values ────
#
# Codex round-12 specifically called out that the negotiated path
# must NOT use a narrower tenancy gate than the JSON path. These
# tests assert ``backend.memories.get_memory`` is called with the
# same VisibilityFilter regardless of Accept value — so a memory
# admitted by READABLE (federated, world, group) under JSON is also
# admitted under text/plain and dense.


def _last_get_memory_call(backend) -> dict:
    for name, kw in reversed(backend.memories.calls):
        if name == "get_memory":
            return kw
    raise AssertionError("no get_memory call captured")


def test_text_plain_uses_same_visibility_filter_as_json(monkeypatch):
    backend = _install_backend(
        monkeypatch, memory_row=_memory_row(), variant_row=None,
    )

    asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("text/plain"),
        user=_user("user", "alice", "alice-ns"),
    ))
    vis_text_plain = _last_get_memory_call(backend)["visibility"]

    backend.memories.calls.clear()
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"]},
    )
    asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/json"),
        user=_user("user", "alice", "alice-ns"),
    ))
    vis_json = _last_get_memory_call(backend)["visibility"]

    # Same scope and same namespace pin — non-root callers can read
    # via either Accept value with identical results.
    assert vis_text_plain.scope == vis_json.scope
    assert vis_text_plain.namespace == vis_json.namespace
    assert vis_text_plain.user_id == vis_json.user_id


def test_dense_uses_same_visibility_filter_as_json(monkeypatch):
    backend = _install_backend(
        monkeypatch, memory_row=_memory_row(), variant_row=None,
    )

    asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/x-apollo-dense"),
        user=_root(),
    ))
    vis_dense = _last_get_memory_call(backend)["visibility"]

    backend.memories.calls.clear()
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"]},
    )
    asyncio.run(get_memory(
        memory_id="m1",
        request=_request_with_accept("application/json"),
        user=_root(),
    ))
    vis_json = _last_get_memory_call(backend)["visibility"]

    assert vis_dense.scope == vis_json.scope
    assert vis_dense.namespace == vis_json.namespace

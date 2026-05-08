from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.routes import (
    document_import,
    entities,
    morpheus,
    sessions,
    state,
)
from mnemos.persistence.sqlite import SqliteBackend


def _user() -> UserContext:
    return UserContext(user_id="u", group_ids=[], role="root", namespace="default", authenticated=True)


@pytest.fixture
def edge_client(monkeypatch, tmp_path):
    backend = SqliteBackend(tmp_path / "edge.sqlite3", SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)

    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(entities.router)
    app.include_router(state.router)
    app.include_router(morpheus.router)
    app.include_router(document_import.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_root] = _user
    return TestClient(app)


def test_edge_profile_postgres_only_routes_return_503(edge_client):
    cases = [
        ("post", "/v1/sessions", {"json": {}}),
        ("get", "/entities", {}),
        ("get", "/v1/morpheus/runs", {}),
    ]
    for method, path, kwargs in cases:
        resp = getattr(edge_client, method)(path, **kwargs)
        assert resp.status_code == 503
        # Detail wording differs across routes — sessions now goes
        # through ``require_postgres_pool_or_503`` which produces the
        # profile-aware "requires the Postgres backend" message; the
        # other Postgres-only routes still hit the older
        # ``_require_postgres_backend`` "requires a Postgres backend"
        # message. Both are valid 503 details that make the
        # SQLite/edge-profile cause obvious to operators, so accept
        # either phrasing here.
        assert "Postgres backend" in resp.text


def test_edge_profile_state_route_uses_sqlite_backend(edge_client):
    resp = edge_client.put("/state/edge-key", json={"value": {"ok": True}})
    assert resp.status_code == 200, resp.text

    resp = edge_client.get("/state/edge-key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == '{"ok": true}'


def test_edge_profile_documents_import_returns_503_with_correct_route_label(
    edge_client,
):
    """The single-file ``POST /v1/documents/import`` route must emit a
    503 detail naming its own path on edge profiles.

    Round-56 hard-coded the wrong label ``POST /v1/import/document``
    (a path that doesn't exist — the router prefix is
    ``/v1/documents``). Codex caught this in the round-61 review.
    The fix passes a per-caller ``route_label`` into
    ``import_memories_from_document`` so each endpoint surfaces its
    real path in the 503 detail.

    This test pins the label end-to-end: a SQLite-backed deployment
    being asked to serve a Postgres-only route must respond with
    503 AND the response detail must name the canonical
    ``POST /v1/documents/import`` path operators can dig into.
    """
    resp = edge_client.post(
        "/v1/documents/import",
        files={"file": ("doc.txt", b"hello", "text/plain")},
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 503
    assert "POST /v1/documents/import" in resp.text
    # Negative: the bogus pre-fix label must NEVER appear.
    assert "/v1/import/document" not in resp.text


def test_edge_profile_documents_batch_import_returns_top_level_503(
    edge_client,
):
    """The multi-file ``POST /v1/documents/batch-import`` route must
    return a top-level 503 (NOT a 207 with per-file 503 entries) when
    the deployment can't serve the route at all.

    Pre-fix, ``import_memories_from_document`` raised the 503 from
    inside the batch's per-file ``try/except HTTPException`` and the
    catch folded it into a 207 body with ``status_code=503`` per
    entry. That hides the unsupported-route condition behind a
    success-shaped response — operators on edge profiles would see a
    207 containing per-file errors and reasonably conclude the
    documents themselves were malformed instead of recognizing the
    deployment misconfiguration.

    The fix calls ``require_postgres_pool_or_503`` ONCE before the
    per-file loop with the batch route label so the 503 escapes
    uncaught with the correct top-level status and a route-named
    detail.
    """
    resp = edge_client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("a.txt", b"hello", "text/plain")),
            ("files", ("b.txt", b"world", "text/plain")),
        ],
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 503
    assert "POST /v1/documents/batch-import" in resp.text
    # Negative: the bogus pre-fix label must NEVER appear.
    assert "/v1/import/document" not in resp.text

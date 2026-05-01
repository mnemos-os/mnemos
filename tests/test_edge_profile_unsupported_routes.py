from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.routes import entities, morpheus, sessions, state
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
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_root] = _user
    return TestClient(app)


def test_edge_profile_postgres_only_routes_return_503(edge_client):
    cases = [
        ("post", "/v1/sessions", {"json": {}}),
        ("get", "/entities", {}),
        ("get", "/state", {}),
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

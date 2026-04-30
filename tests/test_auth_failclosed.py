from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mnemos.api import dependencies
from mnemos.core import config


def _reset(monkeypatch, profile: str):
    monkeypatch.setenv("MNEMOS_PROFILE", profile)
    monkeypatch.delenv("MNEMOS_AUTH_ENABLED", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    dependencies.configure_auth(None)


def test_server_profile_defaults_auth_enabled_and_rejects_missing_credentials(monkeypatch):
    _reset(monkeypatch, "server")
    app = FastAPI()
    app.state.pool = SimpleNamespace()

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 401


def test_edge_profile_defaults_personal_mode(monkeypatch):
    _reset(monkeypatch, "edge")
    app = FastAPI()

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 200

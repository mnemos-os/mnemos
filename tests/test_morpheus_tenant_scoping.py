from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.morpheus import router
from mnemos.persistence.postgres import PostgresBackend


class _Conn:
    async def fetch(self, *_args):
        now = datetime.now(timezone.utc)
        return [{
            "id": "00000000-0000-0000-0000-000000000001",
            "started_at": now,
            "finished_at": None,
            "status": "success",
            "phase": "done",
            "triggered_by": "api",
            "window_started_at": None,
            "window_ended_at": None,
            "window_hours": 1,
            "cluster_min_size": 2,
            "memories_scanned": 0,
            "clusters_found": 0,
            "summaries_created": 0,
            "error": None,
            "config": {},
            "namespace": "other",
        }]


class _Pool:
    def acquire(self, **_kwargs):
        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _ctx(role: str) -> UserContext:
    return UserContext(user_id=role, group_ids=[], role=role, namespace="default", authenticated=True)


def _client(monkeypatch, role: str) -> TestClient:
    pool = _Pool()
    monkeypatch.setattr(lifecycle, "_pool", pool)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)
    monkeypatch.setattr(lifecycle, "_persistence_backend", PostgresBackend(pool, SimpleNamespace()))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _ctx(role)
    return TestClient(app)


def test_morpheus_reads_require_root(monkeypatch):
    assert _client(monkeypatch, "user").get("/v1/morpheus/runs").status_code == 403


def test_morpheus_reads_root_ok(monkeypatch):
    resp = _client(monkeypatch, "root").get("/v1/morpheus/runs")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

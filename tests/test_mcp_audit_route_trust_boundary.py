"""Slice #153: route-level tests for /v1/internal/mcp_audit trust boundary.

The trust-boundary dependency `_require_internal_audit_token` was
introduced in #148 (round-3 residual #1 of #146) and made default-on
by #150 (fresh-install autogen) + #151 (--upgrade autogen). Existing
tests in `test_mcp_audit_log.py` cover the writers (insert,
persist_via_pool, persist_via_http) but not the FastAPI dependency-
injection trust-boundary check itself. This file exercises the route
end-to-end with a TestClient:

  - Token unset (legacy mode): authenticated bearer is enough; 204.
  - Token set + correct X-Mnemos-Audit-Token: 204.
  - Token set + missing header: 401.
  - Token set + wrong header: 401.
  - Token set + whitespace-only header: 401.

Without these tests, a future refactor that drops or weakens the
dependency wiring (e.g. removes the `Depends(_require_internal_
audit_token)` line) would be caught only by manual inspection.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.mcp_audit import router


def _user_ctx(role: str = "user") -> UserContext:
    return UserContext(
        user_id="audit-test-user",
        group_ids=[],
        role=role,
        namespace="default",
        authenticated=True,
    )


@pytest.fixture
def patched_audit_route(monkeypatch):
    """TestClient for the audit route with the writer + pool guard
    monkey-patched out, plus a hook for setting the configured
    `internal_audit_token` per-test."""

    # Stub out the actual DB write — the trust-boundary check runs
    # before this in dependency order, but if the request lands on
    # the route body we don't want it talking to a real Postgres pool.
    captured: list[dict[str, Any]] = []

    async def _fake_insert(conn, **kwargs):
        captured.append(kwargs)
        return None

    import mnemos.api.routes.mcp_audit as audit_route

    monkeypatch.setattr(audit_route, "insert_audit_record", _fake_insert)
    monkeypatch.setattr(
        audit_route, "require_postgres_pool_or_503", lambda **_kw: None
    )

    # Fake pool manager + acquire context so the `async with
    # _lc.get_pool_manager().acquire() as conn` path returns
    # without touching real DB.
    class _FakeConn:
        async def execute(self, *_a, **_kw):
            return None

    class _AcquireCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_exc):
            return False

    class _FakePoolManager:
        def acquire(self):
            return _AcquireCtx()

    import mnemos.core.lifecycle as _lc

    monkeypatch.setattr(_lc, "get_pool_manager", lambda: _FakePoolManager())

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _user_ctx("user")

    def _set_token(value: str | None):
        """Override what _require_internal_audit_token sees as the
        configured token. We can't reach into the singleton cleanly,
        so we patch the get_settings function to return a stub."""
        configured = SimpleNamespace(
            server=SimpleNamespace(internal_audit_token=value or "")
        )
        monkeypatch.setattr(audit_route, "get_settings", lambda: configured)

    return SimpleNamespace(
        app=app,
        captured=captured,
        set_token=_set_token,
    )


def _valid_body() -> dict:
    return {
        "tool": "list_memories",
        "parameter_shape": {
            "limit": {"type": "int"},
            "category": {"type": "str", "length": 7},
        },
        "outcome": "success",
        "error_class": None,
    }


def test_legacy_mode_accepts_bearer_only(patched_audit_route):
    """Token unset: authenticated bearer is enough; no audit-token
    header needed."""
    patched_audit_route.set_token(None)
    with TestClient(patched_audit_route.app) as client:
        r = client.post("/v1/internal/mcp_audit", json=_valid_body())
    assert r.status_code == 204, r.text
    assert len(patched_audit_route.captured) == 1


def test_legacy_mode_accepts_bearer_with_garbage_header(patched_audit_route):
    """Legacy mode ignores the audit-token header completely (it's
    only consulted when the configured token is non-empty)."""
    patched_audit_route.set_token("")
    with TestClient(patched_audit_route.app) as client:
        r = client.post(
            "/v1/internal/mcp_audit",
            json=_valid_body(),
            headers={"X-Mnemos-Audit-Token": "this-should-be-ignored"},
        )
    assert r.status_code == 204, r.text


def test_locked_down_correct_token_accepts(patched_audit_route):
    """Token set + matching X-Mnemos-Audit-Token: 204."""
    expected = "a" * 64
    patched_audit_route.set_token(expected)
    with TestClient(patched_audit_route.app) as client:
        r = client.post(
            "/v1/internal/mcp_audit",
            json=_valid_body(),
            headers={"X-Mnemos-Audit-Token": expected},
        )
    assert r.status_code == 204, r.text
    assert len(patched_audit_route.captured) == 1


def test_locked_down_missing_token_rejected(patched_audit_route):
    """Token set + no X-Mnemos-Audit-Token header: 401."""
    patched_audit_route.set_token("a" * 64)
    with TestClient(patched_audit_route.app) as client:
        r = client.post("/v1/internal/mcp_audit", json=_valid_body())
    assert r.status_code == 401, r.text
    assert "X-Mnemos-Audit-Token" in r.text
    # Writer must NOT have been called.
    assert patched_audit_route.captured == []


def test_locked_down_wrong_token_rejected(patched_audit_route):
    """Token set + WRONG X-Mnemos-Audit-Token: 401."""
    patched_audit_route.set_token("a" * 64)
    with TestClient(patched_audit_route.app) as client:
        r = client.post(
            "/v1/internal/mcp_audit",
            json=_valid_body(),
            headers={"X-Mnemos-Audit-Token": "b" * 64},
        )
    assert r.status_code == 401, r.text
    assert patched_audit_route.captured == []


def test_locked_down_whitespace_only_token_rejected(patched_audit_route):
    """Token set + whitespace-only X-Mnemos-Audit-Token: 401.
    Defends against headers that strip-trim to empty (e.g. a misset
    template value)."""
    patched_audit_route.set_token("a" * 64)
    with TestClient(patched_audit_route.app) as client:
        r = client.post(
            "/v1/internal/mcp_audit",
            json=_valid_body(),
            headers={"X-Mnemos-Audit-Token": "   \t   "},
        )
    assert r.status_code == 401, r.text
    assert patched_audit_route.captured == []


def test_locked_down_constant_time_compare_does_not_partial_accept(
    patched_audit_route,
):
    """A header that's a prefix of the configured token must be rejected
    — defends against a future refactor that drops `hmac.compare_digest`
    in favor of `==`."""
    expected = "a" * 64
    patched_audit_route.set_token(expected)
    with TestClient(patched_audit_route.app) as client:
        r = client.post(
            "/v1/internal/mcp_audit",
            json=_valid_body(),
            headers={"X-Mnemos-Audit-Token": "a" * 32},
        )
    assert r.status_code == 401, r.text


def test_route_level_dependency_wiring_present():
    """Source-level guard: the route must include
    `Depends(_require_internal_audit_token)` in its signature.
    Without this, the trust-boundary check is silently bypassed."""
    import inspect

    from mnemos.api.routes import mcp_audit as audit_route

    source = inspect.getsource(audit_route.write_mcp_audit_record)
    assert "_require_internal_audit_token" in source, (
        "write_mcp_audit_record must depend on "
        "_require_internal_audit_token; without it any authenticated "
        "caller can POST to /v1/internal/mcp_audit even when the "
        "token is configured"
    )
    assert "Depends" in source, (
        "expected `Depends(_require_internal_audit_token)` in route signature"
    )

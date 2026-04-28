"""Webhooks + entities Tier 3 namespace enforcement (v3.2).

After per-user namespaces landed in 2aa41ea, webhooks + entities
were the last handlers still scoping by owner_id only. Codex audit
019dbd11 flagged this as "latent design drift rather than an
immediate exploit" under the old single-namespace auth. With
multi-namespace auth live, it becomes real. These tests pin the
two-dim gate:

  * webhooks: create with cross-namespace request → 403 for non-root.
    list / get / revoke / deliveries all filter by owner_id AND
    namespace for non-root; root bypasses both.
  * entities: INSERT stamps namespace from caller; list / get /
    patch paths filter owner_id AND namespace. Root-only overrides
    via ?owner_id= + ?namespace= query params.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest

from mnemos.api.dependencies import UserContext


def _alice(ns: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=ns, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, *, rows=None, row=None):
        self._rows = rows or []
        self._row = row
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self._row

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _install_public_dns(monkeypatch, wh):
    async def _fake_resolve_addrs(host: str):
        return ["93.184.216.34"]

    monkeypatch.setattr(wh, "_resolve_addrs", _fake_resolve_addrs)


# ─── entities ────────────────────────────────────────────────────────────────


def test_entities_create_stamps_caller_namespace(monkeypatch):
    from mnemos.api.routes import entities as ent

    row = {
        "id": str(uuid.uuid4()), "entity_type": "person", "name": "alice",
        "description": None, "metadata": {},
        "created": "2026-04-24T00:00:00", "updated": "2026-04-24T00:00:00",
    }
    conn = _Conn(row=row)
    _install(monkeypatch, conn)

    req = ent.EntityCreateRequest(entity_type="person", name="alice")
    asyncio.run(ent.create_entity(req, user=_alice("alice-ns")))

    # INSERT SQL + args: namespace column present, caller's namespace
    # passed positionally
    sql, args = conn.fetchrow_calls[-1]
    assert "namespace" in sql
    assert "alice-ns" in args


def test_entities_list_filters_by_owner_and_namespace(monkeypatch):
    from mnemos.api.routes import entities as ent

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(ent.list_entities(
        entity_type=None, search=None, limit=50,
        user=_alice("alice-ns"), owner_id=None, namespace=None,
    ))

    sql, args = conn.fetch_calls[-1]
    assert "owner_id=$" in sql
    assert "namespace=$" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_entities_list_rejects_cross_namespace_for_non_root(monkeypatch):
    from mnemos.api.routes import entities as ent

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ent.list_entities(
            entity_type=None, search=None, limit=50,
            user=_alice("alice-ns"),
            owner_id=None, namespace="other-ns",
        ))
    assert exc.value.status_code == 403


def test_entities_list_root_may_target_any_namespace(monkeypatch):
    from mnemos.api.routes import entities as ent

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(ent.list_entities(
        entity_type=None, search=None, limit=50,
        user=_root(),
        owner_id="bob", namespace="bob-ns",
    ))
    _, args = conn.fetch_calls[-1]
    assert "bob" in args
    assert "bob-ns" in args


def test_entities_assert_owned_requires_matching_namespace(monkeypatch):
    """assert_owned_context (used by get/patch/link) must check BOTH owner
    and namespace for non-root. Cross-namespace access returns 404."""
    from mnemos.core.security import assert_owned_context

    conn = _Conn(row={"owner_id": "alice", "namespace": "other-ns"})

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(assert_owned_context(conn, "entities", str(uuid.uuid4()), _alice("alice-ns")))
    assert exc.value.status_code == 404


def test_entities_assert_owned_root_bypasses_namespace(monkeypatch):
    from mnemos.core.security import assert_owned_context

    conn = _Conn(row={"owner_id": "bob", "namespace": "bob-ns"})
    # Root call — should NOT raise
    result = asyncio.run(assert_owned_context(conn, "entities", str(uuid.uuid4()), _root()))
    assert (result.owner, result.namespace) == ("bob", "bob-ns")


# ─── webhooks ────────────────────────────────────────────────────────────────


def test_webhook_list_filters_by_owner_and_namespace(monkeypatch):
    from mnemos.api.routes import webhooks as wh

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(wh.list_webhooks(user=_alice("alice-ns"), include_revoked=False))

    sql, args = conn.fetch_calls[-1]
    assert "owner_id = $" in sql
    assert "namespace = $" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_webhook_list_root_sees_all_without_filter(monkeypatch):
    from mnemos.api.routes import webhooks as wh

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(wh.list_webhooks(user=_root(), include_revoked=False))

    sql, _ = conn.fetch_calls[-1]
    # Root path: no owner/namespace filter
    assert "owner_id = $" not in sql
    assert "namespace = $" not in sql


def test_webhook_get_filters_by_owner_and_namespace(monkeypatch):
    from mnemos.api.routes import webhooks as wh

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wh.get_webhook(
            str(uuid.uuid4()), user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 404
    sql, args = conn.fetchrow_calls[-1]
    assert "owner_id = $" in sql
    assert "namespace = $" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_webhook_revoke_filters_by_owner_and_namespace(monkeypatch):
    from mnemos.api.routes import webhooks as wh

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wh.revoke_webhook(
            str(uuid.uuid4()), user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 404
    sql, args = conn.fetchrow_calls[-1]
    assert "owner_id = $" in sql
    assert "namespace = $" in sql
    assert "alice-ns" in args


def test_webhook_create_rejects_cross_namespace_for_non_root(monkeypatch):
    """Pre-v3.2 a non-root user could pass request.namespace to create
    a webhook in another namespace. v3.2 closes this: 403."""
    from mnemos.api.routes import webhooks as wh

    conn = _Conn(row=None)
    _install(monkeypatch, conn)
    _install_public_dns(monkeypatch, wh)

    req = wh.WebhookCreateRequest(
        url="https://example.com/hook",
        events=["memory.created"],
        description=None,
        namespace="other-ns",
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wh.create_webhook(req, user=_alice("alice-ns")))
    assert exc.value.status_code == 403


def test_webhook_create_own_namespace_succeeds_for_non_root(monkeypatch):
    """Passing request.namespace that equals user.namespace is fine —
    only mismatched namespaces are rejected."""
    from mnemos.api.routes import webhooks as wh

    ok_row = {
        "id": uuid.uuid4(),
        "url": "https://example.com/hook",
        "events": ["memory.created"],
        "description": None,
        "owner_id": "alice",
        "namespace": "alice-ns",
        "created": __import__("datetime").datetime(2026, 4, 24),
        "revoked": False,
    }
    conn = _Conn(row=ok_row)
    _install(monkeypatch, conn)
    _install_public_dns(monkeypatch, wh)

    req = wh.WebhookCreateRequest(
        url="https://example.com/hook",
        events=["memory.created"],
        description=None,
        namespace="alice-ns",  # same as caller's
    )

    resp = asyncio.run(wh.create_webhook(req, user=_alice("alice-ns")))
    assert resp.namespace == "alice-ns"

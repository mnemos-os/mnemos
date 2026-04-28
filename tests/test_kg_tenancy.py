"""KG-triple tenancy contract tests (v3.1.2).

Pure unit tests against the mnemos.api.routes.kg module with a mock
connection that records SQL + args. Verifies:

  * create_triple stamps owner_id + namespace from UserContext
  * list/timeline filter by owner_id for non-root callers
  * root callers skip the owner filter
  * update/delete return 404 for non-owners (not 403 — existence
    is invisible per the read contract)
  * memory_id cross-ownership rejection on create_triple

No TestClient / FastAPI router wiring — handlers are called
directly with a mocked pool, which stays fast and avoids coupling
the test to request/response serialization details.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import kg as kg_handlers
from mnemos.domain.models import KGTripleCreate, KGTripleUpdate


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


def _bob() -> UserContext:
    return UserContext(
        user_id="bob",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


class _RecorderConn:
    """Mock asyncpg Connection. Records every SQL + args.

    fetch/fetchrow/fetchval callbacks dispatch on SQL substrings to
    return canned data; tests assert on the recorded calls.
    """

    def __init__(self, *, triples=None, memories=None):
        # id -> row dict
        self._triples = triples or {}
        self._memories = memories or {}
        self.executes: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO kg_triples"):
            triple_id = args[0]
            self._triples[triple_id] = {
                "id": triple_id, "subject": args[1], "predicate": args[2],
                "object": args[3], "subject_type": args[4], "object_type": args[5],
                "valid_from": args[6], "valid_until": args[7], "memory_id": args[8],
                "confidence": args[9], "owner_id": args[10], "namespace": args[11],
                "created": None,
            }
            return "INSERT 0 1"
        if sql.lstrip().startswith("UPDATE kg_triples"):
            return "UPDATE 1"
        if sql.lstrip().startswith("DELETE FROM kg_triples"):
            triple_id = args[0]
            if triple_id in self._triples:
                del self._triples[triple_id]
                return "DELETE 1"
            return "DELETE 0"
        return "OK"

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        if "FROM kg_triples" in sql:
            rows = list(self._triples.values())
            # crude WHERE owner_id=$N filter — match against any args[]
            if "owner_id=$" in sql or "owner_id = $" in sql:
                rows = [r for r in rows if r.get("owner_id") in args]
            return rows
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetchrows.append((sql, args))
        # update/delete precheck: SELECT owner_id, namespace FROM kg_triples
        if sql.strip().startswith("SELECT owner_id, namespace FROM kg_triples"):
            t = self._triples.get(args[0])
            if t is None:
                return None
            return {"owner_id": t.get("owner_id"), "namespace": t.get("namespace", "default")}
        # create_triple's cross-tenant memory check
        if sql.strip().startswith("SELECT owner_id, namespace FROM memories"):
            m = self._memories.get(args[0])
            if m is None:
                return None
            return {"owner_id": m.get("owner_id"), "namespace": m.get("namespace", "default")}
        if "FROM kg_triples WHERE id=$1" in sql:
            triple_id = args[0]
            t = self._triples.get(triple_id)
            if t is None:
                return None
            from datetime import datetime
            row = dict(t)
            row["valid_from"] = row.get("valid_from") or datetime(2026, 1, 1)
            row["created"] = row.get("created") or datetime(2026, 1, 1)
            return row
        return None

    async def fetchval(self, sql: str, *args):
        self.fetchvals.append((sql, args))
        if "SELECT 1 FROM memories WHERE id=$1" in sql:
            return 1 if args[0] in self._memories else None
        if "COUNT(*) FROM kg_triples" in sql:
            rows = list(self._triples.values())
            if "owner_id=$" in sql or "owner_id = $" in sql:
                rows = [r for r in rows if r.get("owner_id") in args]
            return len(rows)
        return None


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _install_pool(monkeypatch, conn):
    """Wire a fake pool.acquire() that yields `conn` into mnemos.core.lifecycle."""
    import mnemos.core.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


# ---- create_triple ---------------------------------------------------------


def test_create_triple_stamps_owner_id_and_namespace(monkeypatch):
    conn = _RecorderConn()
    _install_pool(monkeypatch, conn)
    req = KGTripleCreate(subject="Jason", predicate="works_at", object="MNEMOS")

    asyncio.run(kg_handlers.create_triple(req, user=_alice()))

    insert = next(e for e in conn.executes if "INSERT INTO kg_triples" in e[0])
    args = insert[1]
    # Positional args end with owner_id, namespace per the SQL template
    assert args[-2] == "alice"
    assert args[-1] == "default"


def test_create_triple_rejects_cross_owner_memory_id(monkeypatch):
    """Alice can't attach a triple to Bob's memory."""
    conn = _RecorderConn(memories={
        "mem_bob": {"id": "mem_bob", "owner_id": "bob", "namespace": "default"},
    })
    _install_pool(monkeypatch, conn)
    req = KGTripleCreate(
        subject="x", predicate="y", object="z", memory_id="mem_bob",
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(kg_handlers.create_triple(req, user=_alice()))
    assert exc.value.status_code == 404
    # No INSERT occurred
    assert not any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


def test_create_triple_rejects_cross_namespace_memory_id(monkeypatch):
    """Same owner, different namespace — still rejected."""
    conn = _RecorderConn(memories={
        "mem_x": {"id": "mem_x", "owner_id": "alice", "namespace": "other-ns"},
    })
    _install_pool(monkeypatch, conn)
    req = KGTripleCreate(
        subject="x", predicate="y", object="z", memory_id="mem_x",
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(kg_handlers.create_triple(req, user=_alice()))
    assert exc.value.status_code == 404
    assert not any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


def test_create_triple_allows_own_memory_id(monkeypatch):
    conn = _RecorderConn(
        memories={"mem_alice": {"id": "mem_alice", "owner_id": "alice", "namespace": "default"}},
    )
    _install_pool(monkeypatch, conn)
    req = KGTripleCreate(
        subject="x", predicate="y", object="z", memory_id="mem_alice",
    )

    asyncio.run(kg_handlers.create_triple(req, user=_alice()))
    assert any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


def test_create_triple_root_can_reference_any_memory(monkeypatch):
    conn = _RecorderConn(
        memories={"mem_alice": {"id": "mem_alice", "owner_id": "alice", "namespace": "default"}},
    )
    _install_pool(monkeypatch, conn)
    req = KGTripleCreate(
        subject="x", predicate="y", object="z", memory_id="mem_alice",
    )

    asyncio.run(kg_handlers.create_triple(req, user=_root()))
    assert any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


# ---- list_triples / get_timeline filtering --------------------------------


def test_list_triples_filters_by_owner_and_namespace_for_non_root(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_alice1": {"id": "kg_alice1", "owner_id": "alice", "subject": "A",
                      "predicate": "p", "object": "o", "confidence": 1.0,
                      "valid_from": None, "created": None},
        "kg_bob1":   {"id": "kg_bob1",   "owner_id": "bob",   "subject": "B",
                      "predicate": "p", "object": "o", "confidence": 1.0,
                      "valid_from": None, "created": None},
    })
    _install_pool(monkeypatch, conn)

    asyncio.run(kg_handlers.list_triples(user=_alice()))

    # At least one fetch must filter on BOTH owner_id and namespace
    scoped_fetches = [
        f for f in conn.fetches
        if "owner_id=$" in f[0] and "namespace=$" in f[0]
    ]
    assert scoped_fetches, "expected owner_id + namespace filter on list_triples"
    # And the args include the caller's user_id AND namespace
    args = scoped_fetches[0][1]
    assert "alice" in args
    assert "default" in args


def test_list_triples_no_owner_filter_for_root(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_1": {"id": "kg_1", "owner_id": "alice", "subject": "A",
                 "predicate": "p", "object": "o", "confidence": 1.0,
                 "valid_from": None, "created": None},
    })
    _install_pool(monkeypatch, conn)

    asyncio.run(kg_handlers.list_triples(user=_root()))

    # None of the SELECTs should include an owner_id clause
    assert not any("owner_id=$" in f[0] or "owner_id = $" in f[0] for f in conn.fetches)


def test_timeline_filters_by_owner_and_namespace_for_non_root(monkeypatch):
    conn = _RecorderConn()
    _install_pool(monkeypatch, conn)

    asyncio.run(kg_handlers.get_timeline("subject-x", user=_alice()))

    tl_fetch = conn.fetches[-1]
    assert "owner_id=$" in tl_fetch[0]
    assert "namespace=$" in tl_fetch[0]
    assert "alice" in tl_fetch[1]
    assert "default" in tl_fetch[1]


def test_timeline_no_owner_filter_for_root(monkeypatch):
    conn = _RecorderConn()
    _install_pool(monkeypatch, conn)

    asyncio.run(kg_handlers.get_timeline("subject-x", user=_root()))

    tl_fetch = conn.fetches[-1]
    assert "owner_id" not in tl_fetch[0]


# ---- update_triple / delete_triple ownership checks -----------------------


def test_update_triple_returns_404_for_non_owner(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_bob1": {"id": "kg_bob1", "owner_id": "bob"},
    })
    _install_pool(monkeypatch, conn)
    req = KGTripleUpdate(subject="new")

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(kg_handlers.update_triple("kg_bob1", req, user=_alice()))
    assert exc.value.status_code == 404
    # No UPDATE fired
    assert not any("UPDATE kg_triples" in e[0] for e in conn.executes)


def test_update_triple_succeeds_for_owner(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_a1": {
            "id": "kg_a1", "owner_id": "alice", "subject": "old",
            "predicate": "p", "object": "o", "subject_type": None,
            "object_type": None, "valid_from": None, "valid_until": None,
            "memory_id": None, "confidence": 1.0, "created": None,
        },
    })
    _install_pool(monkeypatch, conn)
    req = KGTripleUpdate(subject="new")

    asyncio.run(kg_handlers.update_triple("kg_a1", req, user=_alice()))
    assert any("UPDATE kg_triples" in e[0] for e in conn.executes)


def test_update_triple_root_can_modify_any_row(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_bob1": {
            "id": "kg_bob1", "owner_id": "bob", "subject": "x",
            "predicate": "p", "object": "o", "subject_type": None,
            "object_type": None, "valid_from": None, "valid_until": None,
            "memory_id": None, "confidence": 1.0, "created": None,
        },
    })
    _install_pool(monkeypatch, conn)
    req = KGTripleUpdate(subject="new")

    asyncio.run(kg_handlers.update_triple("kg_bob1", req, user=_root()))
    assert any("UPDATE kg_triples" in e[0] for e in conn.executes)


def test_delete_triple_returns_404_for_non_owner(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_bob1": {"id": "kg_bob1", "owner_id": "bob"},
    })
    _install_pool(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(kg_handlers.delete_triple("kg_bob1", user=_alice()))
    assert exc.value.status_code == 404
    assert not any("DELETE FROM kg_triples" in e[0] for e in conn.executes)


def test_delete_triple_succeeds_for_owner(monkeypatch):
    conn = _RecorderConn(triples={
        "kg_a1": {"id": "kg_a1", "owner_id": "alice"},
    })
    _install_pool(monkeypatch, conn)

    asyncio.run(kg_handlers.delete_triple("kg_a1", user=_alice()))
    assert any("DELETE FROM kg_triples" in e[0] for e in conn.executes)

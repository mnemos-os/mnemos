"""Cross-namespace isolation for state, journal, and entities.

These tests use real bearer API keys against a live Postgres database so they
exercise auth-time namespace resolution plus handler SQL predicates together.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PG_URL = os.getenv("MNEMOS_TEST_DB")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="set MNEMOS_TEST_DB=postgres://... to run integration tests"),
    pytest.mark.asyncio,
]


@dataclass(frozen=True)
class Tenant:
    user_id: str
    namespace: str
    api_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass(frozen=True)
class NamespaceHarness:
    client: AsyncClient
    pool: Any
    prefix: str
    alice: Tenant
    bob: Tenant


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _headers(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def _decode_jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _seed_api_key(conn, *, user_id: str, namespace: str, raw_key: str, role: str = "user") -> None:
    await conn.execute(
        """
        INSERT INTO users (id, display_name, role, namespace)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (id) DO UPDATE
        SET role = EXCLUDED.role,
            namespace = EXCLUDED.namespace,
            updated_at = NOW()
        """,
        user_id,
        user_id,
        role,
        namespace,
    )
    await conn.execute(
        """
        INSERT INTO api_keys (user_id, key_hash, key_prefix, label, revoked)
        VALUES ($1, $2, $3, $4, FALSE)
        ON CONFLICT (key_hash) DO UPDATE
        SET user_id = EXCLUDED.user_id,
            label = EXCLUDED.label,
            revoked = FALSE
        """,
        user_id,
        _hash_key(raw_key),
        raw_key[:12],
        f"namespace-isolation-{user_id}",
    )


@pytest_asyncio.fixture
async def pg_app(monkeypatch):
    import asyncpg

    import mnemos.core.lifecycle as lc
    from mnemos.api.dependencies import configure_auth
    from mnemos.api.main import app
    from mnemos.persistence.postgres import PostgresBackend

    assert PG_URL is not None
    prefix = f"nsiso{uuid.uuid4().hex[:12]}"
    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=4)
    background_tasks: list[asyncio.Task] = []

    def _schedule_background(coro):
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    monkeypatch.setattr(lc, "_pool", pool)
    monkeypatch.setattr(lc, "_persistence_backend", PostgresBackend(pool, object()))
    monkeypatch.setattr(lc, "_cache", None)
    monkeypatch.setattr(lc, "_schedule_background", _schedule_background)
    app.state.pool = pool
    configure_auth({"enabled": True, "default_namespace": "default", "personal_user_id": "default"})

    alice = Tenant(
        user_id=f"{prefix}alice",
        namespace=f"{prefix}nsA",
        api_key=f"{prefix}-alice-{uuid.uuid4().hex}",
    )
    bob = Tenant(
        user_id=f"{prefix}bob",
        namespace=f"{prefix}nsB",
        api_key=f"{prefix}-bob-{uuid.uuid4().hex}",
    )

    async with pool.acquire() as conn:
        await _seed_api_key(conn, user_id=alice.user_id, namespace=alice.namespace, raw_key=alice.api_key)
        await _seed_api_key(conn, user_id=bob.user_id, namespace=bob.namespace, raw_key=bob.api_key)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield NamespaceHarness(client=client, pool=pool, prefix=prefix, alice=alice, bob=bob)
    finally:
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM entities WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM state WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM journal WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM api_keys WHERE label LIKE $1", f"namespace-isolation-{prefix}%")
            await conn.execute("DELETE FROM users WHERE id LIKE $1", f"{prefix}%")
        configure_auth({"enabled": False})
        await pool.close()


async def test_state_namespace_isolation(pg_app: NamespaceHarness):
    key_a = f"{pg_app.prefix}-state-a"
    key_b = f"{pg_app.prefix}-state-b"

    resp = await pg_app.client.put(
        f"/state/{key_a}",
        json={"value": {"tenant": "a"}},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await pg_app.client.put(
        f"/state/{key_b}",
        json={"value": {"tenant": "b"}},
        headers=pg_app.bob.headers,
    )
    assert resp.status_code == 200, resp.text

    resp = await pg_app.client.get(f"/state/{key_b}", headers=pg_app.alice.headers)
    assert resp.status_code == 404

    resp = await pg_app.client.get("/state", headers=pg_app.alice.headers)
    assert resp.status_code == 200, resp.text
    visible = {item["key"] for item in resp.json()["keys"]}
    assert key_a in visible
    assert key_b not in visible

    resp = await pg_app.client.put(
        f"/state/{key_b}?namespace={pg_app.bob.namespace}",
        json={"value": {"tenant": "a-overwrite"}},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 403

    resp = await pg_app.client.delete(f"/state/{key_b}", headers=pg_app.alice.headers)
    assert resp.status_code == 404

    resp = await pg_app.client.get(f"/state/{key_b}", headers=pg_app.bob.headers)
    assert resp.status_code == 200, resp.text
    assert _decode_jsonb(resp.json()["value"]) == {"tenant": "b"}


async def test_journal_namespace_isolation(pg_app: NamespaceHarness):
    topic_a = f"{pg_app.prefix}-journal-a"
    topic_b = f"{pg_app.prefix}-journal-b"

    resp = await pg_app.client.post(
        "/journal",
        json={"topic": topic_a, "content": "visible only in namespace A"},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 201, resp.text
    entry_a = resp.json()["id"]

    resp = await pg_app.client.post(
        "/journal",
        json={"topic": topic_b, "content": "visible only in namespace B"},
        headers=pg_app.bob.headers,
    )
    assert resp.status_code == 201, resp.text
    entry_b = resp.json()["id"]

    resp = await pg_app.client.get("/journal", headers=pg_app.alice.headers)
    assert resp.status_code == 200, resp.text
    visible = {item["id"] for item in resp.json()["entries"]}
    assert entry_a in visible
    assert entry_b not in visible

    resp = await pg_app.client.post(
        f"/journal?namespace={pg_app.bob.namespace}",
        json={"topic": topic_b, "content": "cross-namespace write attempt"},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 403

    resp = await pg_app.client.delete(f"/journal/{entry_b}", headers=pg_app.alice.headers)
    assert resp.status_code == 404

    resp = await pg_app.client.get("/journal", headers=pg_app.bob.headers)
    assert resp.status_code == 200, resp.text
    visible = {item["id"] for item in resp.json()["entries"]}
    assert entry_b in visible


async def test_entities_namespace_isolation_and_related_lookup(pg_app: NamespaceHarness):
    alice_name = f"{pg_app.prefix}-alice-entity"
    bob_name = f"{pg_app.prefix}-bob-entity"

    resp = await pg_app.client.post(
        "/entities",
        json={"entity_type": "person", "name": alice_name},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 201, resp.text
    alice_entity_id = resp.json()["id"]

    resp = await pg_app.client.post(
        "/entities",
        json={"entity_type": "person", "name": bob_name},
        headers=pg_app.bob.headers,
    )
    assert resp.status_code == 201, resp.text
    bob_entity_id = resp.json()["id"]

    resp = await pg_app.client.get(f"/entities?search={bob_name}", headers=pg_app.alice.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["entities"] == []

    resp = await pg_app.client.get(f"/entities/{bob_entity_id}", headers=pg_app.alice.headers)
    assert resp.status_code == 404

    resp = await pg_app.client.patch(
        f"/entities/{bob_entity_id}",
        json={"description": "alice should not update this"},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 404

    resp = await pg_app.client.post(
        f"/entities/{alice_entity_id}/link",
        json={"related_id": bob_entity_id},
        headers=pg_app.alice.headers,
    )
    assert resp.status_code == 404

    async with pg_app.pool.acquire() as conn:
        await conn.execute(
            "UPDATE entities SET related_entities = ARRAY[$2::uuid] WHERE id = $1::uuid",
            alice_entity_id,
            bob_entity_id,
        )

    resp = await pg_app.client.get(f"/entities/{alice_entity_id}/related", headers=pg_app.alice.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["related"] == []

    resp = await pg_app.client.delete(f"/entities/{bob_entity_id}", headers=pg_app.alice.headers)
    assert resp.status_code == 404

    resp = await pg_app.client.get(f"/entities/{bob_entity_id}", headers=pg_app.bob.headers)
    assert resp.status_code == 200, resp.text


async def test_same_owner_can_create_same_entity_name_in_two_namespaces(pg_app: NamespaceHarness):
    owner_id = f"{pg_app.prefix}sameowner"
    namespace_a = f"{pg_app.prefix}ownerA"
    namespace_b = f"{pg_app.prefix}ownerB"
    key_a = f"{pg_app.prefix}-same-owner-a-{uuid.uuid4().hex}"
    key_b = f"{pg_app.prefix}-same-owner-b-{uuid.uuid4().hex}"
    entity_name = f"{pg_app.prefix}-Alice"

    async with pg_app.pool.acquire() as conn:
        await _seed_api_key(conn, user_id=owner_id, namespace=namespace_a, raw_key=key_a)
        await _seed_api_key(conn, user_id=owner_id, namespace=namespace_a, raw_key=key_b)

    resp = await pg_app.client.post(
        "/entities",
        json={"entity_type": "person", "name": entity_name},
        headers=_headers(key_a),
    )
    assert resp.status_code == 201, resp.text
    first_id = resp.json()["id"]

    async with pg_app.pool.acquire() as conn:
        await conn.execute("UPDATE users SET namespace = $1 WHERE id = $2", namespace_b, owner_id)

    resp = await pg_app.client.post(
        "/entities",
        json={"entity_type": "person", "name": entity_name},
        headers=_headers(key_b),
    )
    assert resp.status_code == 201, resp.text
    second_id = resp.json()["id"]
    assert second_id != first_id

    async with pg_app.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT namespace
            FROM entities
            WHERE owner_id = $1 AND entity_type = 'person' AND name = $2
            ORDER BY namespace
            """,
            owner_id,
            entity_name,
        )

    assert [row["namespace"] for row in rows] == [namespace_a, namespace_b]

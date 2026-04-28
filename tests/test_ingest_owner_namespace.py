from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_namespace_isolation import PG_URL, NamespaceHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="set MNEMOS_TEST_DB=postgres://... to run integration tests"),
    pytest.mark.asyncio,
]


async def _cleanup(pg_app: NamespaceHarness) -> None:
    async with pg_app.pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM webhook_deliveries d
            USING webhook_subscriptions s
            WHERE d.subscription_id = s.id
              AND s.owner_id LIKE $1
            """,
            f"{pg_app.prefix}%",
        )
        await conn.execute("DELETE FROM webhook_subscriptions WHERE owner_id LIKE $1", f"{pg_app.prefix}%")
        await conn.execute(
            "DELETE FROM memory_branches WHERE memory_id IN "
            "(SELECT id FROM memories WHERE owner_id LIKE $1)",
            f"{pg_app.prefix}%",
        )
        await conn.execute("DELETE FROM memory_versions WHERE owner_id LIKE $1", f"{pg_app.prefix}%")
        await conn.execute("DELETE FROM memories WHERE owner_id LIKE $1", f"{pg_app.prefix}%")


async def _subscribe(conn, *, owner_id: str, namespace: str) -> None:
    await conn.execute(
        """
        INSERT INTO webhook_subscriptions (url, events, secret, owner_id, namespace)
        VALUES ('https://example.test/hook', ARRAY['memory.created']::text[], 'secret', $1, $2)
        """,
        owner_id,
        namespace,
    )


async def test_ingest_session_stamps_owner_namespace_and_emits_webhooks(pg_app: NamespaceHarness, monkeypatch):
    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(lc, "_schedule_delivery_attempt", lambda coro: coro.close())

    try:
        async with pg_app.pool.acquire() as conn:
            await _subscribe(conn, owner_id=pg_app.alice.user_id, namespace=pg_app.alice.namespace)

        resp = await pg_app.client.post(
            "/ingest/session",
            headers=pg_app.alice.headers,
            json={
                "session_id": f"{pg_app.prefix}-alice-session",
                "source": "claude-code",
                "agent_id": "agent-a",
                "raw_data": {"messages": [{"role": "user", "content": "alice-only memory"}]},
            },
        )
        assert resp.status_code == 200, resp.text
        alice_memory_id = resp.json()["memory_ids"][0]

        resp = await pg_app.client.post(
            "/ingest/session",
            headers=pg_app.bob.headers,
            json={
                "session_id": f"{pg_app.prefix}-bob-session",
                "source": "claude-code",
                "agent_id": "agent-b",
                "raw_data": {"code_blocks": [{"text": "bob-only memory"}]},
            },
        )
        assert resp.status_code == 200, resp.text
        bob_memory_id = resp.json()["memory_ids"][0]

        resp = await pg_app.client.get("/v1/memories?category=session_activity", headers=pg_app.alice.headers)
        assert resp.status_code == 200, resp.text
        assert {m["id"] for m in resp.json()["memories"]} == {alice_memory_id}

        resp = await pg_app.client.get("/v1/memories?category=session_code", headers=pg_app.alice.headers)
        assert resp.status_code == 200, resp.text
        assert bob_memory_id not in {m["id"] for m in resp.json()["memories"]}

        resp = await pg_app.client.get("/v1/memories?category=session_activity", headers=pg_app.bob.headers)
        assert resp.status_code == 200, resp.text
        assert alice_memory_id not in {m["id"] for m in resp.json()["memories"]}

        resp = await pg_app.client.get("/v1/memories?category=session_code", headers=pg_app.bob.headers)
        assert resp.status_code == 200, resp.text
        assert {m["id"] for m in resp.json()["memories"]} == {bob_memory_id}

        async with pg_app.pool.acquire() as conn:
            memory = await conn.fetchrow(
                """
                SELECT owner_id, namespace, permission_mode, verbatim_content,
                       source_provider, source_session, source_agent
                FROM memories
                WHERE id = $1
                """,
                alice_memory_id,
            )
            deliveries = await conn.fetch(
                """
                SELECT d.event_type, d.payload
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                WHERE s.owner_id = $1 AND s.namespace = $2
                """,
                pg_app.alice.user_id,
                pg_app.alice.namespace,
            )

        assert memory["owner_id"] == pg_app.alice.user_id
        assert memory["namespace"] == pg_app.alice.namespace
        assert memory["permission_mode"] == 600
        assert memory["source_provider"] == "claude-code"
        assert memory["source_session"] == f"{pg_app.prefix}-alice-session"
        assert memory["source_agent"] == "agent-a"
        assert "alice-only memory" in memory["verbatim_content"]
        assert [row["event_type"] for row in deliveries] == ["memory.created"]
        assert alice_memory_id in deliveries[0]["payload"]
    finally:
        await _cleanup(pg_app)


async def test_ingest_session_rolls_back_webhook_when_memory_insert_fails(pg_app: NamespaceHarness, monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.api.routes import ingest

    monkeypatch.setattr(lc, "_schedule_delivery_attempt", lambda coro: coro.close())
    monkeypatch.setattr(ingest.uuid, "uuid4", lambda: SimpleNamespace(hex="sameid000000000000000000000000"))

    try:
        async with pg_app.pool.acquire() as conn:
            await _subscribe(conn, owner_id=pg_app.alice.user_id, namespace=pg_app.alice.namespace)

        resp = await pg_app.client.post(
            "/ingest/session",
            headers=pg_app.alice.headers,
            json={
                "session_id": f"{pg_app.prefix}-rollback-session",
                "source": "claude-code",
                "raw_data": {
                    "messages": [{"role": "user", "content": "first insert"}],
                    "code_blocks": [{"text": "duplicate id insert"}],
                },
            },
        )
        assert resp.status_code == 500, resp.text

        async with pg_app.pool.acquire() as conn:
            memory_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE owner_id = $1 AND source_session = $2",
                pg_app.alice.user_id,
                f"{pg_app.prefix}-rollback-session",
            )
            delivery_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                WHERE s.owner_id = $1 AND s.namespace = $2
                """,
                pg_app.alice.user_id,
                pg_app.alice.namespace,
            )

        assert memory_count == 0
        assert delivery_count == 0
    finally:
        await _cleanup(pg_app)

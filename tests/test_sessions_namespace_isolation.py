from __future__ import annotations

import pytest

from tests.test_namespace_isolation import PG_URL, NamespaceHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="set MNEMOS_TEST_DB=postgres://... to run integration tests"),
    pytest.mark.asyncio,
]


async def _cleanup(pg_app: NamespaceHarness) -> None:
    async with pg_app.pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE user_id LIKE $1", f"{pg_app.prefix}%")


async def test_sessions_are_isolated_by_owner_and_namespace(pg_app: NamespaceHarness):
    try:
        resp = await pg_app.client.post(
            "/v1/sessions",
            json={"model": "gpt-4o", "initial_context": "alice context"},
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 200, resp.text
        alice_session = resp.json()["session_id"]

        resp = await pg_app.client.post(
            "/v1/sessions",
            json={"model": "gpt-4o", "initial_context": "bob context"},
            headers=pg_app.bob.headers,
        )
        assert resp.status_code == 200, resp.text
        bob_session = resp.json()["session_id"]

        resp = await pg_app.client.get(f"/v1/sessions/{bob_session}", headers=pg_app.alice.headers)
        assert resp.status_code == 404

        resp = await pg_app.client.get(f"/v1/sessions/{bob_session}/history", headers=pg_app.alice.headers)
        assert resp.status_code == 404

        resp = await pg_app.client.post(
            f"/v1/sessions/{bob_session}/messages",
            json={"role": "user", "content": "cross namespace write"},
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 404

        resp = await pg_app.client.delete(f"/v1/sessions/{bob_session}", headers=pg_app.alice.headers)
        assert resp.status_code == 404

        resp = await pg_app.client.get(f"/v1/sessions/{alice_session}", headers=pg_app.alice.headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_id"] == alice_session

        async with pg_app.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, namespace FROM sessions WHERE id = ANY($1::text[])",
                [alice_session, bob_session],
            )
        by_id = {row["id"]: row for row in rows}
        assert by_id[alice_session]["namespace"] == pg_app.alice.namespace
        assert by_id[bob_session]["namespace"] == pg_app.bob.namespace
    finally:
        await _cleanup(pg_app)

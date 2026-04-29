from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_namespace_isolation import PG_URL, NamespaceHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="set MNEMOS_TEST_DB=postgres://... to run integration tests"),
    pytest.mark.asyncio,
]


class _FakeGraeae:
    async def consult(self, prompt: str, task_type: str | None, selection=None, mode: str = "auto"):
        return {
            "all_responses": {
                "openai": {
                    "response_text": f"response for {prompt}",
                    "final_score": 0.9,
                    "latency_ms": 10,
                    "status": "success",
                }
            },
            "consensus_response": f"response for {prompt}",
            "consensus_score": 0.9,
            "winning_muse": "openai",
            "cost": 0.01,
            "latency_ms": 10,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "memory_ids": [],
        }


async def _cleanup(pg_app: NamespaceHarness) -> None:
    async with pg_app.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM graeae_audit_log WHERE consultation_id IN "
            "(SELECT id FROM graeae_consultations WHERE owner_id LIKE $1)",
            f"{pg_app.prefix}%",
        )
        await conn.execute(
            "DELETE FROM consultation_memory_refs WHERE consultation_id IN "
            "(SELECT id FROM graeae_consultations WHERE owner_id LIKE $1)",
            f"{pg_app.prefix}%",
        )
        await conn.execute("DELETE FROM graeae_consultations WHERE owner_id LIKE $1", f"{pg_app.prefix}%")


async def test_consultations_are_isolated_by_owner_and_namespace(pg_app: NamespaceHarness, monkeypatch):
    import mnemos.domain.graeae.engine as graeae_engine

    monkeypatch.setattr(graeae_engine, "get_graeae_engine", lambda: _FakeGraeae())

    try:
        resp = await pg_app.client.post(
            "/v1/consultations",
            json={"prompt": f"{pg_app.prefix} alice prompt", "task_type": "reasoning"},
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 200, resp.text
        alice_consultation = resp.json()["consultation_id"]

        resp = await pg_app.client.post(
            "/v1/consultations",
            json={"prompt": f"{pg_app.prefix} bob prompt", "task_type": "reasoning"},
            headers=pg_app.bob.headers,
        )
        assert resp.status_code == 200, resp.text
        bob_consultation = resp.json()["consultation_id"]

        resp = await pg_app.client.get(
            f"/v1/consultations/{bob_consultation}",
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 404

        resp = await pg_app.client.get(
            f"/v1/consultations/{bob_consultation}/artifacts",
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 404

        resp = await pg_app.client.get("/v1/consultations/audit", headers=pg_app.alice.headers)
        assert resp.status_code == 200, resp.text
        visible_ids = {entry["consultation_id"] for entry in resp.json()}
        assert alice_consultation in visible_ids
        assert bob_consultation not in visible_ids

        resp = await pg_app.client.get(
            f"/v1/consultations/{alice_consultation}",
            headers=pg_app.alice.headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == alice_consultation

        async with pg_app.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id::text, owner_id, namespace FROM graeae_consultations "
                "WHERE id = ANY($1::uuid[])",
                [alice_consultation, bob_consultation],
            )
        by_id = {row["id"]: row for row in rows}
        assert by_id[alice_consultation]["namespace"] == pg_app.alice.namespace
        assert by_id[bob_consultation]["namespace"] == pg_app.bob.namespace
    finally:
        await _cleanup(pg_app)

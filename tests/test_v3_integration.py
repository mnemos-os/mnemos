"""Integration tests for MNEMOS v3.0.0 using the in-process FastAPI app."""

import hashlib

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestConsultationsV1:
    async def test_create_consultation(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/consultations",
            json={
                "prompt": "What is the capital of France?",
                "task_type": "reasoning",
                "mode": "auto",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["consultation_id"].startswith("consult-")
        assert data["consensus_response"]
        assert "openai" in data["all_responses"]

    async def test_get_consultation(
        self, client: AsyncClient, auth_headers: dict, sample_consultation_id: str
    ):
        resp = await client.get(
            f"/v1/consultations/{sample_consultation_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_consultation_id
        assert data["consensus_response"]

    async def test_get_consultation_artifacts(
        self, client: AsyncClient, auth_headers: dict, sample_consultation_id: str
    ):
        resp = await client.get(
            f"/v1/consultations/{sample_consultation_id}/artifacts",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["consultation_id"] == sample_consultation_id
        assert "mem_seed_001" in data["citations"]

    async def test_list_audit_log(self, client: AsyncClient, auth_headers: dict):
        await client.post(
            "/v1/consultations",
            json={"prompt": "Write an audit row", "task_type": "reasoning"},
            headers=auth_headers,
        )
        resp = await client.get("/v1/consultations/audit", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data
        assert "chain_hash" in data[0]

    async def test_verify_audit_chain(self, client: AsyncClient, auth_headers: dict):
        await client.post(
            "/v1/consultations",
            json={"prompt": "Verify chain", "task_type": "reasoning"},
            headers=auth_headers,
        )
        resp = await client.get("/v1/consultations/audit/verify", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["entries_checked"] >= 1


class TestProvidersV1:
    async def test_list_providers(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/providers", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["providers"], list)
        assert data["total_models"] >= 1
        assert "status" in data

    async def test_provider_health(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/providers/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data

    async def test_recommend_model(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get(
            "/v1/providers/recommend",
            params={"task_type": "reasoning", "cost_budget": 10.0, "quality_floor": 0.85},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["recommended"]["provider"] == "openai"
        assert data["quality_score"] >= 0.85


@pytest.mark.skip(
    reason=(
        "v4.1: handlers migrated to backend-neutral repository surface; "
        "the conftest FakePool only wires _lc._pool, not _lc._persistence_backend. "
        "Equivalent integration coverage now lives in "
        "tests/test_persistence_parity.py:test_edge_sqlite_list_memories_serializes_timestamp_text "
        "(GET+POST) and "
        "tests/test_persistence_parity.py:test_edge_sqlite_search_memories_serializes_timestamp_text "
        "(real SqliteBackend + FastAPI TestClient). Reactivate after the "
        "client fixture is updated to install a seeded SqliteBackend."
    )
)
class TestMemoriesV1:
    async def test_create_memory(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/memories",
            json={"content": "Test memory content", "category": "solutions"},
            headers=auth_headers,
        )
        assert resp.status_code in [200, 201]

    async def test_list_memories(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/memories", headers=auth_headers)
        assert resp.status_code == 200

    async def test_search_memories(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/memories/search",
            json={"query": "integration search", "limit": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200


class TestDagRoutes:
    async def test_dag_routes_are_versioned(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/memories/mem_seed_001/log", headers=auth_headers)
        assert resp.status_code in [200, 404]


class TestDatabaseMigrations:
    async def test_consultation_memory_refs_table_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name='consultation_memory_refs')"
            )
        assert exists

    async def test_consultation_memory_refs_has_indexes(self, db_pool):
        async with db_pool.acquire() as conn:
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename='consultation_memory_refs'"
            )
        index_names = [idx["indexname"] for idx in indexes]
        assert any("consultation" in idx for idx in index_names)
        assert any("memory" in idx for idx in index_names)
        assert any("injected" in idx for idx in index_names)

    async def test_graeae_audit_log_table_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name='graeae_audit_log')"
            )
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='graeae_audit_log'"
            )
        assert exists
        col_names = [col["column_name"] for col in cols]
        assert "chain_hash" in col_names


class TestAuditChainIntegrity:
    async def test_audit_entries_form_chain(
        self, client: AsyncClient, auth_headers: dict, db_pool
    ):
        await client.post(
            "/v1/consultations",
            json={"prompt": "First chain row", "task_type": "reasoning"},
            headers=auth_headers,
        )
        await client.post(
            "/v1/consultations",
            json={"prompt": "Second chain row", "task_type": "reasoning"},
            headers=auth_headers,
        )
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id "
                "FROM graeae_audit_log ORDER BY sequence_num ASC"
            )

        genesis = hashlib.sha256(b"MNEMOS_AUDIT_GENESIS_v3").hexdigest()
        prev_chain = genesis
        for row in rows:
            expected = hashlib.sha256(
                (prev_chain + row["prompt_hash"] + row["response_hash"]).encode()
            ).hexdigest()
            assert expected == row["chain_hash"]
            prev_chain = row["chain_hash"]

    async def test_memory_refs_link_consultations(
        self, client: AsyncClient, auth_headers: dict, db_pool
    ):
        await client.post(
            "/v1/consultations",
            json={"prompt": "Capture memory refs", "task_type": "reasoning"},
            headers=auth_headers,
        )
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT cmr.consultation_id, cmr.memory_id, cmr.relevance_score "
                "FROM consultation_memory_refs cmr LIMIT 1"
            )
            consult_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM graeae_consultations WHERE id=$1)",
                row["consultation_id"],
            )
            memory_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM memories WHERE id=$1)",
                row["memory_id"],
            )

        assert consult_exists
        assert memory_exists


class TestVersions:
    async def test_health_reports_v3(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "4.2.0a1"

    async def test_api_version_in_responses(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/providers/health", headers=auth_headers)
        assert resp.status_code == 200

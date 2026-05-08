import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("MNEMOS_PATRONI_TEST") != "1",
    reason="set MNEMOS_PATRONI_TEST=1 to run Patroni integration smoke tests",
)


@pytest.mark.asyncio
async def test_pythia_patroni_leader_and_sql_round_trip() -> None:
    import asyncpg
    import httpx

    patroni_url = os.getenv("MNEMOS_PATRONI_PYTHIA_REST_URL", "http://192.168.207.67:8008")
    sql_dsn = os.getenv(
        "MNEMOS_PATRONI_SQL_DSN",
        "postgresql://mnemos_user:mnemos_local@192.168.207.67:5000/mnemos",
    )

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{patroni_url.rstrip('/')}/primary")

    assert response.status_code == 200
    payload = response.json()
    assert payload.get("state") == "running"
    assert payload.get("role") in {"master", "primary"}

    token = f"mnemos-patroni-smoke-{uuid.uuid4()}"
    conn = await asyncpg.connect(sql_dsn)
    try:
        result = await conn.fetchval("SELECT $1::text", token)
    finally:
        await conn.close()

    assert result == token

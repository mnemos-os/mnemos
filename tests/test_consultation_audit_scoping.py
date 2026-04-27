"""Consultation audit endpoint tenancy regressions."""

from __future__ import annotations

import hashlib

import pytest
from httpx import AsyncClient

from api.auth import UserContext, get_current_user

pytestmark = pytest.mark.asyncio


def _user(user_id: str, role: str = "user") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace="default",
        authenticated=True,
    )


@pytest.fixture
def current_user_override():
    from api_server import app

    current = {"user": _user("alice")}

    async def override_user():
        return current["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _create_consultation(
    client: AsyncClient,
    auth_headers: dict,
    current_user: dict,
    user_id: str,
    prompt: str,
) -> str:
    current_user["user"] = _user(user_id)
    resp = await client.post(
        "/v1/consultations",
        json={"prompt": prompt, "task_type": "reasoning"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    return resp.json()["consultation_id"]


async def _create_alice_bob_consultations(
    client: AsyncClient,
    auth_headers: dict,
    current_user: dict,
) -> dict[str, list[str]]:
    ids = {"alice": [], "bob": []}
    ids["alice"].append(await _create_consultation(
        client, auth_headers, current_user, "alice", "alice audit row 1",
    ))
    ids["bob"].append(await _create_consultation(
        client, auth_headers, current_user, "bob", "bob audit row 1",
    ))
    ids["alice"].append(await _create_consultation(
        client, auth_headers, current_user, "alice", "alice audit row 2",
    ))
    ids["bob"].append(await _create_consultation(
        client, auth_headers, current_user, "bob", "bob audit row 2",
    ))
    ids["alice"].append(await _create_consultation(
        client, auth_headers, current_user, "alice", "alice audit row 3",
    ))
    return ids


async def test_list_audit_log_scopes_to_caller(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    ids = await _create_alice_bob_consultations(
        client, auth_headers, current_user_override,
    )

    current_user_override["user"] = _user("alice")
    alice_resp = await client.get("/v1/consultations/audit", headers=auth_headers)
    assert alice_resp.status_code == 200
    alice_rows = alice_resp.json()
    assert {row["consultation_id"] for row in alice_rows} == set(ids["alice"])
    assert [row["sequence_num"] for row in alice_rows] == [3, 2, 1]
    alice_rows_by_sequence = {row["sequence_num"]: row for row in alice_rows}
    assert alice_rows_by_sequence[1]["prev_id"] is None
    assert alice_rows_by_sequence[2]["prev_id"] == alice_rows_by_sequence[1]["id"]
    assert alice_rows_by_sequence[3]["prev_id"] == alice_rows_by_sequence[2]["id"]
    assert {
        row["prev_id"] for row in alice_rows if row["prev_id"] is not None
    } <= {row["id"] for row in alice_rows}

    current_user_override["user"] = _user("bob")
    bob_resp = await client.get("/v1/consultations/audit", headers=auth_headers)
    assert bob_resp.status_code == 200
    bob_rows = bob_resp.json()
    assert {row["consultation_id"] for row in bob_rows} == set(ids["bob"])
    assert [row["sequence_num"] for row in bob_rows] == [2, 1]
    bob_rows_by_sequence = {row["sequence_num"]: row for row in bob_rows}
    assert bob_rows_by_sequence[1]["prev_id"] is None
    assert bob_rows_by_sequence[2]["prev_id"] == bob_rows_by_sequence[1]["id"]
    assert {
        row["prev_id"] for row in bob_rows if row["prev_id"] is not None
    } <= {row["id"] for row in bob_rows}


async def test_list_audit_log_root_sees_all(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    ids = await _create_alice_bob_consultations(
        client, auth_headers, current_user_override,
    )

    current_user_override["user"] = _user("root", role="root")
    resp = await client.get("/v1/consultations/audit", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert {row["consultation_id"] for row in rows} == set(ids["alice"] + ids["bob"])
    assert [row["sequence_num"] for row in rows] == [5, 4, 3, 2, 1]
    rows_by_sequence = {row["sequence_num"]: row for row in rows}
    assert rows_by_sequence[1]["prev_id"] is None
    for sequence_num in range(2, 6):
        assert rows_by_sequence[sequence_num]["prev_id"] == rows_by_sequence[
            sequence_num - 1
        ]["id"]


async def test_verify_audit_chain_scopes_to_caller(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    await _create_alice_bob_consultations(
        client, auth_headers, current_user_override,
    )

    current_user_override["user"] = _user("alice")
    alice_resp = await client.get("/v1/consultations/audit/verify", headers=auth_headers)
    assert alice_resp.status_code == 200
    alice_data = alice_resp.json()
    assert alice_data["valid"] is True
    assert alice_data["entries_checked"] == 3

    current_user_override["user"] = _user("bob")
    bob_resp = await client.get("/v1/consultations/audit/verify", headers=auth_headers)
    assert bob_resp.status_code == 200
    bob_data = bob_resp.json()
    assert bob_data["valid"] is True
    assert bob_data["entries_checked"] == 2


async def test_verify_audit_chain_root_global(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    await _create_alice_bob_consultations(
        client, auth_headers, current_user_override,
    )

    current_user_override["user"] = _user("root", role="root")
    resp = await client.get("/v1/consultations/audit/verify", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["entries_checked"] == 5


async def test_verify_audit_chain_uses_actual_global_predecessor(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
    db_pool,
):
    await _create_consultation(
        client, auth_headers, current_user_override, "alice", "alice audit row 1",
    )
    await _create_consultation(
        client, auth_headers, current_user_override, "bob", "bob hidden audit row",
    )
    await _create_consultation(
        client, auth_headers, current_user_override, "alice", "alice audit row 2",
    )

    rows = db_pool.state["audit_log"]
    alice_first = rows[0]
    alice_second = rows[2]
    alice_second["prev_id"] = alice_first["id"]
    alice_second["prev_chain_hash"] = alice_first["chain_hash"]
    alice_second["chain_hash"] = hashlib.sha256(
        (
            alice_first["chain_hash"]
            + alice_second["prompt_hash"]
            + alice_second["response_hash"]
        ).encode()
    ).hexdigest()

    current_user_override["user"] = _user("alice")
    resp = await client.get("/v1/consultations/audit/verify", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["entries_checked"] == 2
    assert data["entries_failed"] == [3]
    assert data["first_broken_sequence"] == 3
    assert "actual previous row" in data["message"]


async def test_get_consultation_owner_scope(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    bob_consultation_id = await _create_consultation(
        client, auth_headers, current_user_override, "bob", "bob private consultation",
    )

    current_user_override["user"] = _user("alice")
    resp = await client.get(
        f"/v1/consultations/{bob_consultation_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_get_consultation_artifacts_owner_scope(
    client: AsyncClient,
    auth_headers: dict,
    current_user_override: dict,
):
    bob_consultation_id = await _create_consultation(
        client, auth_headers, current_user_override, "bob", "bob private artifacts",
    )

    current_user_override["user"] = _user("alice")
    resp = await client.get(
        f"/v1/consultations/{bob_consultation_id}/artifacts",
        headers=auth_headers,
    )
    assert resp.status_code == 404

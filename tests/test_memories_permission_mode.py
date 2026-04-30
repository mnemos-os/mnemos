"""Regression coverage for client-supplied memory permission modes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes import memories
from mnemos.domain.models import MemoryUpdateRequest
from mnemos.persistence.visibility import VisibilityScope
from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


def _user(user_id: str = "alice", role: str = "user") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=f"{user_id}-ns",
        authenticated=True,
    )


@pytest.fixture
def current_user_override():
    from mnemos.api.main import app

    current = {"user": _user()}

    async def override_user():
        return current["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _row(memory_id: str = "mem_test", *, permission_mode: int = 600) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "id": memory_id,
        "content": "remember this",
        "category": "facts",
        "subcategory": None,
        "metadata": {},
        "quality_rating": 75,
        "compressed_content": None,
        "verbatim_content": "remember this",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": permission_mode,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "created": now,
        "updated": now,
    }


async def test_post_memory_permission_mode_644_stores_value(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", _row(permission_mode=644))

    resp = await client.post(
        "/v1/memories",
        json={"content": "remember this", "category": "facts", "permission_mode": 644},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["permission_mode"] == 644
    insert_calls = [payload for name, payload in backend.memories.calls if name == "insert_memory"]
    assert insert_calls[0]["permission_mode"] == 644


async def test_post_memory_permission_mode_defaults_to_600(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", _row(permission_mode=600))

    resp = await client.post(
        "/v1/memories",
        json={"content": "remember this", "category": "facts"},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    insert_calls = [payload for name, payload in backend.memories.calls if name == "insert_memory"]
    assert insert_calls[0]["permission_mode"] == 600


async def test_post_memory_permission_mode_999_returns_422(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)

    resp = await client.post(
        "/v1/memories",
        json={"content": "remember this", "category": "facts", "permission_mode": 999},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert [name for name, _payload in backend.memories.calls] == []


async def test_patch_memory_permission_mode_updates(monkeypatch: pytest.MonkeyPatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "update_memory",
        _row(memory_id="mem_patch", permission_mode=644),
    )

    response = await memories.update_memory(
        "mem_patch",
        MemoryUpdateRequest(permission_mode=644),
        user=_user(),
    )

    update_calls = [payload for name, payload in backend.memories.calls if name == "update_memory"]
    assert update_calls[0]["fields"] == {"permission_mode": 644}
    assert response.permission_mode == 644


async def test_bulk_create_uses_per_row_permission_mode(
    client,
    auth_headers: dict[str, str],
    current_user_override,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = install_fake_backend(monkeypatch)

    resp = await client.post(
        "/v1/memories/bulk",
        json={
            "memories": [
                {"content": "private", "category": "facts"},
                {"content": "world", "category": "facts", "permission_mode": 644},
                {"content": "read only", "category": "facts", "permission_mode": 404},
            ]
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["created"] == 3
    insert_calls = [payload for name, payload in backend.memories.calls if name == "insert_memory"]
    assert [call["permission_mode"] for call in insert_calls] == [600, 644, 404]


async def test_non_root_cannot_patch_other_users_permission_mode(monkeypatch: pytest.MonkeyPatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("update_memory", None)

    with pytest.raises(HTTPException) as exc:
        await memories.update_memory(
            "mem_bob",
            MemoryUpdateRequest(permission_mode=644),
            user=_user("alice"),
        )

    assert exc.value.status_code == 404
    update_calls = [payload for name, payload in backend.memories.calls if name == "update_memory"]
    assert update_calls[0]["fields"] == {"permission_mode": 644}
    visibility = update_calls[0]["visibility"]
    assert visibility.scope is VisibilityScope.OWN_ONLY
    assert visibility.user_id == "alice"

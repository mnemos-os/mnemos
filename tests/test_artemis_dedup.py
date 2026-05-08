"""ARTEMIS duplicate-content detection on memory create."""

from __future__ import annotations

import json
import pytest
from typer.testing import CliRunner

from mnemos.core import config as core_config

@pytest.fixture(autouse=True)
def _reset_artemis_dedup_settings(monkeypatch):
    for key in (
        "MNEMOS_ARTEMIS_DEDUP_MODE",
        "MNEMOS_ARTEMIS_DEDUP_CROSS_NAMESPACE",
    ):
        monkeypatch.delenv(key, raising=False)
    core_config.reload_settings()
    yield
    for key in (
        "MNEMOS_ARTEMIS_DEDUP_MODE",
        "MNEMOS_ARTEMIS_DEDUP_CROSS_NAMESPACE",
    ):
        monkeypatch.delenv(key, raising=False)
    core_config.reload_settings()


@pytest.mark.asyncio
async def test_create_duplicate_rejects_by_default(client, auth_headers):
    first = await client.post(
        "/v1/memories",
        json={"content": "identical\r\ncontent", "category": "facts"},
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/v1/memories",
        json={"content": "identical\ncontent", "category": "facts"},
        headers=auth_headers,
    )

    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "duplicate_content"
    assert body["existing_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_create_duplicate_merge_returns_existing_and_bumps_recall(
    client,
    auth_headers,
    monkeypatch,
    db_pool,
):
    monkeypatch.setenv("MNEMOS_ARTEMIS_DEDUP_MODE", "merge")
    core_config.reload_settings()

    first = await client.post(
        "/v1/memories",
        json={"content": "merge me", "category": "facts"},
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text
    existing_id = first.json()["id"]

    second = await client.post(
        "/v1/memories",
        json={"content": "merge me", "category": "facts"},
        headers=auth_headers,
    )

    assert second.status_code == 200, second.text
    assert second.json()["id"] == existing_id
    assert db_pool.state["memories"][existing_id]["recall_count"] == 1
    assert db_pool.state["memories"][existing_id]["last_recalled_at"] is not None


@pytest.mark.asyncio
async def test_create_duplicate_off_allows_silent_duplicate(
    client,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setenv("MNEMOS_ARTEMIS_DEDUP_MODE", "off")
    core_config.reload_settings()

    first = await client.post(
        "/v1/memories",
        json={"content": "off duplicate", "category": "facts"},
        headers=auth_headers,
    )
    second = await client.post(
        "/v1/memories",
        json={"content": "off duplicate", "category": "facts"},
        headers=auth_headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_create_duplicate_isolated_across_namespaces(
    client,
    auth_headers,
):
    from mnemos.api.dependencies import UserContext, get_current_user
    from mnemos.api.main import app

    async def root_user():
        return UserContext(
            user_id="root",
            group_ids=[],
            role="root",
            namespace="root-ns",
            authenticated=True,
        )

    app.dependency_overrides[get_current_user] = root_user
    try:
        first = await client.post(
            "/v1/memories",
            json={"content": "namespace-local", "category": "facts", "namespace": "a"},
            headers=auth_headers,
        )
        second = await client.post(
            "/v1/memories",
            json={"content": "namespace-local", "category": "facts", "namespace": "b"},
            headers=auth_headers,
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


def test_dedup_sweep_cli_dry_run_identifies_duplicates(monkeypatch):
    from mnemos.cli import main as cli_main
    from tests._fake_backend import FakeBackend

    backend = FakeBackend()
    backend.memories.configure_return(
        "find_duplicate_content_groups",
        [
            {
                "owner_id": "alice",
                "namespace": "lab",
                "content_hash": "a" * 64,
                "duplicate_count": 2,
                "memory_ids": ["mem_old", "mem_new"],
                "canonical_id": "mem_old",
            }
        ],
    )

    async def _open_backend():
        return backend, False

    monkeypatch.setattr(cli_main, "_open_cli_persistence_backend", _open_backend)
    result = CliRunner().invoke(
        cli_main.app,
        ["artemis", "dedup-sweep", "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["group_count"] == 1
    assert payload["duplicate_count"] == 1
    assert payload["groups"][0]["canonical_id"] == "mem_old"
    assert payload["groups"][0]["duplicate_ids"] == ["mem_new"]


def test_dedup_sweep_cli_auto_merge_consolidates(monkeypatch):
    from mnemos.cli import main as cli_main
    from mnemos.domain.artemis_dedup import content_sha256
    from tests._fake_backend import FakePoolBackedBackend

    state = {"memories": {}}

    class _Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return None

    class _Pool:
        def __init__(self, state):
            self.state = state

        def acquire(self):
            return _Acquire()

    backend = FakePoolBackedBackend(_Pool(state))
    digest = content_sha256("same")
    state["memories"] = {
        "mem_old": {
            "id": "mem_old",
            "owner_id": "alice",
            "namespace": "lab",
            "content_hash": digest,
            "content": "same",
            "created": "2026-01-01T00:00:00",
            "deleted_at": None,
            "archived_at": None,
            "consolidated_into": None,
        },
        "mem_new": {
            "id": "mem_new",
            "owner_id": "alice",
            "namespace": "lab",
            "content_hash": digest,
            "content": "same",
            "created": "2026-01-02T00:00:00",
            "deleted_at": None,
            "archived_at": None,
            "consolidated_into": None,
        },
    }

    async def _open_backend():
        return backend, False

    monkeypatch.setattr(cli_main, "_open_cli_persistence_backend", _open_backend)
    result = CliRunner().invoke(
        cli_main.app,
        ["artemis", "dedup-sweep", "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["merged_count"] == 1
    assert state["memories"]["mem_new"]["consolidated_into"] == "mem_old"
    assert state["memories"]["mem_new"]["deleted_at"] is not None

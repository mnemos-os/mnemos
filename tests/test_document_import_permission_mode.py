"""Regression coverage for permission_mode plumbing in document_import."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


async def test_import_document_rejects_invalid_permission_mode(client, auth_headers):
    """422 fires before DOCLING/pool checks so bad input is visible immediately."""
    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("test.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "permission_mode": "999"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "permission_mode" in resp.text


async def test_batch_import_rejects_invalid_permission_mode(client, auth_headers):
    resp = await client.post(
        "/v1/documents/batch-import",
        files=[("files", ("doc1.pdf", b"%PDF-1.4\nx"))],
        data={"category": "documents", "permission_mode": "888"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "permission_mode" in resp.text


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_document_persists_explicit_permission_mode(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Explicit permission_mode=644 reaches the INSERT statement."""
    import mnemos.core.lifecycle as lc

    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "fed.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk",
                "content": "federation-visible chunk",
                "metadata": {"source_file": "fed.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer

    mock_conn = AsyncMock()

    # Round-47 added a per-chunk ``async with conn.transaction():``
    # wrapper inside document_import. asyncpg's
    # Connection.transaction() is a SYNCHRONOUS factory returning
    # an async-context-manager — give the mock a stub that
    # returns one explicitly so the route's ``async with`` works.
    class _TxCtx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_TxCtx())

    # Stub the in-transaction webhook dispatch so it returns an
    # empty delivery_ids list without touching webhook_subscriptions.
    monkeypatch.setattr(
        "mnemos.api.routes.document_import._dispatch_webhook"
        if False else "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    mock_pool_manager = MagicMock()
    mock_pool_manager.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool_manager.acquire.return_value.__aexit__.return_value = None
    monkeypatch.setattr(lc, "get_pool_manager", lambda: mock_pool_manager)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("fed.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "permission_mode": "644"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    # Last positional arg of INSERT is the permission_mode value
    assert mock_conn.execute.await_count >= 1
    last_call = mock_conn.execute.await_args_list[-1]
    assert last_call.args[-1] == 644


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_document_defaults_to_600(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    import mnemos.core.lifecycle as lc

    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "default.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk",
                "content": "default-perm chunk",
                "metadata": {"source_file": "default.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer

    mock_conn = AsyncMock()

    # Round-47 added a per-chunk ``async with conn.transaction():``
    # wrapper inside document_import. asyncpg's
    # Connection.transaction() is a SYNCHRONOUS factory returning
    # an async-context-manager — give the mock a stub that
    # returns one explicitly so the route's ``async with`` works.
    class _TxCtx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_TxCtx())

    # Stub the in-transaction webhook dispatch so it returns an
    # empty delivery_ids list without touching webhook_subscriptions.
    monkeypatch.setattr(
        "mnemos.api.routes.document_import._dispatch_webhook"
        if False else "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    mock_pool_manager = MagicMock()
    mock_pool_manager.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool_manager.acquire.return_value.__aexit__.return_value = None
    monkeypatch.setattr(lc, "get_pool_manager", lambda: mock_pool_manager)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("default.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    last_call = mock_conn.execute.await_args_list[-1]
    assert last_call.args[-1] == 600

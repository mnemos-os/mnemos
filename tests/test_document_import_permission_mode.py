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

    # Round-68 switched the chunk INSERT from ``execute`` to
    # ``fetchval`` (so ON CONFLICT (import_chunk_key) DO UPDATE
    # ... RETURNING id can return the canonical row id). Default
    # the fetchval mock to echo back the surrogate id (first
    # positional arg after the SQL string), simulating a
    # successful new-row insert.
    # Round-72 added a legacy v70 chunk_key resolution UPDATE
    # that runs BEFORE the INSERT-with-ON-CONFLICT. The mock
    # has to return None for the UPDATE (no legacy row exists in
    # this fresh-fixture scenario) so the helper falls through to
    # the new INSERT — otherwise the INSERT never fires and the
    # permission_mode positional-arg assertion below has nothing
    # to check.
    async def _fetchval_legacy_aware(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            return None
        return args[1] if len(args) >= 2 else None

    mock_conn.fetchval.side_effect = _fetchval_legacy_aware

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

    # Permission_mode is the 9th positional arg of the chunk
    # INSERT (id, content, category, subcategory, metadata,
    # verbatim_content, owner_id, namespace, permission_mode,
    # import_chunk_key). The 10th — ``import_chunk_key`` —
    # appended in round-68. Inspect fetchval (was execute pre-
    # round-68).
    assert mock_conn.fetchval.await_count >= 1
    last_call = mock_conn.fetchval.await_args_list[-1]
    # args = (sql, id, content, category, subcategory, metadata,
    #         verbatim_content, owner_id, namespace,
    #         permission_mode, import_chunk_key)
    # permission_mode is at index 9 (0=sql, 1=id, ..., 9=perm_mode).
    assert last_call.args[9] == 644


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

    # Round-68 fetchval default — see explicit-permission_mode
    # test above for the why.
    # Round-72 added a legacy v70 chunk_key resolution UPDATE
    # that runs BEFORE the INSERT-with-ON-CONFLICT. The mock
    # has to return None for the UPDATE (no legacy row exists in
    # this fresh-fixture scenario) so the helper falls through to
    # the new INSERT — otherwise the INSERT never fires and the
    # permission_mode positional-arg assertion below has nothing
    # to check.
    async def _fetchval_legacy_aware(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            return None
        return args[1] if len(args) >= 2 else None

    mock_conn.fetchval.side_effect = _fetchval_legacy_aware

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
    last_call = mock_conn.fetchval.await_args_list[-1]
    # See round-68 commit on the permission_mode positional layout.
    assert last_call.args[9] == 600

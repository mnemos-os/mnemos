"""Active-project scoping for document import."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


class _TxCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return None


def _wire_import(monkeypatch, mock_conn):
    import mnemos.core.lifecycle as lc

    async def _fetchval_default(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            return None
        return args[1] if len(args) >= 2 else None

    mock_conn.fetchval.side_effect = _fetchval_default
    mock_pool_manager = MagicMock()
    mock_pool_manager.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool_manager.acquire.return_value.__aexit__.return_value = None
    monkeypatch.setattr(lc, "get_pool_manager", lambda: mock_pool_manager)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )


def _one_chunk_importer(mock_importer_class, *, text: str = "active project docs"):
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        text,
        {"source_file": "scope.md"},
        [
            {
                "chunk_num": 0,
                "title": "Scope",
                "content": text,
                "metadata": {"source_file": "scope.md", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer


async def test_import_without_project_tag_returns_422(client, auth_headers):
    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("scope.md", b"# Scope")},
        data={"category": "documents"},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "project_tag" in resp.text


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_with_project_tag_persists_metadata(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    _one_chunk_importer(mock_importer_class)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_import(monkeypatch, mock_conn)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("scope.md", b"# Scope")},
        data={"category": "documents", "project_tag": "ic-engine"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    insert_call = mock_conn.fetchval.await_args_list[-1]
    metadata = json.loads(insert_call.args[5])
    assert metadata["project_tag"] == "ic-engine"
    assert metadata["import_source"] == "doc-import"


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_archive_snapshot_rejected_by_default(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    _one_chunk_importer(
        mock_importer_class,
        text="[PYTHIA] /mnt/datapool/backups/old-project/README.md",
    )
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_import(monkeypatch, mock_conn)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("archive.md", b"# archived")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "historical archive snapshot" in resp.text
    assert mock_conn.fetchval.await_count == 0


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_archive_snapshot_override_records_audit_metadata(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    _one_chunk_importer(
        mock_importer_class,
        text="[ARTEMIS] /Users/jperlow/.claude/plugins/cache/old/doc.md",
    )
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_import(monkeypatch, mock_conn)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("archive.md", b"# archived")},
        data={
            "category": "documents",
            "project_tag": "mnemos",
            "allow_archive_snapshot": "true",
        },
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    insert_call = mock_conn.fetchval.await_args_list[-1]
    metadata = json.loads(insert_call.args[5])
    assert metadata["archive_override_at"]
    assert metadata["archive_override_reason"] == "claude_plugin_cache"

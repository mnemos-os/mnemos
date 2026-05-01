"""Regression coverage for document_import HTTP status semantics
when webhook-outbox dispatch fails inside the per-chunk
transaction.

Round-47 wrapped each chunk's INSERT memory + webhook
``_dispatch_webhook(conn=conn)`` in a single transaction, so a
webhook table problem rolls the chunk back. Codex round-1 of
round-47 caught that the route then hid the rollback behind a
200 response — clients that only check HTTP status would treat
the document as imported and could later double-import on retry.

Round-48 returns:
  * 200 OK when every chunk committed.
  * 207 Multi-Status when some chunks committed and some rolled
    back (partial success, recoverable on retry).
  * 502 Bad Gateway when zero chunks committed (full failure).

These tests pin the contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


class _TxCtx:
    async def __aenter__(self_):
        return None

    async def __aexit__(self_, *args):
        return None


def _wire_pool_manager(monkeypatch, mock_conn):
    import mnemos.core.lifecycle as lc

    mock_pool_manager = MagicMock()
    mock_pool_manager.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool_manager.acquire.return_value.__aexit__.return_value = None
    monkeypatch.setattr(lc, "get_pool_manager", lambda: mock_pool_manager)


def _stub_importer_with_chunks(mock_importer_class, n: int):
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "doc.pdf"},
        [
            {
                "chunk_num": i,
                "title": f"Chunk {i}",
                "content": f"chunk {i} content",
                "metadata": {"source_file": "doc.pdf", "chunk_num": i},
            }
            for i in range(n)
        ],
    )
    mock_importer_class.return_value = mock_importer


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_all_chunks_committed_returns_200(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Happy path: every chunk's transaction commits → 200."""
    _stub_importer_with_chunks(mock_importer_class, n=2)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["memories_created"] == 2
    assert body["errors"] == []


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_partial_failure_returns_207_multi_status(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Some chunks commit, some roll back → 207 Multi-Status.

    Drives the codex-flagged scenario: webhook dispatch raises on
    chunk #2 (zero-indexed). Chunks 0 and 1 commit. The endpoint
    must NOT return 200 (would hide the partial failure from
    HTTP-status-only clients) — 207 surfaces the partial-success
    state.
    """
    _stub_importer_with_chunks(mock_importer_class, n=3)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    call_count = {"n": 0}

    async def _flaky_dispatch(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("webhook table unavailable")
        return []

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _flaky_dispatch,
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 207, (
        f"partial-failure must surface as 207 Multi-Status; got "
        f"{resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["memories_created"] == 2
    assert len(body["errors"]) == 1
    assert body["errors"][0]["chunk"] == 2


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_total_failure_returns_502(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Zero chunks commit (every chunk rolls back) → 502 Bad
    Gateway. Nothing was persisted; client should treat the import
    as fully failed and fix the underlying issue before retry."""
    _stub_importer_with_chunks(mock_importer_class, n=2)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    async def _always_fail(*args, **kwargs):
        raise RuntimeError("webhook table unavailable")

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _always_fail,
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["memories_created"] == 0
    assert len(body["errors"]) == 2


# ── /batch-import: per-file status_code in result + top-level
# 207/502 surfacing (codex round-2 of round-47 caught that the
# pre-refactor JSONResponse return leaked Response internals
# through the batch list). ────────────────────────────────────


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_partial_failure_returns_207(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """File 1 fully succeeds, file 2 fully fails — top-level batch
    response is 207 with per-file ``status_code`` field on each
    entry."""
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    call_count = {"n": 0}

    async def _flaky_dispatch(*args, **kwargs):
        call_count["n"] += 1
        # First file's chunk → succeed. Second file's chunk → fail.
        if call_count["n"] == 1:
            return []
        raise RuntimeError("simulated for file 2")

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _flaky_dispatch,
    )

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 207, (
        f"batch with mixed-success files must return 207; got "
        f"{resp.status_code}: {resp.text}"
    )
    results = resp.json()
    assert isinstance(results, list)
    assert len(results) == 2
    # Each per-file entry must be a flat dict (NOT a Response
    # body / status_code / raw_headers triple). status_code is a
    # FIELD inside the dict, not a leaked Response attribute.
    for entry in results:
        assert isinstance(entry, dict)
        assert "raw_headers" not in entry, (
            f"per-file entry leaks Response internals: {entry!r}"
        )
        assert "status_code" in entry
    assert results[0]["status_code"] == 200
    assert results[1]["status_code"] == 502


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_total_failure_returns_502(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Every file fails → batch returns 502."""
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    async def _always_fail(*args, **kwargs):
        raise RuntimeError("webhook table unavailable")

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _always_fail,
    )

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 0
        assert entry["status_code"] == 502


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_all_success_returns_200(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Every file fully imports → batch returns 200 (legacy
    contract, unchanged from pre-round-48)."""
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 1
        assert entry["status_code"] == 200

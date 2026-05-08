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
    """Wire a mock connection into the lifecycle pool manager.

    Round-68 switched the chunk INSERT to ``fetchval`` (so the
    ON CONFLICT path can return the canonical id). Tests that use
    this helper get a default ``fetchval`` that echoes back the
    first positional arg — i.e., simulates the new-row insert
    path returning the surrogate ``memory_id`` we generated. Tests
    that need to simulate the conflict path (existing-row id
    different from the surrogate) override
    ``mock_conn.fetchval`` themselves.
    """
    import mnemos.core.lifecycle as lc

    async def _fetchval_default(*args, **kwargs):
        # Distinguish the round-72 legacy-resolution UPDATE
        # query from the new INSERT-with-ON-CONFLICT query.
        # The legacy UPDATE is ``UPDATE memories SET ...
        # RETURNING id`` — return None to simulate "no legacy
        # row found" (the common case in fresh-test fixtures).
        # The new INSERT is ``INSERT INTO memories ... ON
        # CONFLICT ... RETURNING id`` — echo back the first
        # positional arg (the surrogate ``id`` from VALUES)
        # to simulate a new-row insert.
        sql = args[0] if args else ""
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            return None
        return args[1] if len(args) >= 2 else None

    # ``mock_conn = AsyncMock()`` auto-creates a ``fetchval``
    # AsyncMock attribute on access, so isinstance checks against
    # the auto-generated child don't tell us whether the test
    # explicitly configured fetchval. Force-set the side_effect
    # so the default new-row-returning behavior is in place;
    # tests that need conflict semantics override fetchval AFTER
    # this helper runs.
    mock_conn.fetchval.side_effect = _fetchval_default

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
        data={"category": "documents", "project_tag": "mnemos"},
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
        data={"category": "documents", "project_tag": "mnemos"},
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
        data={"category": "documents", "project_tag": "mnemos"},
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
        data={"category": "documents", "project_tag": "mnemos"},
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
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 502
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 0
        assert entry["status_code"] == 502


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
async def test_batch_import_all_empty_files_returns_207_not_502(
    client,
    auth_headers,
):
    """Codex round-3 of round-47 caught that the previous 502
    aggregation conflated genuine infra-rollback (502) with
    client-error 4xx — every empty file would have produced
    ``memories_created=0`` and the batch would have returned
    502 (retryable gateway failure) when the right answer is
    207 with per-file 400 entries that clients should NOT retry."""
    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("empty1.pdf", b"")),
            ("files", ("empty2.pdf", b"")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 207, (
        f"all-empty-files batch must NOT return 502; got "
        f"{resp.status_code}: {resp.text}"
    )
    results = resp.json()
    assert len(results) == 2
    # Per-file entries carry the actual 400 client-error code so
    # client-side retry logic can do the right thing.
    for entry in results:
        assert entry["status_code"] == 400
        assert entry["memories_created"] == 0


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
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 1
        assert entry["status_code"] == 200


# ── Mid-batch pool-loss + infra-error escape (codex round-2 of
# round-62 caught that the round-62 pre-loop pool check only
# protected against pool absence BEFORE the batch loop began;
# the per-file try/except HTTPException would otherwise fold a
# mid-batch pool-loss 503 into a 207 body and hide a deployment-
# wide DB outage behind a success-shaped batch response.) ──────


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_mid_batch_pool_loss_returns_top_level_503(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Pool drops AFTER the pre-loop check passes — file 1 imports
    OK, then ``_lc._pool`` is cleared, file 2 hits the helper's
    per-file pool check and raises 503. The batch's per-file
    try/except HTTPException catches it and adds it to results
    with ``status_code=503``; the top-level aggregator must
    surface 503 (not 207) so operators see the deployment-level
    unavailability through HTTP status alone.

    Pre-fix the aggregator only checked all-502 → 502, so a single
    mid-batch 503 mixed with 200s collapsed to 207 and a SQLite-
    incompat or transient pool outage looked like a per-file content
    issue.
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    # File 1 succeeds, then the pool object disappears, so file 2
    # hits the helper's ``require_postgres_pool_or_503`` and raises
    # HTTPException(503, "POST /v1/documents/batch-import requires
    # the Postgres backend; ...").
    import mnemos.core.lifecycle as lc

    real_pool = lc._pool  # FakePool from db_pool fixture
    original_pool_kind = type(real_pool).__name__

    dispatch_call_count = {"n": 0}

    async def _dispatch_then_drop_pool(*args, **kwargs):
        dispatch_call_count["n"] += 1
        # File 1's chunk dispatch — drop the pool AFTER this call so
        # file 2's per-file ``require_postgres_pool_or_503`` check
        # sees an absent pool and raises 503. The dispatch itself
        # returns normally so file 1 completes with status_code=200.
        if dispatch_call_count["n"] == 1:
            monkeypatch.setattr(lc, "_pool", None)
        return []

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _dispatch_then_drop_pool,
    )

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, (
        f"mid-batch pool-loss must surface as TOP-LEVEL 503, not "
        f"a 207 body — got {resp.status_code}: {resp.text}\n"
        f"(helper test-fixture pool kind: {original_pool_kind})"
    )
    results = resp.json()
    assert isinstance(results, list)
    assert len(results) == 2
    # Body still preserves per-file outcomes so retry-aware clients
    # can see which committed vs which were rejected.
    per_status = [int(r.get("status_code", 0)) for r in results]
    # File 1 happened before the pool drop — it committed.
    # File 2 hit the dropped-pool check — it 503'd.
    assert 503 in per_status, f"missing per-file 503 in {per_status!r}"


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_single_import_mid_file_infra_loss_preserves_committed_chunks(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Chunk 1 commits, chunk 2 raises ``asyncio.TimeoutError``
    mid-loop — single-file ``POST /v1/documents/import`` must
    return 503 with the committed chunk's ``memory_ids`` and
    ``memories_created=1`` preserved in the body.

    Pre-fix the helper raised a bare ``HTTPException(503)`` and
    the route's JSONResponse fallback dropped the partial-commit
    payload, so a retry-aware client would re-import the same
    chunk under a fresh non-deterministic ``new_memory_id()``,
    creating persistent duplicates. Codex round-3 of round-62
    caught this. The fix returns ``(payload, 503)`` from the
    helper with ``memory_ids`` populated for committed chunks.
    """
    import asyncio as _asyncio

    _stub_importer_with_chunks(mock_importer_class, n=2)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    dispatch_call_count = {"n": 0}

    async def _dispatch_then_infra_fail(*args, **kwargs):
        dispatch_call_count["n"] += 1
        # Chunk 1: commits cleanly.
        # Chunk 2: raise asyncio.TimeoutError mid-transaction so the
        # helper's outer try/except catches an infra-class error
        # AFTER chunk 1 has appended to memory_ids.
        if dispatch_call_count["n"] >= 2:
            raise _asyncio.TimeoutError("simulated pool-acquire timeout")
        return []

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _dispatch_then_infra_fail,
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, (
        f"mid-file infra loss must surface as 503; got "
        f"{resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    # Must preserve the committed chunk's memory_id so a retry-
    # aware client can reconcile and skip re-importing.
    assert payload["memories_created"] == 1, (
        f"committed chunks lost — payload reports "
        f"{payload['memories_created']}: {payload!r}"
    )
    assert len(payload["memory_ids"]) == 1
    # Errors list must record the infra failure.
    assert any(
        "infrastructure" in (e.get("error") or "").lower()
        for e in payload.get("errors", [])
    ), f"infra failure not recorded in errors: {payload.get('errors')!r}"


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_single_import_transaction_exit_infra_loss_surfaces_unconfirmed_id(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """The chunk's INSERT statement was accepted by the server, but
    ``conn.transaction().__aexit__`` raises asyncio.TimeoutError
    before / during commit-ack. The row may or may not have
    committed — the helper cannot tell.

    Pre-round-65 the helper appended to ``memory_ids`` only AFTER
    the ``async with conn.transaction()`` block exited, so a
    commit-ack timeout discarded the ID entirely. A retry-aware
    client got no ID to query, retried the chunk, and
    ``new_memory_id()`` produced a fresh non-deterministic ID —
    creating a duplicate row on top of the (possibly already
    committed) original. Codex round-4 of round-62 caught this
    commit-ambiguity case explicitly.

    The fix surfaces the in-flight ID in
    ``unconfirmed_memory_ids`` so retry clients can query
    ``GET /v1/memories/{id}`` to reconcile before retrying.
    """
    import asyncio as _asyncio

    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()

    class _FailingExitTx:
        """Async context manager whose ``__aexit__`` raises
        asyncio.TimeoutError to simulate commit-ack loss."""

        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *exc_info):
            raise _asyncio.TimeoutError("simulated commit-ack timeout")

    mock_conn.transaction = MagicMock(return_value=_FailingExitTx())
    _wire_pool_manager(monkeypatch, mock_conn)

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, (
        f"transaction-exit infra loss must surface as 503; got "
        f"{resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    # The chunk did NOT confirm-commit, so ``memories_created``
    # stays 0 and ``memory_ids`` stays empty.
    assert payload["memories_created"] == 0
    assert payload["memory_ids"] == []
    # But the in-flight ID MUST surface so the client can query +
    # reconcile. Pre-round-65 this would have been [].
    assert payload.get("unconfirmed_memory_ids"), (
        f"in-flight memory_id missing from payload: {payload!r}"
    )
    assert len(payload["unconfirmed_memory_ids"]) == 1


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_mid_file_infra_loss_preserves_per_file_committed_chunks(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """File 1 chunk 1 commits, file 1 chunk 2 raises
    ``asyncio.TimeoutError`` mid-loop. The batch helper returns
    ``(payload, 503)`` for that file with ``memories_created=1``.
    The aggregator surfaces top-level 503; the response body's
    per-file entry must preserve the committed memory_ids so the
    client can skip the already-imported chunk on retry.
    """
    import asyncio as _asyncio

    _stub_importer_with_chunks(mock_importer_class, n=2)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    dispatch_call_count = {"n": 0}

    async def _dispatch_then_infra_fail(*args, **kwargs):
        dispatch_call_count["n"] += 1
        # First chunk: dispatch ok.
        # Second chunk (still file 1): infra timeout.
        if dispatch_call_count["n"] >= 2:
            raise _asyncio.TimeoutError("simulated pool-acquire timeout")
        return []

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        _dispatch_then_infra_fail,
    )

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, (
        f"batch with mid-file infra-loss must surface 503; got "
        f"{resp.status_code}: {resp.text}"
    )
    results = resp.json()
    assert isinstance(results, list)
    assert len(results) == 1
    entry = results[0]
    assert entry.get("status_code") == 503
    assert entry.get("memories_created") == 1, (
        f"committed chunk lost — entry: {entry!r}"
    )
    assert len(entry.get("memory_ids", [])) == 1


# ── Round-68: ON CONFLICT (import_chunk_key) DO UPDATE
# RETURNING id is the stable-chunk-identity primitive that closes
# the deferred-design follow-up from rounds 65-67. A retry of the
# same chunk hits the existing row and ``RETURNING id`` returns
# its canonical id, so retry-aware clients no longer risk
# duplicate imports under commit-ambiguous failures. ────────────


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_uses_canonical_id_from_returning_clause(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Simulate the ON CONFLICT path: ``fetchval`` returns an id
    DIFFERENT from the surrogate ``new_memory_id()`` we passed in.
    The helper must trust the RETURNING value and surface THAT id
    in ``memory_ids``, not the surrogate. Pre-round-68 the helper
    appended the surrogate id directly; under a real conflict that
    would lie about which row exists.
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    # Override the default echo-first-arg behavior with a fixed
    # canonical id that is OBVIOUSLY different from any value
    # ``new_memory_id()`` would generate. This simulates Postgres
    # returning the EXISTING row's id from the ON CONFLICT path.
    canonical_id = "mem_canonical_from_existing_row"
    mock_conn.fetchval.side_effect = None
    mock_conn.fetchval.return_value = canonical_id

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    # The canonical id from RETURNING is what surfaces, NOT the
    # surrogate ``new_memory_id()`` value the INSERT VALUES used.
    assert payload["memory_ids"] == [canonical_id], (
        f"helper appended surrogate id instead of canonical "
        f"RETURNING id: {payload['memory_ids']!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_is_stable_and_present_in_insert(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """The chunk INSERT must include ``import_chunk_key`` as the
    last positional arg, and the value must be a stable sha256-
    derived hex string (NOT random) so a retry of the same file +
    chunk_num produces the SAME key and hits ON CONFLICT.

    The stable-key derivation uses
    ``sha256(owner_id NUL namespace NUL filename NUL chunk_num)``;
    this test verifies the call site without re-deriving the
    expected hash (the helper is the source of truth).
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    # Two back-to-back imports of the same file should produce the
    # same chunk_key — that's the contract that lets ON CONFLICT
    # actually fire on retry.
    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("stable.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    first_call = mock_conn.fetchval.await_args_list[-1]
    # args = (sql, id, content, category, subcategory, metadata,
    #         verbatim_content, owner_id, namespace,
    #         permission_mode, import_chunk_key)
    chunk_key_1 = first_call.args[10]
    assert isinstance(chunk_key_1, str)
    assert len(chunk_key_1) == 64, (
        f"expected sha256 hex (64 chars), got {len(chunk_key_1)}: "
        f"{chunk_key_1!r}"
    )
    assert all(c in "0123456789abcdef" for c in chunk_key_1)

    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("stable.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    second_call = mock_conn.fetchval.await_args_list[-1]
    chunk_key_2 = second_call.args[10]

    # SAME file + SAME chunk_num + SAME owner/namespace + SAME
    # content → SAME key. (Round-70 added a content digest to the
    # chunk_key derivation so revised files with same name don't
    # silently return the old row.)
    assert chunk_key_1 == chunk_key_2, (
        f"chunk_key not stable across imports: "
        f"{chunk_key_1!r} vs {chunk_key_2!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_content_digest(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Same filename + same chunk_num + DIFFERENT content must
    produce a DIFFERENT chunk_key. Codex review-8 of round-68
    caught that without a content digest in the key, a user re-
    importing an updated version of the same file would hit the
    OLD row's chunk_key, the no-op SET would NOT actually update
    content/metadata, and the API would silently return the OLD
    memory_id while presenting it as a fresh import.

    Round-70 includes ``sha256(chunk["content"])`` in the
    chunk_key derivation. Two imports of files with the same
    filename but different content must therefore hit different
    chunk_keys and create separate rows.
    """
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    # First import: original content.
    mock_importer_v1 = MagicMock()
    mock_importer_v1.parse_document.return_value = (
        "v1 text",
        {"source_file": "draft.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk",
                "content": "original draft content",
                "metadata": {"source_file": "draft.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer_v1
    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("draft.pdf", b"%PDF-1.4\nv1")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_v1 = mock_conn.fetchval.await_args_list[-1].args[10]

    # Second import: SAME filename, SAME chunk_num, DIFFERENT
    # content (the user revised the document).
    mock_importer_v2 = MagicMock()
    mock_importer_v2.parse_document.return_value = (
        "v2 text",
        {"source_file": "draft.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk",
                "content": "revised draft content with new wording",
                "metadata": {"source_file": "draft.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer_v2
    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("draft.pdf", b"%PDF-1.4\nv2")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_v2 = mock_conn.fetchval.await_args_list[-1].args[10]

    assert chunk_key_v1 != chunk_key_v2, (
        f"chunk_key did not change for revised content — same key "
        f"would silently return the stale row on import: "
        f"v1={chunk_key_v1!r} v2={chunk_key_v2!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_unconfirmed_memory_ids_uses_canonical_id_after_conflict(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Conflict path returned the canonical existing-row id; then
    ``conn.transaction().__aexit__`` raised an infra error before
    commit-ack. The ``unconfirmed_memory_ids`` payload must
    contain the CANONICAL id (the existing row's id from
    RETURNING), NOT the surrogate ``new_memory_id()`` that was
    never actually written.

    Codex review-8 of round-68 caught that the helper kept
    ``in_flight_id`` pinned to the surrogate even after fetchval
    returned a different canonical id. On the conflict path,
    the client's reconciliation target was a memory_id that
    Postgres never wrote — useless for the
    ``GET /v1/memories/{id}`` follow-up the unconfirmed-list
    contract is built around.

    Round-70 promotes ``in_flight_id = canonical_id`` immediately
    after fetchval returns; so even when __aexit__ raises infra,
    the surfaced unconfirmed id is the existing row's id and the
    client can query it productively.
    """
    import asyncio as _asyncio

    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()

    class _FailingOuterExitTx:
        """Outer transaction whose __aexit__ raises commit-ack
        timeout to simulate real infra loss at commit time."""

        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *exc_info):
            raise _asyncio.TimeoutError("simulated commit-ack timeout")

    class _CleanSavepointTx:
        """Round-74's nested savepoint around the legacy UPDATE.
        Real Postgres doesn't raise commit-ack errors on
        SAVEPOINT release — that only happens on the outer
        COMMIT — so this exits cleanly."""

        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *exc_info):
            return False

    transaction_count = {"n": 0}

    def _mock_transaction():
        transaction_count["n"] += 1
        # First call → outer (will fail on exit); subsequent
        # → savepoints (clean).
        if transaction_count["n"] == 1:
            return _FailingOuterExitTx()
        return _CleanSavepointTx()

    mock_conn.transaction = MagicMock(side_effect=_mock_transaction)
    _wire_pool_manager(monkeypatch, mock_conn)

    canonical_id_from_existing_row = "mem_canonical_existing_row"
    # Round-74: with the savepoint pattern, the legacy UPDATE
    # runs inside the nested transaction. Default fetchval
    # echoes args[1] (surrogate id) on UPDATE → would set
    # legacy_id=surrogate. We want a CONFLICT path test, so
    # force fetchval to return the canonical_id for any
    # query.
    mock_conn.fetchval.side_effect = None
    mock_conn.fetchval.return_value = canonical_id_from_existing_row

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["memories_created"] == 0
    assert payload["memory_ids"] == []
    unconfirmed = payload.get("unconfirmed_memory_ids") or []
    assert len(unconfirmed) == 1
    # The id surfaced must be the CANONICAL id (the existing
    # row's id from RETURNING), not the surrogate.
    assert unconfirmed[0] == canonical_id_from_existing_row, (
        f"unconfirmed_memory_ids contained the surrogate "
        f"new_memory_id() instead of the canonical RETURNING id: "
        f"got {unconfirmed[0]!r}, expected "
        f"{canonical_id_from_existing_row!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_permission_mode(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Same filename + same content + DIFFERENT permission_mode
    must produce a DIFFERENT chunk_key.

    Codex review-9 of round-70 caught a privacy-downgrade
    hazard: pre-fix, a user uploading identical bytes under
    permission_mode=600 after a 644 import would hit the OLD
    row's chunk_key, the no-op SET would NOT update
    permission_mode, and the API would return the OLD memory_id
    with permission_mode=644 while presenting it as a fresh
    import. The world/federation-readable ACL would silently
    persist on a memory the user just tried to make private.

    Round-71 includes ``str(perm_mode)`` in the chunk_key so
    different permission_modes produce different keys and
    distinct rows.
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    # First import: permission_mode=644 (world-readable).
    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "644"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_644 = mock_conn.fetchval.await_args_list[-1].args[10]

    # Second import: SAME bytes, SAME content, but
    # permission_mode=600 (private).
    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "600"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_600 = mock_conn.fetchval.await_args_list[-1].args[10]

    assert chunk_key_644 != chunk_key_600, (
        f"chunk_key did not change for different permission_mode "
        f"— a tighter ACL re-import would silently keep the "
        f"old permissive permission: 644={chunk_key_644!r} "
        f"600={chunk_key_600!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_category(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Different category for the same content → different
    chunk_key. Round-71 closure of the same codex review-9
    finding for the category dimension. A user reorganizing a
    document into a different category should NOT silently get
    the old categorization back via ON CONFLICT.
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)
    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_documents = mock_conn.fetchval.await_args_list[-1].args[10]

    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "research", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_research = mock_conn.fetchval.await_args_list[-1].args[10]

    assert chunk_key_documents != chunk_key_research, (
        f"chunk_key did not change for different category: "
        f"documents={chunk_key_documents!r} "
        f"research={chunk_key_research!r}"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_resolves_legacy_v70_chunk_key_to_canonical_id(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Round-72: when an alpha deployment upgrades from
    round-70 to round-71+, rows already written under the
    round-70 chunk_key shape (no permission_mode / category /
    subcategory) need to be resolved by the legacy key shape
    BEFORE the new INSERT-with-ON-CONFLICT runs. Otherwise the
    new key shape misses the legacy row and creates a
    duplicate.

    Codex review-10 of round-71 caught the upgrade-path
    regression. The fix runs an UPDATE...RETURNING id that
    looks up the legacy v70 key and migrates the row to the
    new key. If the UPDATE returns a row id, the helper uses
    that as canonical and skips the INSERT path.

    This test simulates the legacy-row-found path: the UPDATE
    fetchval returns a canonical id; the helper must surface
    THAT id in memory_ids (no surrogate / no duplicate INSERT).
    """
    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_TxCtx())
    _wire_pool_manager(monkeypatch, mock_conn)

    # Override fetchval to recognize the UPDATE and return a
    # canonical id (legacy row found and migrated). The default
    # _wire_pool_manager fetchval returns None for UPDATEs; this
    # test overrides to simulate the alpha-upgrade scenario.
    legacy_canonical_id = "mem_legacy_row_already_existed"

    async def _fetchval_legacy_found(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            return legacy_canonical_id
        # New INSERT path should NOT run when legacy_id is
        # returned. Return a sentinel that, if it ever surfaced
        # in memory_ids, would obviously be wrong.
        return "mem_should_never_reach_insert"

    mock_conn.fetchval.side_effect = _fetchval_legacy_found

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert payload["memory_ids"] == [legacy_canonical_id], (
        f"helper did not surface the legacy canonical id; "
        f"got {payload['memory_ids']!r}, expected "
        f"[{legacy_canonical_id!r}]"
    )

    # Verify the helper actually called the UPDATE query and
    # then SHORT-CIRCUITED — the INSERT-with-ON-CONFLICT must
    # NOT have fired.
    update_calls = [
        call for call in mock_conn.fetchval.await_args_list
        if isinstance(call.args[0], str)
        and call.args[0].lstrip().upper().startswith("UPDATE")
    ]
    insert_calls = [
        call for call in mock_conn.fetchval.await_args_list
        if isinstance(call.args[0], str)
        and call.args[0].lstrip().upper().startswith("INSERT")
    ]
    assert len(update_calls) == 1, (
        f"expected exactly 1 UPDATE legacy-resolution call, "
        f"got {len(update_calls)}"
    )
    assert len(insert_calls) == 0, (
        f"INSERT-with-ON-CONFLICT must NOT fire when legacy "
        f"row was found and migrated; got {len(insert_calls)} "
        f"INSERT calls"
    )


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_legacy_update_falls_through_to_on_conflict_on_unique_violation(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Round-74: when the round-72 legacy UPDATE hits a
    UniqueViolationError on the import_chunk_key constraint,
    the helper must roll back the SAVEPOINT (nested
    ``conn.transaction()``) and fall through to the standard
    INSERT-with-ON-CONFLICT path. The ON CONFLICT path then
    hits the new-key row via the unique index and returns its
    canonical id.

    Codex review-12 of round-73 caught that catching the
    violation inside the OUTER transaction left it in
    Postgres' aborted state — the next fetchval would raise
    ``InFailedSQLTransactionError``. Round-74 wraps the UPDATE
    in a nested ``conn.transaction()`` (asyncpg savepoint) so
    a violation rolls back ONLY the savepoint, leaving the
    outer transaction usable for the INSERT.

    The mock models the abort behavior: after a
    UniqueViolationError, subsequent fetchval calls on the
    SAME (outer) transaction would raise
    InFailedSQLTransactionError. The savepoint pattern
    prevents that. We verify by checking that the helper
    created TWO transactions (outer + nested savepoint)
    rather than one, and that the INSERT runs cleanly after
    the violation.
    """
    import asyncpg

    _stub_importer_with_chunks(mock_importer_class, n=1)
    mock_conn = AsyncMock()

    # Track whether the OUTER transaction is in a "post-
    # violation aborted" state. The nested savepoint pattern
    # should keep the outer txn out of that state. If the
    # helper accidentally catches the violation in the outer
    # txn, the next fetchval should see this aborted flag and
    # raise InFailedSQLTransactionError to mirror Postgres'
    # real behavior.
    state = {"outer_aborted": False}
    transaction_call_count = {"n": 0}

    class _OuterTxCtx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *exc_info):
            return None

    class _SavepointTxCtx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, exc_type, exc_value, _tb):
            # If the savepoint exited via UniqueViolationError,
            # the savepoint rolls back but the OUTER txn stays
            # usable — that's the savepoint contract. Return
            # False so the exception still propagates to
            # Python's ``except``, but mark the outer as
            # NOT aborted (savepoint absorbed it).
            return False

    def _mock_transaction():
        transaction_call_count["n"] += 1
        # First call → outer transaction. Subsequent → savepoint.
        if transaction_call_count["n"] == 1:
            return _OuterTxCtx()
        return _SavepointTxCtx()

    mock_conn.transaction = MagicMock(side_effect=_mock_transaction)
    _wire_pool_manager(monkeypatch, mock_conn)

    insert_canonical_id = "mem_canonical_from_on_conflict_path"
    update_call_count = {"n": 0}
    insert_call_count = {"n": 0}

    async def _fetchval_simulating_unique_violation(*args, **kwargs):
        sql = args[0] if args else ""
        if not isinstance(sql, str):
            return None
        sql_stripped = sql.lstrip().upper()

        # If the helper ever tries an INSERT/UPDATE while the
        # outer transaction is in an aborted state, mirror
        # Postgres by raising InFailedSQLTransactionError. The
        # savepoint pattern should prevent this.
        if state["outer_aborted"]:
            raise asyncpg.InFailedSQLTransactionError(
                "current transaction is aborted, commands "
                "ignored until end of transaction block"
            )

        if sql_stripped.startswith("UPDATE"):
            update_call_count["n"] += 1
            # Only the savepoint should be active when this
            # raises — verify by inspecting the transaction
            # call count. If it's exactly 2 (outer + savepoint
            # entered), we're inside the savepoint. If it's
            # 1, the helper didn't open a savepoint and the
            # violation will hit the outer txn (then the
            # state["outer_aborted"] flag flips and the next
            # fetchval fails).
            if transaction_call_count["n"] < 2:
                state["outer_aborted"] = True
            raise asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint "
                "\"memories_import_chunk_key_uniq\""
            )
        if sql_stripped.startswith("INSERT"):
            insert_call_count["n"] += 1
            return insert_canonical_id
        return None

    mock_conn.fetchval.side_effect = _fetchval_simulating_unique_violation

    monkeypatch.setattr(
        "mnemos.webhooks.dispatcher.dispatch",
        AsyncMock(return_value=[]),
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, (
        f"unique-violation on legacy UPDATE must fall through "
        f"to ON CONFLICT, NOT bubble as a content error — got "
        f"{resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert payload["memory_ids"] == [insert_canonical_id]
    # Helper opened TWO transactions (outer + savepoint), not
    # one. This pins the savepoint pattern statically.
    assert transaction_call_count["n"] >= 2, (
        f"helper did not open a nested savepoint around the "
        f"legacy UPDATE — outer transaction would be aborted "
        f"by Postgres after the violation. Saw "
        f"{transaction_call_count['n']} ``conn.transaction()`` "
        f"calls; expected ≥ 2."
    )
    assert update_call_count["n"] == 1
    assert insert_call_count["n"] == 1, (
        f"INSERT-with-ON-CONFLICT did not fire after the "
        f"legacy UPDATE's savepoint rolled back: "
        f"{insert_call_count['n']} INSERT calls"
    )

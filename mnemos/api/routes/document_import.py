"""Document import utilities using Docling for intelligent content extraction."""
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

try:
    from docling.document_converter import DocumentConverter
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.api.routes.memories import _validate_permission_mode
from mnemos.core.ids import new_memory_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/documents", tags=["document-import"])


class DoclingImporter:
    """Handles document parsing and memory extraction via Docling."""

    def __init__(self):
        if not DOCLING_AVAILABLE:
            raise ImportError("Docling not installed. Install with: pip install mnemos-os[docling]")
        self.converter = DocumentConverter()

    def parse_document(
        self, file_content: bytes, filename: str
    ) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
        """Parse document and extract content, metadata, and chunks.

        Returns:
            (full_text, metadata, chunks) where chunks are memory-sized segments
        """
        try:
            # Parse document with Docling
            doc = self.converter.convert_bytes(
                file_content,
                file_name=filename,
                format_hint=self._guess_format(filename),
            )

            # Extract full text
            full_text = doc.document.export_to_markdown()

            # Extract metadata
            metadata = {
                "source_file": filename,
                "source_type": self._get_document_type(filename),
                "parsed_at": datetime.utcnow().isoformat(),
                "page_count": len(doc.pages) if hasattr(doc, "pages") else None,
            }

            # Create memory chunks (split by semantic boundaries)
            chunks = self._chunk_content(
                full_text, metadata, doc
            )

            logger.info(
                f"[DOCLING] Parsed {filename}: {len(full_text)} chars, "
                f"{len(chunks)} chunks, {metadata.get('page_count', '?')} pages"
            )

            return full_text, metadata, chunks

        except Exception as e:
            logger.error(f"[DOCLING] Parse error for {filename}: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Document parsing failed: {str(e)}"
            )

    def _guess_format(self, filename: str) -> str:
        """Guess document format from filename."""
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        format_map = {
            "pdf": "pdf",
            "docx": "docx",
            "doc": "docx",
            "pptx": "pptx",
            "ppt": "pptx",
            "xlsx": "xlsx",
            "xls": "xlsx",
            "txt": "txt",
            "md": "md",
            "html": "html",
        }
        return format_map.get(ext, "auto")

    def _get_document_type(self, filename: str) -> str:
        """Extract document type from filename."""
        ext = filename.lower().split(".")[-1] if "." in filename else "unknown"
        type_map = {
            "pdf": "PDF",
            "docx": "Word Document",
            "doc": "Word Document",
            "pptx": "PowerPoint",
            "ppt": "PowerPoint",
            "xlsx": "Excel Spreadsheet",
            "xls": "Excel Spreadsheet",
            "txt": "Text File",
            "md": "Markdown",
            "html": "HTML",
        }
        return type_map.get(ext, "Unknown")

    def _chunk_content(
        self,
        text: str,
        metadata: Dict[str, Any],
        doc: Any,
    ) -> List[Dict[str, Any]]:
        """Split document content into memory-sized chunks with semantic awareness."""
        chunks = []
        target_chunk_size = 1500  # ~500 tokens, typical memory unit

        # Try to use document structure if available
        sections = self._extract_sections(text, doc)

        current_chunk = ""
        current_metadata = metadata.copy()
        chunk_num = 0

        for section_title, section_text in sections:
            if len(current_chunk) + len(section_text) > target_chunk_size:
                if current_chunk:
                    chunks.append({
                        "chunk_num": chunk_num,
                        "title": section_title or current_metadata.get("chunk_title", ""),
                        "content": current_chunk.strip(),
                        "metadata": {**current_metadata, "chunk_num": chunk_num},
                    })
                    chunk_num += 1
                current_chunk = section_text
            else:
                current_chunk += f"\n{section_text}" if current_chunk else section_text

        # Final chunk
        if current_chunk:
            chunks.append({
                "chunk_num": chunk_num,
                "title": sections[-1][0] if sections else "Content",
                "content": current_chunk.strip(),
                "metadata": {**current_metadata, "chunk_num": chunk_num},
            })

        return chunks

    def _extract_sections(
        self,
        text: str,
        doc: Any,
    ) -> List[Tuple[str, str]]:
        """Extract hierarchical sections from document for better chunking."""
        sections = []

        # Simple heuristic: split by markdown headings
        lines = text.split("\n")
        current_section = ""
        current_title = ""

        for line in lines:
            if line.startswith("#"):
                if current_section:
                    sections.append((current_title, current_section))
                current_title = line.lstrip("#").strip()
                current_section = ""
            else:
                current_section += f"{line}\n"

        if current_section:
            sections.append((current_title, current_section))

        return sections if sections else [("", text)]


async def import_memories_from_document(
    file: UploadFile,
    category: str = Form("documents"),
    subcategory: Optional[str] = Form(None),
    permission_mode: Optional[int] = Form(None),
    user: UserContext = Depends(get_current_user),
    *,
    route_label: str = "POST /v1/documents/import",
) -> Tuple[Dict[str, Any], int]:
    """Import document into MNEMOS as memory records.

    Creates one memory per document chunk with automatic metadata extraction.
    Requires docling extra: pip install mnemos-os[docling]

    Returns ``(payload, status_code)`` where ``status_code`` is:
      * 200 — every chunk committed.
      * 207 — partial: some committed, some rolled back. ``errors``
        list itemises the failed chunks.
      * 502 — total: zero chunks committed (content failures only).
      * 503 — infrastructure failure (asyncpg connection family,
        asyncio.TimeoutError) at acquire time or mid-loop. Payload
        preserves ``memory_ids`` for chunks that committed before
        the failure. ``unconfirmed_memory_ids`` (when present)
        names chunk IDs whose INSERT was accepted but commit-ack
        was lost — see the ``unconfirmed_memory_ids`` block below
        for the operator-honest retry contract.

    A SINGLE ``GET /v1/memories/{id}`` read is NOT a safe rollback
    oracle for ``unconfirmed_memory_ids`` taken in isolation —
    Postgres MVCC can show 404 while the original transaction is
    still resolving. Round-68 shipped the stable-chunk-identity
    primitive (``import_chunk_key`` + UNIQUE + ``ON CONFLICT
    DO UPDATE RETURNING id``), so a client retry of the same
    file is safe regardless of the read outcome: the conflict
    path returns the canonical id and no duplicate row is
    created. ``unconfirmed_memory_ids`` is therefore now a hint
    for clients that want to query immediately, not the only
    safe-retry primitive.

    Returning a tuple instead of a Response keeps the helper
    sharable between the single-file ``/import`` route (which
    wraps in JSONResponse) and the multi-file ``/batch-import``
    route (which appends ``{**payload, "status_code": ...}`` per
    file). Codex round-2 of round-47 caught that the previous
    JSONResponse return leaked Response internals into the batch
    response shape.

    ``route_label`` is keyword-only so each caller surfaces the
    correct edge-profile 503 detail. The previous hard-coded
    ``"POST /v1/import/document"`` named a non-existent path
    (the actual routes are ``POST /v1/documents/import`` and
    ``POST /v1/documents/batch-import``) — codex caught this
    in the round-61 review of the round-54..60 503-helper sweep.
    The pool check is intentionally redundant with the batch
    endpoint's own pre-loop ``require_postgres_pool_or_503`` call:
    the batch's pre-loop check escapes the per-file try/except so
    operators on edge profiles see a top-level 503 with the batch
    route label, not a 207 body burying SQLite-incompatibility 503s
    in per-file results.
    """
    perm_mode = _validate_permission_mode(permission_mode, default=600)

    # Pool check FIRST — an edge-profile / SQLite deployment can't
    # serve this route regardless of whether Docling is installed,
    # so 503 with a route-named profile-aware detail is the more
    # informative top-level signal than a Docling 501 (which would
    # send operators chasing the wrong dependency). Codex round-61
    # review of the round-54..60 sweep called this out.
    require_postgres_pool_or_503(route_label=route_label)

    if not DOCLING_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="Docling not installed. Install with: pip install mnemos-os[docling]"
        )

    # Read file
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # Parse with Docling
    importer = DoclingImporter()
    full_text, doc_metadata, chunks = importer.parse_document(
        content, file.filename or "document"
    )

    # Create memories from chunks. Match the canonical /v1/memories create
    # path exactly: timestamped `mem_...` id, populate verbatim_content +
    # quality_rating + permission_mode, and fire `memory.created` webhooks +
    # search-cache invalidation so document-imported memories behave like
    # any other for downstream subscribers and search consumers. The
    # AFTER INSERT trigger writes memory_versions v1 automatically.
    memory_ids = []
    errors = []
    # Webhook delivery_ids accumulated INSIDE the per-chunk
    # transactions; the post-commit loop below schedules send
    # tasks for each. This is the transactional-outbox pattern:
    # the webhook_deliveries row is atomic with the memory INSERT
    # (so a failure rolls back BOTH and we don't get
    # phantom-event-without-data or vice versa), then the send
    # task is dispatched only after commit so a failed-to-fire
    # send doesn't lose the event — the durable outbox row stays
    # 'pending' for the worker to pick up. Closes
    # corpus-review-2026-04-29 #2 for this route.
    pending_delivery_ids: list[str] = []

    from mnemos.core.pool import is_infrastructure_error
    from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook

    # Infrastructure errors (asyncio.TimeoutError + asyncpg
    # connection family) at acquire time OR mid-loop AFTER one or
    # more chunks have committed must surface as 503 WITHOUT
    # discarding the committed-chunks payload. Codex round-3 of
    # round-62 caught that the round-63 form raised a bare
    # HTTPException(503) on infra failure, which the batch
    # endpoint's ``except HTTPException`` path then re-shaped into
    # a per-file entry with ``memories_created=0`` and no
    # ``memory_ids`` — the body lied about work that had already
    # been committed, so a retry-aware client could re-import the
    # same chunks (memory IDs from ``new_memory_id()`` are
    # non-deterministic). Returning ``(payload, 503)`` instead
    # preserves the committed memory_ids in the response body so
    # operators can reconcile partial commits.
    #
    # ``pool_manager.acquire()`` returns an async context manager;
    # the connection acquire happens at ``__aenter__`` time, so the
    # try/except wraps the ``async with`` block, NOT the
    # ``acquire()`` call itself.
    infra_failure: Optional[Exception] = None
    # Commit-ambiguous IDs: the chunk's INSERT statement was
    # accepted by the server but ``conn.transaction().__aexit__``
    # raised an infra-class error before / during commit-ack. The
    # row may or may not exist on disk; the only honest answer is
    # to surface the ID to the client so it can query
    # ``GET /v1/memories/{id}`` (or any read endpoint) and
    # reconcile before retrying. Codex round-4 of round-62 caught
    # that the previous shape lost commit-ambiguous IDs entirely,
    # forcing retry-aware clients to risk duplicate imports.
    #
    # Round-68 closes the safe-retry loop with the
    # ``import_chunk_key`` primitive (migrations_v4_2_document
    # _import_chunk_idempotency.sql): each chunk gets a stable
    # sha256-derived key from
    # ``(owner_id, namespace, source_file, chunk_num)`` and the
    # INSERT uses ``ON CONFLICT (import_chunk_key) DO UPDATE ...
    # RETURNING id``. A retry of the same chunk hits the existing
    # row, returns its canonical id via RETURNING, and creates no
    # duplicate. ``unconfirmed_memory_ids`` is still surfaced for
    # operators on deployments that haven't applied the migration
    # yet (or for clients that want to track in-flight IDs
    # explicitly), but with the migration in place the retry path
    # itself is safe.
    unconfirmed_memory_ids: list[str] = []

    def _chunk_key_v70(chunk_num: int, content: str) -> str:
        """Round-70 chunk-key shape (PRE-permission_mode/category).

        Kept as a legacy-resolution helper for round-72. After an
        alpha upgrade from round-70 to round-71+, rows written
        with this key shape are still in the database under a
        chunk_key that the v71 derivation will not match. The
        round-72 path detects them and migrates them to the
        current key shape before the INSERT-with-ON-CONFLICT
        runs, so retry-aware clients don't get duplicate rows on
        the FIRST post-upgrade retry.

        Codex review-10 of round-71 caught this upgrade-path
        regression — the new key shape is correct but the legacy
        rows wouldn't match it.
        """
        content_digest = hashlib.sha256(
            (content or "").encode("utf-8")
        ).hexdigest()
        return hashlib.sha256(
            (
                (user.user_id or "")
                + "\x00"
                + (user.namespace or "")
                + "\x00"
                + (file.filename or "")
                + "\x00"
                + str(chunk_num)
                + "\x00"
                + content_digest
            ).encode("utf-8")
        ).hexdigest()

    def _chunk_key(chunk_num: int, content: str) -> str:
        """Stable per-document-revision chunk identity for ON CONFLICT.

        Components: ``owner_id`` + ``namespace`` + ``filename`` +
        ``chunk_num`` + ``sha256(content)`` + ``permission_mode``
        + ``category`` + ``subcategory``. The content digest binds
        the key to a specific document revision; the
        permission_mode + category + subcategory triple binds the
        key to the exact ACL/categorization the caller asked for.

        Codex review-8 of round-68 caught that omitting the
        content digest let revised same-name files silently
        return stale rows; codex review-9 of round-70 caught that
        omitting permission_mode let a user re-import the same
        bytes under a tighter permission_mode (e.g. 644 → 600)
        and silently keep the OLD permissive ACL — a privacy
        downgrade hidden behind a "memory created" response.
        Including all semantically-significant import parameters
        in the key makes truly-identical retries match (correct
        dedup) and any change to content / ACL / categorization
        creates a new row.

        sha256 with NUL separators avoids splice ambiguity on
        filenames containing characters that might appear in
        the other components (e.g. ``:`` in user_id, ``/`` in
        source_file). The key is opaque to the rest of the
        system; only the document_import surface writes or reads
        it, gated by the unique index from migrations
        _v4_2_document_import_chunk_idempotency.sql.
        """
        content_digest = hashlib.sha256(
            (content or "").encode("utf-8")
        ).hexdigest()
        return hashlib.sha256(
            (
                (user.user_id or "")
                + "\x00"
                + (user.namespace or "")
                + "\x00"
                + (file.filename or "")
                + "\x00"
                + str(chunk_num)
                + "\x00"
                + content_digest
                + "\x00"
                + str(perm_mode)
                + "\x00"
                + (category or "")
                + "\x00"
                + (subcategory or "")
            ).encode("utf-8")
        ).hexdigest()
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            for chunk in chunks:
                chunk_delivery_ids: list[str] = []
                memory_id = new_memory_id()
                # Pre-allocate the ID outside the per-chunk try so
                # the outer infra-catch below can see it and decide
                # whether to surface as committed / unconfirmed.
                in_flight_id = memory_id
                try:
                    chunk_metadata = {
                        **doc_metadata,
                        **chunk["metadata"],
                        "chunk_title": chunk["title"],
                    }

                    chunk_key = _chunk_key(chunk["chunk_num"], chunk["content"])
                    chunk_key_legacy_v70 = _chunk_key_v70(
                        chunk["chunk_num"], chunk["content"]
                    )
                    canonical_id: Optional[str] = None
                    async with conn.transaction():
                        # Round-72 legacy-key resolution: if a row
                        # exists in this caller's (owner, namespace)
                        # under the round-70 key shape AND its
                        # stored permission_mode + category +
                        # subcategory match the current request's
                        # ACL/categorization, atomically migrate it
                        # to the v71 key shape and use that row.
                        # Codex review-10 of round-71 caught that
                        # without this step, an alpha upgrade would
                        # lose idempotency for already-committed
                        # imports.
                        #
                        # The UPDATE can fail with a unique-violation
                        # if the new chunk_key row ALREADY exists
                        # (concurrent retry, rolling deploy, prior
                        # duplicate). Codex review-12 of round-73
                        # caught that catching UniqueViolationError
                        # inside the OUTER transaction leaves the
                        # transaction in Postgres' aborted state —
                        # Python's ``except`` doesn't undo the
                        # server-side abort. The very next
                        # ``fetchval`` (the INSERT-with-ON-CONFLICT
                        # path) would raise
                        # ``InFailedSQLTransactionError``. Round-74:
                        # wrap the UPDATE in a NESTED
                        # ``conn.transaction()`` (asyncpg implements
                        # this as a SAVEPOINT). On
                        # UniqueViolationError the savepoint rolls
                        # back, leaving the outer transaction
                        # usable, and we fall through to the
                        # ON CONFLICT path. The catch is narrowed
                        # to the ``memories_import_chunk_key_uniq``
                        # constraint so it doesn't swallow
                        # unrelated unique violations (e.g., a
                        # ``memory_id`` PK collision should still
                        # bubble as a content error).
                        legacy_id = None
                        try:
                            async with conn.transaction():
                                legacy_id = await conn.fetchval(
                                    "UPDATE memories "
                                    "SET import_chunk_key = $1 "
                                    "WHERE import_chunk_key = $2 "
                                    "  AND owner_id = $3 "
                                    "  AND namespace = $4 "
                                    "  AND permission_mode = $5 "
                                    "  AND category IS NOT DISTINCT FROM $6 "
                                    "  AND subcategory IS NOT DISTINCT FROM $7 "
                                    "RETURNING id",
                                    chunk_key,
                                    chunk_key_legacy_v70,
                                    user.user_id,
                                    user.namespace,
                                    perm_mode,
                                    category,
                                    subcategory,
                                )
                        except asyncpg.UniqueViolationError as uv:
                            # Filter to the import_chunk_key
                            # constraint specifically. asyncpg
                            # surfaces the constraint name on the
                            # exception in modern versions; older
                            # paths only carry it in the message
                            # text. Check both so we don't swallow
                            # an unrelated unique violation
                            # (e.g., a ``memory_id`` PK collision
                            # — that should bubble as a content
                            # error so the operator sees the
                            # actual data problem). Re-raising
                            # propagates out of the savepoint
                            # context which has already rolled
                            # back, then the outer except handles
                            # it as a content failure.
                            constraint_name = getattr(
                                uv, "constraint_name", None
                            ) or ""
                            message = str(uv)
                            is_chunk_key_uniq = (
                                constraint_name
                                == "memories_import_chunk_key_uniq"
                                or "memories_import_chunk_key_uniq"
                                in message
                            )
                            if not is_chunk_key_uniq:
                                raise
                            # Savepoint rolled back; outer
                            # transaction is still good. Fall
                            # through to ON CONFLICT below.
                            legacy_id = None
                        if legacy_id is not None:
                            # Legacy row migrated. No INSERT, no
                            # webhook dispatch — the row already
                            # exists with its original
                            # ``memory.created`` event having
                            # fired on the original write.
                            canonical_id = legacy_id
                            in_flight_id = canonical_id
                            chunk_delivery_ids = []
                            logger.debug(
                                f"[DOCLING] Resolved legacy v70 chunk_key "
                                f"to canonical {canonical_id} "
                                f"(chunk={chunk['chunk_num']})"
                            )
                        else:
                            # ON CONFLICT (import_chunk_key) DO
                            # UPDATE SET import_chunk_key =
                            # EXCLUDED.import_chunk_key is the
                            # round-68 idempotency primitive: a
                            # no-op SET because Postgres
                            # ``DO UPDATE`` requires updating at
                            # least one column. The
                            # ``mnemos_version_snapshot()`` AFTER
                            # UPDATE trigger only writes to
                            # ``memory_versions`` when audited
                            # fields are IS DISTINCT FROM their
                            # previous values; ``import_chunk_key``
                            # is not in that audited set, so a
                            # true no-op SET produces no version-
                            # row churn. ``RETURNING id`` returns
                            # the canonical row id — the existing
                            # row's id on conflict, the newly-
                            # inserted row's id otherwise.
                            canonical_id = await conn.fetchval(
                                "INSERT INTO memories "
                                "(id, content, category, subcategory, metadata, quality_rating, "
                                " verbatim_content, owner_id, namespace, permission_mode, "
                                " import_chunk_key) "
                                "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10) "
                                "ON CONFLICT (import_chunk_key) DO UPDATE "
                                "  SET import_chunk_key = EXCLUDED.import_chunk_key "
                                "RETURNING id",
                                memory_id,
                                chunk["content"],
                                category,
                                subcategory,
                                json.dumps(chunk_metadata),
                                chunk["content"],            # verbatim_content == content for chunks
                                user.user_id,
                                user.namespace,
                                perm_mode,
                                chunk_key,
                            )
                            if canonical_id is None:
                                # Defensive: fetchval should never
                                # return NULL given RETURNING id
                                # with a NOT NULL primary key.
                                # Fall back to the surrogate id we
                                # generated — at worst this
                                # matches pre-round-68 behavior
                                # for this single chunk.
                                canonical_id = memory_id
                            # Promote in_flight_id to canonical so
                            # __aexit__ infra failure surfaces the
                            # right id in unconfirmed_memory_ids
                            # (codex review-8 of round-68).
                            in_flight_id = canonical_id
                            # Webhook delivery rows go into the
                            # SAME transaction as the memory
                            # INSERT.
                            chunk_delivery_ids = await _dispatch_webhook(
                                "memory.created",
                                {
                                    "memory_id": canonical_id,
                                    "category": category,
                                    "subcategory": subcategory,
                                    "content": chunk["content"],
                                    "owner_id": user.user_id,
                                    "namespace": user.namespace,
                                    "source": "document_import",
                                },
                                conn=conn,
                                owner_id=user.user_id,
                                namespace=user.namespace,
                            )
                    # __aexit__ returned cleanly → commit confirmed.
                    memory_ids.append(canonical_id)
                    pending_delivery_ids.extend(str(did) for did in chunk_delivery_ids)
                    logger.debug(
                        f"[DOCLING] Created memory {canonical_id} "
                        f"(chunk_key={chunk_key[:12]}... idem-conflict={canonical_id != memory_id}) "
                        f"from chunk {chunk['chunk_num']}"
                    )

                except Exception as chunk_err:
                    # Infra-class errors mid-chunk (e.g., asyncpg
                    # connection drop after acquire, OR a transaction
                    # __aexit__ commit-ack timeout) escape the
                    # per-chunk catch so they reach the outer
                    # try/except below. The in-flight ID is captured
                    # as unconfirmed before re-raising; the outer
                    # handler returns ``(payload, 503)`` with both
                    # ``memory_ids`` (definitely committed) AND
                    # ``unconfirmed_memory_ids`` (commit ambiguous —
                    # client must query ``GET /v1/memories/{id}`` to
                    # reconcile).
                    if is_infrastructure_error(chunk_err):
                        unconfirmed_memory_ids.append(in_flight_id)
                        raise
                    logger.error(
                        f"[DOCLING] Failed to create memory for chunk {chunk['chunk_num']}: {chunk_err}"
                    )
                    errors.append({"chunk": chunk["chunk_num"], "error": str(chunk_err)})
    except HTTPException:
        # Already shaped — don't re-wrap.
        raise
    except Exception as acquire_err:
        if is_infrastructure_error(acquire_err):
            # Capture for post-loop processing — return a
            # 503-shaped payload that preserves committed chunks.
            infra_failure = acquire_err
            errors.append({
                "chunk": None,
                "error": f"infrastructure error: {acquire_err}",
            })
        else:
            raise

    # Side-effects done outside the per-chunk acquire so the connection isn't
    # held while we contact Redis / schedule webhook send tasks. Cache is
    # invalidated ONCE after the whole document — a 100-chunk document would
    # otherwise thrash search cache N times. Send tasks for the deliveries
    # whose outbox rows are already committed get scheduled here.
    if memory_ids:
        if _lc._cache:
            try:
                await _lc._cache.delete("stats:global")
                try:
                    async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                        await _lc._cache.delete(_k)
                except Exception:
                    pass
            except Exception:
                pass
        if pending_delivery_ids:
            try:
                from mnemos.core.lifecycle import _schedule_delivery_attempt
                from mnemos.webhooks.sender import _attempt_delivery
                for delivery_id in pending_delivery_ids:
                    _schedule_delivery_attempt(_attempt_delivery(delivery_id))
            except Exception:
                # Schedule failure is benign — the delivery rows
                # are already committed in webhook_deliveries with
                # status='pending'; the recovery worker will pick
                # them up on its next pass.
                logger.warning(
                    "document_import: send-task scheduling failed for %d "
                    "deliveries (recovery worker will pick them up)",
                    len(pending_delivery_ids), exc_info=True,
                )

    payload = {
        "source_file": file.filename,
        "memories_created": len(memory_ids),
        "memory_ids": memory_ids,
        "chunks_processed": len(chunks),
        "errors": errors,
        "metadata": doc_metadata,
        "total_text_length": len(full_text),
    }
    # ``unconfirmed_memory_ids`` is populated only when an
    # infra-class error fires AFTER the chunk's INSERT statement
    # was accepted but BEFORE / during commit-ack. The row may or
    # may not have committed — the helper cannot tell from the
    # client side.
    #
    # Round-68 shipped the stable-chunk-identity primitive
    # (``import_chunk_key`` + partial UNIQUE + ``ON CONFLICT
    # (import_chunk_key) DO UPDATE ... RETURNING id``), so a
    # client retry of the same import will hit the existing row
    # via ON CONFLICT and the canonical id surfaces in
    # ``memory_ids`` on the retry response. The unconfirmed list
    # is still surfaced on the FIRST 503 response so clients can
    # query immediately if they want, but auto-retry is now safe.
    #
    # Pre-round-68 deployments (v4.1.x, or v4.2.0a14 alphas
    # without the migration applied) saw a duplicate-creation
    # hazard on retry because ``new_memory_id()`` is non-
    # deterministic. See DOCUMENT_IMPORT_GUIDE.md for the pre-
    # migration retry contract operators on those deployments
    # should follow.
    if unconfirmed_memory_ids:
        payload["unconfirmed_memory_ids"] = unconfirmed_memory_ids
    # Status-code contract:
    #   * All chunks committed → 200 OK.
    #   * Infra failure (pool acquire / connection drop) at any
    #     point → 503 with committed-chunks payload preserved
    #     (body retains memory_ids + memories_created so retry-
    #     aware clients reconcile partial commits, codex round-3
    #     of round-62).
    #   * Some chunks committed, some rolled back (content errors)
    #     → 207 Multi-Status.
    #   * Zero chunks committed (content errors) → 502 Bad Gateway.
    # Helper returns ``(payload, status_code)``. Caller routes
    # decide how to surface — single-file /import wraps in
    # JSONResponse, multi-file /batch-import folds status_code
    # into each per-file dict.
    if infra_failure is not None:
        return payload, 503
    if errors and not memory_ids:
        return payload, 502
    if errors:
        return payload, 207
    return payload, 200


# Route: POST /v1/documents/import
@router.post("/import", response_model=dict)
async def import_document(
    file: UploadFile = File(...),
    category: str = Form("documents"),
    subcategory: Optional[str] = Form(None),
    permission_mode: Optional[int] = Form(None),
    user: UserContext = Depends(get_current_user),
):
    """Import document file into MNEMOS as memory records.

    Supported formats: PDF, DOCX, PPTX, XLSX, TXT, MD, HTML

    Returns: {
        source_file: filename,
        memories_created: number of memory records,
        memory_ids: list of created memory UUIDs,
        chunks_processed: number of content chunks,
        errors: any chunk-level errors,
        metadata: extracted document metadata,
        total_text_length: total character count
    }
    """
    payload, status_code = await import_memories_from_document(
        file, category, subcategory, permission_mode, user,
        route_label="POST /v1/documents/import",
    )
    if status_code == 200:
        return payload
    return JSONResponse(status_code=status_code, content=payload)


# Route: POST /v1/documents/batch-import
@router.post("/batch-import", response_model=list)
async def batch_import_documents(
    files: List[UploadFile] = File(...),
    category: str = Form("documents"),
    permission_mode: Optional[int] = Form(None),
    user: UserContext = Depends(get_current_user),
):
    """Batch import multiple documents into MNEMOS.

    Returns list of import results (one per document).
    """
    # Validate permission_mode at the batch boundary so an invalid value
    # fails the whole request fast — otherwise the per-file try/except
    # below would swallow the 422 into a per-file error result.
    _validate_permission_mode(permission_mode, default=600)
    # Pre-loop pool check so an edge-profile 503 (SQLite-only deployment
    # being asked to serve a Postgres-only route) escapes uncaught with
    # the BATCH route label, not the per-file one. The per-file
    # ``import_memories_from_document`` call below repeats the check,
    # which is harmless when the pool is up and the right thing to do
    # if it goes down mid-batch (the next iteration would then bubble).
    # Codex caught the original round-56 form folded SQLite-incompat
    # 503s into a 207 body and named a non-existent path
    # ("POST /v1/import/document"); this loop boundary is the operator-
    # honest top-level signal.
    require_postgres_pool_or_503(route_label="POST /v1/documents/batch-import")
    results = []
    has_partial_or_full_failure = False
    for file in files:
        try:
            payload, status_code = await import_memories_from_document(
                file,
                category=category,
                subcategory=None,
                permission_mode=permission_mode,
                user=user,
                route_label="POST /v1/documents/batch-import",
            )
            # Fold the per-file status_code into the result dict
            # so batch clients can distinguish per-file 200 / 207
            # / 502 without inspecting Response internals.
            entry = {**payload, "status_code": status_code}
            results.append(entry)
            if status_code != 200:
                has_partial_or_full_failure = True
        except HTTPException as e:
            results.append({
                "source_file": file.filename,
                "error": e.detail,
                "memories_created": 0,
                "status_code": e.status_code,
            })
            has_partial_or_full_failure = True
    # Top-level batch HTTP status — codex round-3 of round-47
    # caught that "all files had memories_created=0" wasn't the
    # right 502 trigger: empty-file 400s, Docling-missing 501s,
    # database-unavailable 503s would all match that condition
    # and falsely look like retryable gateway failures.
    #
    # Aggregate from per-file ``status_code`` instead:
    #   * 200 — every file fully imported.
    #   * 503 — at least one file hit a deployment-level
    #          unavailability 503 (helper's pool check fired
    #          mid-batch after the pre-loop check passed). The
    #          per-file try/except HTTPException would otherwise
    #          fold the 503 into a 207 body and hide a database
    #          outage behind a success-shaped response (codex
    #          round-2 of round-62).
    #   * 502 — every file had status_code=502 (full batch
    #          import rollback; retryable infrastructure issue).
    #   * 207 — any other mixed failure shape (clients SHOULD
    #          inspect per-file ``status_code`` to decide retry
    #          policy per file; in particular a 4xx in a per-file
    #          entry means "fix the input, don't retry").
    #
    # 503 is checked BEFORE the all-502 path so a single mid-batch
    # 503 escapes correctly even if every other file aborted with
    # 502. The body still preserves per-file results so retry-
    # aware clients can distinguish committed files from rolled-
    # back / unsupported ones.
    if has_partial_or_full_failure:
        per_file_statuses = [
            int(r.get("status_code", 0)) for r in results
        ]
        any_503 = any(code == 503 for code in per_file_statuses)
        all_502 = (
            not any_503
            and len(per_file_statuses) > 0
            and all(code == 502 for code in per_file_statuses)
        )
        if any_503:
            top = 503
        elif all_502:
            top = 502
        else:
            top = 207
        return JSONResponse(
            status_code=top,
            content=results,
        )
    return results

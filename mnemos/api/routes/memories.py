"""Memory CRUD, search, and rehydration endpoints."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

import mnemos.core.lifecycle as _lc
from mnemos.api.content_negotiation import negotiate_narrate_format
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import (
    backend_or_503 as _backend_or_503,
    maybe_set_pg_rls as _maybe_set_pg_rls,
    require_postgres_pool_or_503,
)
from mnemos.core.ids import new_memory_id
from mnemos.core.lifecycle import (
    _get_cache_key,
    _get_embedding,
)
from mnemos.core.security import is_root
from mnemos.core.visibility import handle_trigger_pgerror
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.persistence.base import DuplicateMemoryError
from mnemos.domain.models import (
    BulkCreateRequest,
    BulkCreateResponse,
    MemoryCreateRequest,
    MemoryItem,
    MemoryListResponse,
    MemorySearchRequest,
    MemoryUpdateRequest,
    RehydrationRequest,
    RehydrationResponse,
    row_to_memory as _row_to_memory,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["memories"])
NatsPublishIntent = tuple[str, dict, str]


@asynccontextmanager
async def _rls_context(conn, user: UserContext):
    """Set PostgreSQL session variables for RLS when auth is active.

    Uses ``SELECT set_config(name, $1, true)`` rather than
    ``SET LOCAL <name> = $1`` because Postgres SET syntax does not
    accept bind parameters (the value position must be a literal —
    https://www.postgresql.org/docs/current/sql-set.html). The third
    argument ``true`` makes the binding transaction-local, equivalent
    to SET LOCAL. Same shape as ``maybe_set_pg_rls`` in
    ``mnemos.api.persistence_helpers`` so the two RLS context paths
    cannot drift.
    """
    if _lc._rls_enabled and user.authenticated:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('mnemos.current_user_id', $1, true)",
                user.user_id,
            )
            await conn.execute(
                "SELECT set_config('mnemos.current_role', $1, true)",
                user.role,
            )
            yield conn
    else:
        yield conn


def _validate_permission_mode(value: int | None, *, default: int | None = None) -> int | None:
    """Validate Unix-style octal permission digits stored as an integer."""
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(status_code=422, detail="permission_mode must be an integer")
    if value < 0 or value > 777 or any(digit not in "01234567" for digit in str(value)):
        raise HTTPException(status_code=422, detail="permission_mode must be octal-style 0-777")
    return value


def _read_visibility_for(user: UserContext, *, namespace: str) -> VisibilityFilter:
    """Read-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are pinned. Use when the
    handler has already pinned the namespace explicitly (e.g. on
    create/update) so the same-namespace round-trip doesn't reject.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_read(user, namespace=namespace)


def _mutation_visibility_for(user: UserContext, *, namespace: str | None) -> VisibilityFilter:
    """Mutation-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are owner+namespace pinned.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_mutation(user, namespace=namespace)


def _schedule_outbox_deliveries(delivery_ids: list[str]) -> None:
    """Schedule HTTP send attempts for newly-enqueued outbox rows.

    Called AFTER the writing transaction commits so the delivery
    worker sees a committed row when it runs. ``_attempt_delivery``
    is imported lazily to avoid pulling the webhook subsystem into
    edge-profile cold paths.
    """
    if not delivery_ids:
        return
    from mnemos.webhooks.sender import _attempt_delivery
    for did in delivery_ids:
        _lc._schedule_delivery_attempt(_attempt_delivery(str(did)))


async def _invalidate_caches_after_mutation() -> None:
    """Drop /stats + per-user search cache entries on any memory write."""
    if not _lc._cache:
        return
    try:
        await _lc._cache.delete("stats:global:v2")
        try:
            async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                await _lc._cache.delete(_k)
        except Exception:
            pass
    except Exception:
        pass


def _read_visibility_predicate(
    user: UserContext, start_param_idx: int
) -> tuple[str, list]:
    """Thin adapter over mnemos.core.visibility.read_visibility_predicate
    that takes a UserContext directly. The shared module powers
    list/get here AND the search/rehydrate helpers in
    api/lifecycle.py — single source of truth for the predicate
    that mirrors the v1_multiuser RLS policies.
    """
    from mnemos.core.visibility import read_visibility_predicate
    return read_visibility_predicate(
        user.user_id, list(user.group_ids), start_param_idx,
    )


async def _insert_memory_with_created_webhook(
    *,
    conn,
    mem_id: str,
    content: str,
    category: str,
    subcategory: Optional[str] = None,
    metadata: Optional[dict] = None,
    owner_id: str,
    namespace: str,
    permission_mode: int = 600,
    verbatim_content: Optional[str] = None,
    source_model: Optional[str] = None,
    source_provider: Optional[str] = None,
    source_session: Optional[str] = None,
    source_agent: Optional[str] = None,
):
    """Insert a canonical memory row and enqueue memory.created in the same txn."""
    verbatim = verbatim_content if verbatim_content is not None else content
    await conn.execute(
        "INSERT INTO memories "
        "(id, content, category, subcategory, metadata, quality_rating, verbatim_content, "
        "owner_id, namespace, permission_mode, "
        "source_model, source_provider, source_session, source_agent) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)",
        mem_id, content, category, subcategory, json.dumps(metadata or {}), verbatim,
        owner_id, namespace, permission_mode,
        source_model, source_provider, source_session, source_agent,
    )

    event_payload = {
        "memory_id": mem_id,
        "category": category,
        "subcategory": subcategory,
        "content": content,
        "owner_id": owner_id,
        "namespace": namespace,
    }

    from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook
    await _dispatch_webhook(
        "memory.created",
        event_payload,
        conn=conn,
        owner_id=owner_id,
        namespace=namespace,
    )

    from mnemos.nats.client import get_node_name as _nats_get_node_name
    safe_ns = (namespace or "default").replace(".", "_")
    nats_intents: list[NatsPublishIntent] = [
        (
            f"mnemos.memory.created.{safe_ns}",
            {
                "memory_id": mem_id,
                "namespace": namespace,
                "category": category,
                "source_node": _nats_get_node_name(),
            },
            f"{mem_id}.created",
        )
    ]

    return nats_intents


async def _bump_recall_counters(memory_ids: list) -> None:
    """Increment recall_count + set last_recalled_at for a hit set.

    Called fire-and-forget after a search returns its response, so
    counter updates don't add latency to the user-visible search path.
    Failures log and swallow — recall counters are observability, not
    user-content correctness.

    Single UPDATE for the whole hit set, so search hits with N memories
    cost one DB round-trip not N.
    """
    if not memory_ids or not _lc._pool:
        return
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            await conn.execute(
                "UPDATE memories "
                "SET recall_count = recall_count + 1, "
                "    last_recalled_at = now() "
                "WHERE id = ANY($1::text[]) AND deleted_at IS NULL",
                list(memory_ids),
            )
    except Exception as e:
        logger.warning(f"[RECALL] bump failed for {len(memory_ids)} ids: {e}")


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    namespace: Optional[str] = None,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    backend = _backend_or_503()
    # Cross-namespace request rejected explicitly for non-root —
    # don't silently scope and hide rows. Root callers may pass any
    # namespace for cross-tenant audit lookups.
    root = is_root(user)
    if not root and namespace and namespace != user.namespace:
        raise HTTPException(
            status_code=403,
            detail="cross-namespace list requires root",
        )
    effective_namespace = namespace if root else user.namespace
    visibility = VisibilityFilter.for_read(user, namespace=effective_namespace)

    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)
        rows, total = await backend.memories.list_memories(
            tx,
            visibility=visibility,
            category=category,
            subcategory=subcategory,
            limit=limit,
            offset=offset,
        )
    return MemoryListResponse(
        count=total, memories=[_row_to_memory(r) for r in rows],
    )


@router.get("/memories/{memory_id}", response_model=MemoryItem)
async def get_memory(
    memory_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Fetch a memory by id.

    Content-negotiation surface (Accept header):
      * default / ``application/json`` / ``*/*`` — returns the JSON
        ``MemoryItem`` (existing behaviour, unchanged).
      * ``text/plain`` — returns the prose narration body, identical
        to ``GET /v1/memories/{id}/narrate?format=prose``.
      * ``application/x-apollo-dense`` — returns the raw winning-
        variant content (APOLLO dense form), identical to
        ``?format=dense`` on the narrate endpoint.

    All representations honour the same ``VisibilityFilter.for_read``
    read contract — owner, federated, world-readable, and group-
    readable memories are returned identically across Accept values,
    so a memory the caller could read as JSON cannot 404 under
    ``Accept: text/plain``. ``Vary: Accept`` is set on every
    representation so caches keyed on URL alone never replay a JSON
    body to a text/plain caller (or vice-versa).
    """
    accept = request.headers.get("accept", "") if request else ""
    narrate_format = negotiate_narrate_format(accept)

    backend = _backend_or_503()
    # Root callers see everything (namespace=None); non-root callers
    # are pinned to their namespace by the visibility factory. 404
    # (not 403) keeps other-tenant memory existence invisible — same
    # contract as the legacy handler.
    visibility = VisibilityFilter.for_read(
        user, namespace=None if is_root(user) else user.namespace,
    )
    body: Optional[str] = None
    row = None
    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)
        row = await backend.memories.get_memory(
            tx, memory_id, visibility=visibility,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        # Variant lookup must run inside the same transaction as the
        # memory fetch so a SQLite backend (single shared connection)
        # sees a consistent view, and so the visibility-gated row and
        # the variant we narrate from it cannot drift across a
        # concurrent compression-write boundary.
        if narrate_format is not None:
            from mnemos.api.routes.narrate import build_narration_body

            body = await build_narration_body(
                backend, tx, row, narrate_format,
            )

    # Vary: Accept on every successful representation. Unifies cache
    # behaviour even when the negotiated branch was not taken (a
    # JSON-first response cached without Vary could otherwise be
    # replayed to a later text/plain caller). Setting on the JSONResponse
    # path requires building it explicitly so the header is on the
    # serialised response — relying on FastAPI's response_model would
    # bypass our header injection.
    if narrate_format is not None:
        media_type = (
            "text/plain"
            if narrate_format == "prose"
            else "application/x-apollo-dense"
        )
        return PlainTextResponse(
            body or "",
            media_type=media_type,
            headers={"Vary": "Accept"},
        )

    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    memory_item = _row_to_memory(row, include_compressed=True)
    return JSONResponse(
        content=jsonable_encoder(memory_item),
        headers={"Vary": "Accept"},
    )


def _render_content_preview(content: Optional[str], include_content: bool) -> Optional[str]:
    """Full content when the caller asked for it, first-200-chars preview
    otherwise. Returning None stays None — the engine produced no output."""
    if content is None:
        return None
    if include_content:
        return content
    return content if len(content) <= 200 else content[:200] + "…"


@router.get("/memories/{memory_id}/compression-manifests")
async def get_compression_manifests(
    memory_id: str,
    include_content: bool = Query(
        False,
        description=(
            "Return full compressed_content for the winning variant and "
            "every candidate. Default returns a 200-character preview to "
            "keep responses small; flip for deep audit inspection."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Return the v3.1 compression audit trail for a memory.

    Two sections:
      * `variant`  — the current winning dense form (or null if no contest
                     has produced a winner yet). Pointer into the contest
                     candidate that "won" most recently.
      * `contests` — every historical contest, grouped by contest_id,
                     ordered most recent first. Each contest lists every
                     engine attempt with scoring fields and reject_reason.

    The response shape mirrors the v3.1 compression schema exactly so
    operators can reason about what was tried, what scored how, and why
    each engine was or wasn't picked.
    """
    require_postgres_pool_or_503(route_label="GET /v1/memories/{memory_id}/compression-manifests")

    async with _lc.get_pool_manager().acquire() as conn:
        async with _rls_context(conn, user):
            # Enforce memory visibility — check owner + namespace for
            # non-root so manifests for cross-tenant memories don't
            # leak their existence. RLS (when enabled) scopes owner_id
            # but never namespace; the app-layer filter here is
            # defense-in-depth for the RLS-disabled case too.
            if is_root(user):
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories WHERE id = $1 AND deleted_at IS NULL",
                    memory_id,
                )
            else:
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories "
                    "WHERE id = $1 AND owner_id = $2 AND namespace = $3 "
                    "AND deleted_at IS NULL",
                    memory_id, user.user_id, user.namespace,
                )
            if not exists:
                raise HTTPException(status_code=404, detail="Memory not found")

            variant_row = await conn.fetchrow(
                """
                SELECT engine_id, engine_version, compressed_content,
                       compressed_tokens, compression_ratio, quality_score,
                       composite_score, scoring_profile, judge_model,
                       selected_at, winner_candidate_id
                FROM memory_compressed_variants
                WHERE memory_id = $1
                """,
                memory_id,
            )

            candidate_rows = await conn.fetch(
                """
                SELECT contest_id, engine_id, engine_version,
                       compressed_content, original_tokens, compressed_tokens,
                       compression_ratio, quality_score, speed_factor,
                       composite_score, scoring_profile, elapsed_ms,
                       judge_model, gpu_used, is_winner, reject_reason,
                       manifest, created
                FROM memory_compression_candidates
                WHERE memory_id = $1
                ORDER BY created ASC, is_winner DESC, engine_id
                """,
                memory_id,
            )

    variant: Optional[dict] = None
    if variant_row is not None:
        variant = {
            "engine_id": variant_row["engine_id"],
            "engine_version": variant_row["engine_version"],
            "compressed_content": _render_content_preview(
                variant_row["compressed_content"], include_content,
            ),
            "compressed_tokens": variant_row["compressed_tokens"],
            "compression_ratio": variant_row["compression_ratio"],
            "quality_score": variant_row["quality_score"],
            "composite_score": variant_row["composite_score"],
            "scoring_profile": variant_row["scoring_profile"],
            "judge_model": variant_row["judge_model"],
            "selected_at": (
                variant_row["selected_at"].isoformat()
                if variant_row["selected_at"] else None
            ),
            "winner_candidate_id": (
                str(variant_row["winner_candidate_id"])
                if variant_row["winner_candidate_id"] else None
            ),
        }

    contests: dict[str, dict] = {}
    for row in candidate_rows:
        cid = str(row["contest_id"])
        bucket = contests.setdefault(cid, {
            "contest_id": cid,
            "started_at": row["created"],
            "candidates": [],
        })
        # earliest created row's timestamp represents the contest start
        if row["created"] < bucket["started_at"]:
            bucket["started_at"] = row["created"]

        manifest_field = row["manifest"]
        if isinstance(manifest_field, str):
            try:
                manifest_field = json.loads(manifest_field)
            except Exception:
                manifest_field = {"_raw": manifest_field}

        bucket["candidates"].append({
            "engine_id": row["engine_id"],
            "engine_version": row["engine_version"],
            "compressed_content": _render_content_preview(
                row["compressed_content"], include_content,
            ),
            "original_tokens": row["original_tokens"],
            "compressed_tokens": row["compressed_tokens"],
            "compression_ratio": row["compression_ratio"],
            "quality_score": row["quality_score"],
            "speed_factor": row["speed_factor"],
            "composite_score": row["composite_score"],
            "scoring_profile": row["scoring_profile"],
            "elapsed_ms": row["elapsed_ms"],
            "judge_model": row["judge_model"],
            "gpu_used": row["gpu_used"],
            "is_winner": row["is_winner"],
            "reject_reason": row["reject_reason"],
            "manifest": manifest_field,
            "created": row["created"].isoformat(),
        })

    contests_list = sorted(
        (
            {**bucket, "started_at": bucket["started_at"].isoformat()}
            for bucket in contests.values()
        ),
        key=lambda c: c["started_at"],
        reverse=True,
    )

    return {
        "memory_id": memory_id,
        "variant": variant,
        "contests": contests_list,
    }


@router.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(
    request: MemorySearchRequest,
    user: UserContext = Depends(get_current_user),
):
    """Search memories with optional 5-minute response caching."""
    request_limit = min(request.limit, 500)  # server-side cap regardless of model field

    # v3.1.2 Tier 3: pin owner_id + namespace to the caller's identity
    # for non-root searches. Previously request.namespace was caller-
    # controlled (a non-root user could search any namespace) and
    # owner_id was never passed at all. Root callers may pass any
    # namespace / owner to support cross-tenant audit.
    if is_root(user):
        search_owner_id = None  # no owner filter for root
        search_namespace = request.namespace  # honor caller's request
    else:
        search_owner_id = user.user_id
        # If the caller asked for a different namespace than theirs,
        # reject explicitly — don't silently scope and hide rows.
        if request.namespace and request.namespace != user.namespace:
            raise HTTPException(
                status_code=403,
                detail="cross-namespace search requires root",
            )
        search_namespace = user.namespace

    # Cache key MUST include user.user_id and the EFFECTIVE namespace +
    # owner_id — the server-resolved filter values, not the caller's
    # raw request. Using request.namespace (possibly None) would create
    # duplicate cache entries for identical result sets.
    # Cache key must include the caller's group_ids — search visibility
    # now depends on group membership (slice 2.1), so caching by
    # user_id alone would either leak rows after a group revoke or
    # hide rows after a group grant for the cache TTL window.
    #
    # Round-8 fix: pass RAW values (no `or ""` truthy-coalescing).
    # The query helpers distinguish None (no SQL predicate) from ""
    # (predicate with empty value); collapsing both to "" before
    # serialization aliases distinct semantics. JSON encoding inside
    # _get_cache_key now preserves None as null vs "" as "" so the
    # digest reflects the request's actual filter shape.
    cache_key = _get_cache_key(
        "search",
        user.user_id, user.namespace,
        request.query, request_limit,
        request.category, request.subcategory,
        "semantic" if request.semantic else "fts",
        request.source_provider, request.source_model,
        request.source_agent,
        search_namespace, search_owner_id,
        sorted(user.group_ids),  # list, not pre-serialized string
    )

    if _lc._cache and not request.include_compressed:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug(f"[CACHE] /memories/search hit for '{request.query[:30]}'")
                return MemoryListResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] search read error: {e}")

    backend = _backend_or_503()
    # Root callers can search across namespaces (search_owner_id is
    # None); non-root callers are pinned. The visibility factory
    # rejects namespace=None for non-root, which the namespace 403
    # check above already prevents reaching.
    visibility = VisibilityFilter.for_read(user, namespace=search_namespace)

    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)
        if request.semantic:
            embedding = await _get_embedding(request.query)
            if not embedding:
                logger.warning("[VECTOR] Embedding failed, falling back to FTS")
                rows = await backend.memories.fts_search(
                    tx,
                    query=request.query,
                    limit=request_limit,
                    visibility=visibility,
                    category=request.category,
                    subcategory=request.subcategory,
                    source_provider=request.source_provider,
                    source_model=request.source_model,
                    source_agent=request.source_agent,
                )
            else:
                logger.info(f"[VECTOR] Semantic search: {len(embedding)}-dim vector")
                rows = await backend.memories.semantic_search(
                    tx,
                    embedding=embedding,
                    limit=request_limit,
                    visibility=visibility,
                    category=request.category,
                    subcategory=request.subcategory,
                    source_provider=request.source_provider,
                    source_model=request.source_model,
                    source_agent=request.source_agent,
                )
        else:
            rows = await backend.memories.fts_search(
                tx,
                query=request.query,
                limit=request_limit,
                visibility=visibility,
                category=request.category,
                subcategory=request.subcategory,
                source_provider=request.source_provider,
                source_model=request.source_model,
                source_agent=request.source_agent,
            )

    memories = [_row_to_memory(r, include_compressed=request.include_compressed) for r in rows]

    # Fire-and-forget recall-frequency bump for the hit set.
    # Doesn't block the response; failure here is logged and ignored
    # (recall counters are observability, not user-content correctness).
    if memories:
        hit_ids = [m.id for m in memories]
        asyncio.create_task(_bump_recall_counters(hit_ids))

    compression_applied = False
    compression_metadata = {}

    response = MemoryListResponse(
        count=len(memories),
        memories=memories,
        compression_applied=compression_applied,
        compression_metadata=compression_metadata if compression_applied else None,
    )

    if _lc._cache and not request.include_compressed and not compression_applied:
        try:
            await _lc._cache.setex(cache_key, 300, response.model_dump_json())
        except Exception as e:
            logger.warning(f"[CACHE] search write error: {e}")

    return response


@router.post("/memories", response_model=MemoryItem, status_code=201)
async def create_memory(
    request: MemoryCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=422, detail="Memory content cannot be empty")
    backend = _backend_or_503()
    mem_id = new_memory_id()

    # Only root may create a memory attributed to a different owner
    # or namespace than the caller — closes the ghost-writing
    # vulnerability where any user could set request.owner_id.
    if request.owner_id and request.owner_id != user.user_id and user.role != "root":
        raise HTTPException(status_code=403, detail="owner_id override requires root")
    if request.namespace and request.namespace != user.namespace and user.role != "root":
        raise HTTPException(status_code=403, detail="namespace override requires root")
    owner_id = request.owner_id or user.user_id
    namespace = request.namespace or user.namespace
    permission_mode = _validate_permission_mode(request.permission_mode, default=600)

    metadata_json = json.dumps(request.metadata or {"source": request.source})
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            # The Postgres trg_memory_version_insert trigger writes
            # version 1 + branch automatically; the SQLite path does
            # not have that trigger today (deferred to v4.2 with
            # branch/version surface).
            await backend.memories.insert_memory(
                tx,
                memory_id=mem_id,
                content=request.content,
                category=request.category,
                subcategory=request.subcategory,
                metadata_json=metadata_json,
                quality_rating=75,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=permission_mode,
                source_model=request.source_model,
                source_provider=request.source_provider,
                source_session=request.source_session,
                source_agent=request.source_agent,
                verbatim_content=(
                    request.verbatim_content
                    if request.verbatim_content is not None
                    else request.content
                ),
                created=None,
                updated=None,
            )
            # Same-tx outbox enqueue — preserves the v4.0 contract
            # that webhook_deliveries rows commit atomically with
            # the data write.
            delivery_ids = await backend.webhooks.dispatch_event(
                tx,
                "memory.created",
                {
                    "memory_id": mem_id,
                    "category": request.category,
                    "subcategory": request.subcategory,
                    "content": request.content,
                    "owner_id": owner_id,
                    "namespace": namespace,
                },
                owner_id=owner_id,
                namespace=namespace,
            )
            # Re-fetch the row inside the same tx so the response
            # carries DB-resolved values (created/updated, etc).
            row = await backend.memories.get_memory(
                tx,
                mem_id,
                visibility=_read_visibility_for(user, namespace=namespace),
            )
    except DuplicateMemoryError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("memory.create transaction failed for %s: %s", mem_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Memory creation failed") from e

    # Schedule HTTP delivery for each enqueued outbox row, after the
    # transaction has committed.
    _schedule_outbox_deliveries(delivery_ids)

    # v4.2 NATS additive emit. Best-effort — silent skip when broker
    # unreachable. Webhooks outbox above is the durable path.
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name
    safe_ns = (namespace or "default").replace(".", "_")
    await _nats_publish_event(
        f"mnemos.memory.created.{safe_ns}",
        {
            "memory_id": mem_id,
            "namespace": namespace,
            "category": request.category,
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{mem_id}.created",
    )

    await _invalidate_caches_after_mutation()
    return _row_to_memory(row)


@router.post("/memories/bulk", response_model=BulkCreateResponse, status_code=201)
async def bulk_create_memories(
    request: BulkCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create multiple memories in one request. Per-item errors are collected, not raised."""
    backend = _backend_or_503()
    created_ids: list[str] = []
    errors: list[str] = []
    delivery_ids: list[str] = []
    nats_created_events: list[dict] = []
    for i, mem in enumerate(request.memories):
        if not mem.content.strip():
            errors.append(f"[{i}] content is empty")
            continue
        if mem.owner_id and mem.owner_id != user.user_id and user.role != "root":
            errors.append(f"[{i}] owner_id override requires root")
            continue
        if mem.namespace and mem.namespace != user.namespace and user.role != "root":
            errors.append(f"[{i}] namespace override requires root")
            continue
        try:
            permission_mode = _validate_permission_mode(mem.permission_mode, default=600)
        except HTTPException as exc:
            errors.append(f"[{i}] {exc.detail}")
            continue
        mid = new_memory_id()
        verbatim = (
            mem.verbatim_content
            if mem.verbatim_content is not None
            else mem.content
        )
        owner_id = mem.owner_id or user.user_id
        namespace = mem.namespace or user.namespace
        try:
            async with backend.transactional() as tx:
                await _maybe_set_pg_rls(tx, user)
                await backend.memories.insert_memory(
                    tx,
                    memory_id=mid,
                    content=mem.content,
                    category=mem.category,
                    subcategory=mem.subcategory,
                    metadata_json=json.dumps(mem.metadata or {}),
                    quality_rating=75,
                    owner_id=owner_id,
                    namespace=namespace,
                    permission_mode=permission_mode,
                    source_model=mem.source_model,
                    source_provider=mem.source_provider,
                    source_session=mem.source_session,
                    source_agent=mem.source_agent,
                    verbatim_content=verbatim,
                    created=None,
                    updated=None,
                )
                item_delivery_ids = await backend.webhooks.dispatch_event(
                    tx,
                    "memory.created",
                    {
                        "memory_id": mid,
                        "category": mem.category,
                        "subcategory": mem.subcategory,
                        "content": mem.content,
                        "owner_id": owner_id,
                        "namespace": namespace,
                    },
                    owner_id=owner_id,
                    namespace=namespace,
                )
        except Exception as e:
            errors.append(f"[{i}] {e}")
            continue
        created_ids.append(mid)
        nats_created_events.append(
            {
                "memory_id": mid,
                "namespace": namespace,
                "category": mem.category,
            }
        )
        delivery_ids.extend(item_delivery_ids)
    _schedule_outbox_deliveries(delivery_ids)
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name
    source_node = _nats_get_node_name()
    for event in nats_created_events:
        safe_ns = (event["namespace"] or "default").replace(".", "_")
        await _nats_publish_event(
            f"mnemos.memory.created.{safe_ns}",
            {**event, "source_node": source_node},
            msg_id=f"{event['memory_id']}.created",
        )
    await _invalidate_caches_after_mutation()
    return BulkCreateResponse(created=len(created_ids), memory_ids=created_ids, errors=errors)


@router.patch("/memories/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Partially update a memory (content, category, subcategory, metadata)."""
    backend = _backend_or_503()
    updates: dict = {}
    if request.content is not None:
        if not request.content.strip():
            raise HTTPException(status_code=422, detail="Memory content cannot be empty")
        updates["content"] = request.content
    if request.category is not None:
        updates["category"] = request.category
    if request.subcategory is not None:
        updates["subcategory"] = request.subcategory
    if request.metadata is not None:
        updates["metadata"] = json.dumps(request.metadata)
    if request.verbatim_content is not None:
        updates["verbatim_content"] = request.verbatim_content
    if request.permission_mode is not None:
        updates["permission_mode"] = _validate_permission_mode(request.permission_mode)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    # Authorization + mutation in a single repository call: the
    # visibility predicate folds into the UPDATE … RETURNING, so a
    # concurrent admin/repair changing ownership between auth check
    # and write cannot complete the update. Same TOCTOU-safe shape
    # as the legacy handler.
    visibility = _mutation_visibility_for(
        user,
        namespace=None if is_root(user) else user.namespace,
    )
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            try:
                row = await backend.memories.update_memory(
                    tx, memory_id, visibility=visibility, fields=updates,
                )
            except asyncpg.PostgresError as exc:
                handle_trigger_pgerror(exc)
            if not row:
                raise HTTPException(
                    status_code=404, detail=f"Memory {memory_id} not found",
                )
            delivery_ids = await backend.webhooks.dispatch_event(
                tx,
                "memory.updated",
                {
                    "memory_id": memory_id,
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "content": row["content"],
                    "owner_id": row["owner_id"],
                    "namespace": row["namespace"],
                },
                owner_id=row["owner_id"],
                namespace=row["namespace"],
            )
    except HTTPException:
        raise
    _schedule_outbox_deliveries(delivery_ids)
    try:
        updated_at = row["updated"]
    except (KeyError, TypeError):
        updated_at = None
    if hasattr(updated_at, "isoformat"):
        updated_suffix = updated_at.isoformat()
    else:
        updated_suffix = str(int(time.time() * 1000))
    namespace = row["namespace"]
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name
    safe_ns = (namespace or "default").replace(".", "_")
    await _nats_publish_event(
        f"mnemos.memory.updated.{safe_ns}",
        {
            "memory_id": memory_id,
            "namespace": namespace,
            "category": row["category"],
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{memory_id}.updated.{updated_suffix}",
    )
    await _invalidate_caches_after_mutation()
    return _row_to_memory(row)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Delete a memory by ID."""
    backend = _backend_or_503()
    # Mutation visibility: non-root pinned to (owner_id, namespace);
    # root sees everything. Closes the cross-namespace deletion path
    # where a namespace-A user could delete a namespace-B row under
    # the same owner_id.
    visibility = _mutation_visibility_for(
        user,
        namespace=None if is_root(user) else user.namespace,
    )
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            try:
                row = await backend.memories.delete_memory(
                    tx, memory_id, visibility=visibility,
                )
            except asyncpg.PostgresError as exc:
                handle_trigger_pgerror(exc)
            if not row:
                raise HTTPException(
                    status_code=404, detail=f"Memory {memory_id} not found",
                )
            delivery_ids = await backend.webhooks.dispatch_event(
                tx,
                "memory.deleted",
                {
                    "memory_id": row["id"],
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "content": row["content"],
                    "owner_id": row["owner_id"],
                    "namespace": row["namespace"],
                },
                owner_id=row["owner_id"],
                namespace=row["namespace"],
            )
    except HTTPException:
        raise
    _schedule_outbox_deliveries(delivery_ids)
    namespace = row["namespace"]
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name
    safe_ns = (namespace or "default").replace(".", "_")
    await _nats_publish_event(
        f"mnemos.memory.deleted.{safe_ns}",
        {
            "memory_id": row["id"],
            "namespace": namespace,
            "category": row["category"],
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{row['id']}.deleted",
    )
    await _invalidate_caches_after_mutation()


@router.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(
    request: RehydrationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Return memories optimized for Claude context injection (Phase 5)."""
    require_postgres_pool_or_503(route_label="POST /v1/memories/rehydrate")
    # Same v3.1.2 Tier 3 pinning as /memories/search — rehydrate is a
    # read path for the caller's own corpus.
    rehydrate_owner_id = None if is_root(user) else user.user_id
    rehydrate_namespace = None if is_root(user) else user.namespace

    # v3.2 compression-in-hot-paths: rehydrate is the canonical
    # "fit memories into a token budget" path, so it benefits most
    # from preferring the contest winner variant over the raw content.
    # Fallback chain: contest winner -> raw content.
    #
    # Inlined here (rather than routed through _fts_fetch) because
    # the JOIN shape is rehydrate-specific: one-to-one with
    # memory_compressed_variants, COALESCE chosen in SELECT. The
    # shared helper doesn't need the complexity.
    #
    # We also track `compression_applied` for the response: true
    # iff at least one row returned a variant-compressed form.
    clean_query = request.query.strip()
    sql_conditions = [
        "to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)",
        "m.deleted_at IS NULL",
    ]
    sql_params: list = [clean_query, request.limit]
    idx = 3
    if rehydrate_owner_id is not None:
        # Slice 2.1: full v1_multiuser-mirror visibility predicate
        # (owner / federation / world-readable / group-readable),
        # aliased to the JOIN's `m.` table reference. Same predicate
        # as list/get/search so a memory visible there is visible
        # via /memories/rehydrate.
        from mnemos.core.visibility import read_visibility_predicate
        clause, vis_params = read_visibility_predicate(
            rehydrate_owner_id, list(user.group_ids), idx, table_alias="m",
        )
        sql_conditions.append(clause)
        sql_params.extend(vis_params)
        idx += len(vis_params)
    if rehydrate_namespace is not None:
        sql_conditions.append(f"m.namespace=${idx}")
        sql_params.append(rehydrate_namespace)
        idx += 1
    if request.category is not None:
        sql_conditions.append(f"m.category=${idx}")
        sql_params.append(request.category)
        idx += 1

    where_sql = " AND ".join(sql_conditions)
    sql = (
        "SELECT m.id, m.category, m.created, m.quality_rating, "
        "       m.content AS raw_content, "
        "       v.compressed_content AS compressed_content, "
        "       v.compressed_content IS NOT NULL AS variant_used, "
        "       ts_rank(to_tsvector('english', m.content), "
        "               plainto_tsquery('english', $1)) AS rank "
        "FROM memories m "
        "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
        f"WHERE {where_sql} "
        "ORDER BY rank DESC LIMIT $2"
    )

    async with _lc.get_pool_manager().acquire() as conn:
        async with _rls_context(conn, user):
            rows = await conn.fetch(sql, *sql_params)

    if not rows:
        return RehydrationResponse(
            context="", tokens_used=0, original_tokens=0,
            compression_ratio=1.0, quality_score=100,
            memories_included=0, compression_applied=False,
        )
    context_parts = []
    raw_size = 0
    variant_hits = 0
    for row in rows:
        # Prefer contest winner (variant_used=True), else raw.
        effective = row["compressed_content"] or row["raw_content"]
        raw_size += len(row["raw_content"] or "")
        if row["variant_used"]:
            variant_hits += 1
        created_str = row['created'].strftime('%Y-%m-%d') if row['created'] else 'unknown'
        context_parts.append(f"[{row['category']} / {created_str}]\n{effective[:2000]}")
    combined_context = "\n\n---\n\n".join(context_parts)
    original_tokens = int(len(combined_context) / 4)

    tokens_used = min(original_tokens, request.budget_tokens) if request.budget_tokens else original_tokens
    compression_applied = variant_hits > 0
    # Only report a non-1.0 ratio when variants were actually used;
    # otherwise the context size is dominated by category/date
    # prefixes added by the rehydrator and the "ratio" is misleading.
    if compression_applied and raw_size > 0:
        compression_ratio = len(combined_context) / raw_size
    else:
        compression_ratio = 1.0

    logger.info(
        f"[REHYDRATE] query='{request.query[:30]}' | memories={len(rows)} | "
        f"variant_hits={variant_hits} | original_tokens={original_tokens} | "
        f"tokens_used={tokens_used} | compression_applied={compression_applied} | "
        f"compression_ratio={compression_ratio:.3f}"
    )
    return RehydrationResponse(
        context=combined_context[:request.budget_tokens * 4] if request.budget_tokens else combined_context,
        tokens_used=tokens_used,
        original_tokens=original_tokens,
        compression_ratio=compression_ratio,
        quality_score=100,
        memories_included=len(rows),
        compression_applied=compression_applied,
    )

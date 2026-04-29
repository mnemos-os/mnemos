"""Memory CRUD, search, and rehydration endpoints."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core.ids import new_memory_id
from mnemos.core.lifecycle import (
    _MEMORY_COLS,
    _fts_fetch,
    _get_cache_key,
    _get_embedding,
    _row_to_memory,
    _vector_search,
)
from mnemos.core.security import is_root
from mnemos.core.visibility import handle_trigger_pgerror
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
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["memories"])


@asynccontextmanager
async def _rls_context(conn, user: UserContext):
    """Set PostgreSQL session variables for RLS when auth is active."""
    if _lc._rls_enabled and user.authenticated:
        async with conn.transaction():
            await conn.execute(
                "SET LOCAL mnemos.current_user_id = $1", user.user_id
            )
            await conn.execute(
                "SET LOCAL mnemos.current_role = $1", user.role
            )
            yield conn
    else:
        yield conn

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
    fetch_row: bool = False,
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
    row = None
    if fetch_row:
        row = await conn.fetchrow(
            f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", mem_id,
        )

    from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook
    await _dispatch_webhook(
        "memory.created",
        {
            "memory_id": mem_id,
            "category": category,
            "subcategory": subcategory,
            "content": content,
            "owner_id": owner_id,
            "namespace": namespace,
        },
        conn=conn,
        owner_id=owner_id,
        namespace=namespace,
    )
    return row


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
        async with _lc._pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories "
                "SET recall_count = recall_count + 1, "
                "    last_recalled_at = now() "
                "WHERE id = ANY($1::text[])",
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
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # Tenancy scoping for non-root callers:
    #   - Namespace pinned. Cross-namespace request returns 403
    #     (mirrors search_memories' parity contract).
    #   - Read-visibility predicate aligned with the v1_multiuser
    #     RLS policies (see _read_visibility_predicate). Combines
    #     owner / federation / group-readable / world-readable into
    #     a single OR-clause at the app layer because RLS cannot
    #     RE-ADD rows that the handler WHERE has already excluded —
    #     a strict owner-only WHERE would silently hide
    #     group/world-readable rows in team/enterprise mode.
    # Root callers see everything; explicit ?namespace= honored for
    # cross-tenant audit lookups.
    root = is_root(user)
    if not root and namespace and namespace != user.namespace:
        raise HTTPException(
            status_code=403,
            detail="cross-namespace list requires root",
        )
    effective_namespace = namespace if root else user.namespace

    # Build dynamic WHERE clauses to avoid 4×2 hardcoded SQL branches.
    # Filter list is preserved across SELECT and COUNT — same params,
    # same predicate.
    where_parts: list[str] = []
    params: list = []
    if category is not None:
        where_parts.append(f"category=${len(params) + 1}")
        params.append(category)
    if subcategory is not None:
        where_parts.append(f"subcategory=${len(params) + 1}")
        params.append(subcategory)
    if effective_namespace is not None:
        where_parts.append(f"namespace=${len(params) + 1}")
        params.append(effective_namespace)
    if not root:
        vis_clause, vis_params = _read_visibility_predicate(
            user, len(params) + 1,
        )
        where_parts.append(vis_clause)
        params.extend(vis_params)

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    select_sql = (
        f"SELECT {_MEMORY_COLS} FROM memories{where_sql} "
        f"ORDER BY created DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    )
    count_sql = f"SELECT COUNT(*) FROM memories{where_sql}"

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            rows = await conn.fetch(select_sql, *params, limit, offset)
            total = await conn.fetchval(count_sql, *params)
    return MemoryListResponse(count=total, memories=[_row_to_memory(r) for r in rows])


@router.get("/memories/{memory_id}", response_model=MemoryItem)
async def get_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            if is_root(user):
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", memory_id,
                )
            else:
                # Read visibility for non-root: namespace pinned + the
                # shared read-visibility predicate (own / federation /
                # world-readable / group-readable), mirroring the
                # v1_multiuser RLS policies. Same predicate as
                # list_memories so a memory visible to a user via
                # list/search is also visible via GET-by-id.
                # Mutation paths (update/delete) keep strict
                # owner_id scoping. 404 (not 403) keeps other-tenant
                # memory existence invisible.
                vis_clause, vis_params = _read_visibility_predicate(user, 3)
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories "
                    f"WHERE id=$1 AND namespace=$2 AND {vis_clause}",
                    memory_id, user.namespace, *vis_params,
                )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _row_to_memory(row, include_compressed=True)


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
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            # Enforce memory visibility — check owner + namespace for
            # non-root so manifests for cross-tenant memories don't
            # leak their existence. RLS (when enabled) scopes owner_id
            # but never namespace; the app-layer filter here is
            # defense-in-depth for the RLS-disabled case too.
            if is_root(user):
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories WHERE id = $1",
                    memory_id,
                )
            else:
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories "
                    "WHERE id = $1 AND owner_id = $2 AND namespace = $3",
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

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            # group_ids is set only for non-root callers (search_owner_id
            # set means the predicate should mirror the v1_multiuser
            # full read-visibility, including group-readable rows).
            search_group_ids = (
                list(user.group_ids) if search_owner_id is not None else None
            )
            _prov = dict(
                source_provider=request.source_provider,
                source_model=request.source_model,
                source_agent=request.source_agent,
                namespace=search_namespace,
                owner_id=search_owner_id,
                group_ids=search_group_ids,
            )
            if request.semantic:
                embedding = await _get_embedding(request.query)
                if not embedding:
                    logger.warning("[VECTOR] Embedding failed, falling back to FTS")
                    rows = await _fts_fetch(
                        conn, request.query, request_limit,
                        request.category, request.subcategory,
                        **_prov,
                    )
                else:
                    logger.info(f"[VECTOR] Semantic search: {len(embedding)}-dim vector")
                    rows = await _vector_search(
                        conn, embedding, request_limit,
                        request.category, request.subcategory,
                        **_prov,
                    )
            else:
                rows = await _fts_fetch(
                    conn, request.query, request_limit,
                    request.category, request.subcategory,
                    **_prov,
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
    mem_id = new_memory_id()
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    # Only root may create a memory attributed to a different owner or namespace
    # than the caller. Previously any user could set request.owner_id and
    # ghost-write memories under someone else's identity.
    if request.owner_id and request.owner_id != user.user_id and user.role != "root":
        raise HTTPException(status_code=403, detail="owner_id override requires root")
    if request.namespace and request.namespace != user.namespace and user.role != "root":
        raise HTTPException(status_code=403, detail="namespace override requires root")
    owner_id = request.owner_id or user.user_id
    namespace = request.namespace or user.namespace

    try:
        async with _lc._pool.acquire() as conn:
            async with _rls_context(conn, user):
                async with conn.transaction():
                    # (trigger trg_memory_version_insert inserts version 1 automatically,
                    # computing commit_hash + branch; no explicit handler INSERT needed)
                    row = await _insert_memory_with_created_webhook(
                        conn=conn,
                        mem_id=mem_id,
                        content=request.content,
                        category=request.category,
                        subcategory=request.subcategory,
                        metadata=request.metadata or {"source": request.source},
                        owner_id=owner_id,
                        namespace=namespace,
                        permission_mode=600,
                        verbatim_content=request.verbatim_content,
                        source_model=request.source_model,
                        source_provider=request.source_provider,
                        source_session=request.source_session,
                        source_agent=request.source_agent,
                        fetch_row=True,
                    )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("memory.create transaction failed for %s: %s", mem_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Memory creation failed") from e
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    return _row_to_memory(row)


@router.post("/memories/bulk", response_model=BulkCreateResponse, status_code=201)
async def bulk_create_memories(
    request: BulkCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create multiple memories in one request. Per-item errors are collected, not raised."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    created_ids: list[str] = []
    errors: list[str] = []
    try:
        from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook

        async with _lc._pool.acquire() as conn:
            async with _rls_context(conn, user):
                async with conn.transaction():
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
                        mid = new_memory_id()
                        verbatim = (
                            mem.verbatim_content
                            if mem.verbatim_content is not None
                            else mem.content
                        )
                        owner_id = mem.owner_id or user.user_id
                        namespace = mem.namespace or user.namespace
                        try:
                            async with conn.transaction():
                                await conn.execute(
                                    "INSERT INTO memories "
                                    "(id, content, category, subcategory, metadata, quality_rating, verbatim_content, "
                                    "owner_id, namespace, permission_mode, "
                                    "source_model, source_provider, source_session, source_agent) "
                                    "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)",
                                    mid, mem.content, mem.category, mem.subcategory,
                                    json.dumps(mem.metadata or {}), verbatim,
                                    owner_id, namespace, 600,
                                    mem.source_model, mem.source_provider,
                                    mem.source_session, mem.source_agent,
                                )
                        except Exception as e:
                            errors.append(f"[{i}] {e}")
                            continue
                        created_ids.append(mid)
                        await _dispatch_webhook(
                            "memory.created",
                            {
                                "memory_id": mid,
                                "category": mem.category,
                                "subcategory": mem.subcategory,
                                "content": mem.content,
                                "owner_id": owner_id,
                                "namespace": namespace,
                            },
                            conn=conn,
                            owner_id=owner_id,
                            namespace=namespace,
                        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("memory.bulk_create transaction failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Bulk memory creation failed") from e
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    return BulkCreateResponse(created=len(created_ids), memory_ids=created_ids, errors=errors)


@router.patch("/memories/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Partially update a memory (content, category, subcategory, metadata)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {}
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
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    values = list(updates.values())

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            async with conn.transaction():
                # Authorization + mutation in a single statement to
                # close the TOCTOU window. The earlier shape was a
                # SELECT-then-UPDATE pair where the SELECT proved
                # owner+namespace but the UPDATE filtered by id alone
                # — between the two, a concurrent admin/repair path
                # could have changed ownership and the caller would
                # still complete the update. Folding the predicate
                # into the UPDATE … RETURNING makes the authorization
                # atomic with the mutation: if the row no longer
                # satisfies the predicate at write time, the update
                # affects zero rows and we 404.
                set_sql = ", ".join(set_clauses)
                try:
                    if is_root(user):
                        row = await conn.fetchrow(
                            f"UPDATE memories SET {set_sql} "
                            f"WHERE id=$1 RETURNING {_lc._MEMORY_COLS}",
                            memory_id, *values,
                        )
                    else:
                        # Append owner_id + namespace placeholders after
                        # the existing $1 (id) + values placeholders.
                        owner_ph = f"${len(values) + 2}"
                        ns_ph = f"${len(values) + 3}"
                        row = await conn.fetchrow(
                            f"UPDATE memories SET {set_sql} "
                            f"WHERE id=$1 AND owner_id={owner_ph} "
                            f"AND namespace={ns_ph} "
                            f"RETURNING {_lc._MEMORY_COLS}",
                            memory_id, *values, user.user_id, user.namespace,
                        )
                except asyncpg.PostgresError as exc:
                    handle_trigger_pgerror(exc)
                if not row:
                    raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
                # mnemos_version_snapshot AFTER UPDATE trigger writes
                # the new memory_versions row (commit_hash + bumped
                # version_num); the handler must not duplicate that
                # INSERT.
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    try:
        from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook
        await _dispatch_webhook(
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
    except Exception:
        logger.warning("webhook dispatch failed for memory.updated %s", memory_id, exc_info=True)
    return _lc._row_to_memory(row)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Delete a memory by ID."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            try:
                if is_root(user):
                    result = await conn.execute(
                        "DELETE FROM memories WHERE id = $1", memory_id,
                    )
                else:
                    # Two-dimensional check: non-root can only delete
                    # rows in their own namespace, preventing a namespace
                    # A user from deleting a namespace B row even under
                    # the same owner_id.
                    result = await conn.execute(
                        "DELETE FROM memories "
                        "WHERE id = $1 AND owner_id = $2 AND namespace = $3",
                        memory_id, user.user_id, user.namespace,
                    )
            except asyncpg.PostgresError as exc:
                handle_trigger_pgerror(exc)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    try:
        from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook
        await _dispatch_webhook(
            "memory.deleted",
            {
                "memory_id": memory_id,
                "owner_id": user.user_id,
                "namespace": user.namespace,
            },
            owner_id=user.user_id,
            namespace=user.namespace,
        )
    except Exception:
        logger.warning("webhook dispatch failed for memory.deleted %s", memory_id, exc_info=True)


@router.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(
    request: RehydrationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Return memories optimized for Claude context injection (Phase 5)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
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
        "to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)"
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

    async with _lc._pool.acquire() as conn:
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

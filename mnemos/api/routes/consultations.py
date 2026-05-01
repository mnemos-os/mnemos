"""GRAEAE multi-provider consultation endpoints — v3.0.0 unified service.

/v1/consultations — GRAEAE reasoning domain with hash-chained audit log and memory refs.

"""
import hashlib
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core.rate_limit import limiter
from mnemos.core.security import is_root, scope_namespace
from mnemos.domain.graeae.engine import _REGISTRY_MAP
from mnemos.domain.models import (
    AuditLogEntry,
    AuditVerifyResponse,
    ConsultationArtifact,
    ConsultationRequest,
    ConsultationResponse,
    SUPPORTED_CONSULTATION_MODES,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["consultations"])

_GENESIS_HASH = hashlib.sha256(b"MNEMOS_AUDIT_GENESIS_v3").hexdigest()


def _schedule_outbox_deliveries(delivery_ids: list[str]) -> None:
    if not delivery_ids:
        return
    from mnemos.api.routes.memories import _schedule_outbox_deliveries as _schedule

    _schedule(delivery_ids)

# ── Custom Query selection (v3.2) ─────────────────────────────────────────────

_VALID_TIERS = {"frontier", "premium", "budget"}

# Translate a model_registry.provider value (e.g. "anthropic") back into
# the GRAEAE engine provider key (e.g. "claude") so consult()'s selection
# filter doesn't silently drop entries. Only `anthropic→claude` flips
# today but the map is built from _REGISTRY_MAP so future renames
# propagate automatically.
_REGISTRY_TO_GRAEAE = {
    cfg["registry_provider"]: name
    for name, cfg in _REGISTRY_MAP.items()
}


def _to_graeae_provider(registry_name: str) -> str:
    return _REGISTRY_TO_GRAEAE.get(registry_name, registry_name)


async def _tier_lineup(tier: str) -> dict:
    """Resolve a tier name to {provider_name: model_id} using model_registry.

    Tier definitions (aligned with the v3.1.2 /v1/models registry work):

      * frontier  — arena_rank <= 5 OR graeae_weight >= 0.95
      * premium   — arena_rank BETWEEN 6 AND 15 OR graeae_weight in [0.85, 0.95)
      * budget    — cheapest available models at graeae_weight >= 0.75

    The caller reflects a tier into a concrete dict that consult()
    consumes as a selection. Empty registry -> empty dict; handler
    treats that as a hard error (otherwise we'd silently fall back
    to auto, which violates the caller's intent).
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    if tier == "frontier":
        sql = """
            SELECT DISTINCT ON (provider) provider, model_id
            FROM model_registry
            WHERE available = true AND deprecated = false
              AND (arena_rank IS NOT NULL AND arena_rank <= 5
                   OR graeae_weight >= 0.95)
            ORDER BY provider, graeae_weight DESC NULLS LAST, arena_rank ASC NULLS LAST
        """
        params: tuple = ()
    elif tier == "premium":
        sql = """
            SELECT DISTINCT ON (provider) provider, model_id
            FROM model_registry
            WHERE available = true AND deprecated = false
              AND ((arena_rank IS NOT NULL AND arena_rank BETWEEN 6 AND 15)
                   OR (graeae_weight >= 0.85 AND graeae_weight < 0.95))
            ORDER BY provider, graeae_weight DESC NULLS LAST, arena_rank ASC NULLS LAST
        """
        params = ()
    elif tier == "budget":
        # NULL costs are EXCLUDED from the budget tier — an unknown
        # cost cannot legally satisfy a "cheapest" semantics, and
        # COALESCEing to 0 would let partially-synced rows rank
        # ahead of priced models. Same invariant as the budget
        # selection in mcp_repo / providers route / openai_compat.
        sql = """
            SELECT DISTINCT ON (provider) provider, model_id
            FROM model_registry
            WHERE available = true AND deprecated = false
              AND graeae_weight >= 0.75
              AND input_cost_per_mtok IS NOT NULL
              AND output_cost_per_mtok IS NOT NULL
            ORDER BY provider,
                     (input_cost_per_mtok + output_cost_per_mtok) ASC
        """
        params = ()
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown tier {tier!r}; "
                f"expected one of {sorted(_VALID_TIERS)}"
            ),
        )

    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    # Translate registry provider → GRAEAE engine provider key
    # (`anthropic` → `claude` etc.) so consult()'s selection filter
    # at engine.py:_candidate_providers doesn't silently drop muses.
    return {_to_graeae_provider(r["provider"]): r["model_id"] for r in rows}


async def _resolve_models(model_ids: List[str]) -> dict:
    """Resolve each explicit model_id to its provider via model_registry.

    Returns {provider_name: model_id}. Raises 400 on the first
    unrecognized model_id — fail-loudly beats silently narrowing a
    deliberately-chosen lineup.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT provider, model_id
            FROM model_registry
            WHERE model_id = ANY($1::text[])
              AND available = true
              AND deprecated = false
            """,
            model_ids,
        )
    found = {r["model_id"]: r["provider"] for r in rows}
    missing = [m for m in model_ids if m not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"unknown model_id(s): {missing}",
        )
    return {_to_graeae_provider(found[m]): m for m in model_ids}


async def _resolve_selection(
    engine,
    models: Optional[List[str]] = None,
    providers: Optional[List[str]] = None,
    tier: Optional[str] = None,
) -> Optional[dict]:
    """Resolve a caller's Custom Query selectors to a
    {provider_name: model_id_or_None} dict consult() understands.

    Precedence: models > providers > tier > None (auto lineup).
    Raises HTTPException(400) for unknown providers, unknown tiers,
    unknown model_ids, or empty tier result sets.
    """
    # Mutual exclusion — at most one selector. Prevents a caller from
    # passing both `tier=frontier` and `providers=[...]` and then
    # wondering which won. If a caller wants combined semantics (e.g.
    # "frontier models FROM these providers"), that's a follow-up
    # design; reject the combination today for clarity.
    set_fields = [n for n in (
        "models" if models else None,
        "providers" if providers else None,
        "tier" if tier else None,
    ) if n]
    if len(set_fields) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Custom Query accepts at most one of "
                f"{{'models', 'providers', 'tier'}}; got {set_fields}"
            ),
        )

    if models:
        return await _resolve_models(models)

    if providers:
        # Accept either GRAEAE name ("claude") or registry name
        # ("anthropic") and normalise to the GRAEAE key that consult()
        # filters against. Without this, providers=["anthropic"] would
        # 400 because engine.providers is keyed by "claude".
        normalised = [_to_graeae_provider(p) for p in providers]
        unknown = [p for p in normalised if p not in engine.providers]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown provider(s): {unknown}",
            )
        # De-duplicate while preserving caller order (two callers could
        # legitimately pass both "anthropic" and "claude" — they map to
        # the same engine slot).
        seen: set[str] = set()
        out: dict = {}
        for p in normalised:
            if p not in seen:
                out[p] = None
                seen.add(p)
        return out

    if tier:
        lineup = await _tier_lineup(tier)
        if not lineup:
            raise HTTPException(
                status_code=404,
                detail=f"tier {tier!r} has no matching rows in model_registry",
            )
        return lineup

    # None set -> auto lineup (existing behavior).
    return None


# ── Audit helpers ─────────────────────────────────────────────────────────────

async def _write_audit_entry_on_conn(
    conn,
    consultation_id,
    prompt: str,
    response: str,
    task_type: str,
    provider: str,
    quality_score: float,
) -> None:
    """Append a hash-chained entry to graeae_audit_log on an existing connection.

    Expects to be called inside an open transaction on `conn`. Raises on
    failure — callers must let the exception propagate so the surrounding
    consultation transaction aborts (tamper-evidence requires the audit row
    and the consultation row to commit atomically).
    """
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    response_hash = hashlib.sha256(response.encode()).hexdigest()

    # Advisory lock serializes concurrent inserts.
    # SELECT FOR UPDATE alone has a TOCTOU race: T2 reads the "last row"
    # before blocking, then computes the chain against that stale row after
    # T1 has already inserted a newer one.
    # Advisory lock (magic key = 0x4772616561 = "Graea") ensures only
    # one writer holds the chain tip at a time.
    await conn.execute("SELECT pg_advisory_xact_lock(285734657)")
    prev_row = await conn.fetchrow(
        "SELECT id, chain_hash FROM graeae_audit_log "
        "ORDER BY sequence_num DESC LIMIT 1"
    )
    if prev_row:
        prev_chain = prev_row["chain_hash"]
        prev_id = prev_row["id"]
    else:
        prev_chain = _GENESIS_HASH
        prev_id = None

    # Chain covers prev_chain + prompt_hash + response_hash so that
    # neither the prompt nor the response can be swapped without
    # breaking chain integrity.
    chain_hash = hashlib.sha256(
        (prev_chain + prompt_hash + response_hash).encode()
    ).hexdigest()

    await conn.execute(
        "INSERT INTO graeae_audit_log "
        "(consultation_id, prompt, prompt_hash, provider, response_text, "
        "response_hash, chain_hash, prev_id, prev_chain_hash, "
        "task_type, quality_score) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        consultation_id, prompt, prompt_hash, provider, response,
        response_hash, chain_hash, prev_id, prev_chain,
        task_type, quality_score,
    )


async def _write_memory_refs_on_conn(
    conn,
    consultation_id: str,
    memory_ids: List[str],
) -> None:
    """Record which memories were injected into this consultation, on an open conn.

    Raises on failure; caller's transaction aborts so memory-ref bookkeeping
    stays consistent with the consultation row.
    """
    if not memory_ids:
        return
    for memory_id in memory_ids:
        await conn.execute(
            "INSERT INTO consultation_memory_refs "
            "(consultation_id, memory_id, injected_at) "
            "VALUES ($1, $2, NOW()) "
            "ON CONFLICT DO NOTHING",
            consultation_id, memory_id,
        )


def _extract_memory_ids(result: dict) -> List[str]:
    """Collect injected/reference memory IDs from known result shapes."""
    raw_ids = (
        result.get("memory_ids")
        or result.get("injected_memory_ids")
        or result.get("citations")
        or []
    )
    memory_ids: list[str] = []
    for raw_id in raw_ids:
        memory_id = str(raw_id).strip()
        if memory_id and memory_id not in memory_ids:
            memory_ids.append(memory_id)
    return memory_ids


def _require_non_empty_consultation_result(result: object, mode: str) -> dict:
    """Fail loudly instead of letting an empty engine result serialize as success."""
    if not isinstance(result, dict) or not result:
        raise HTTPException(
            status_code=502,
            detail=(
                "GRAEAE consultation returned an empty result "
                f"for mode={mode!r}; refusing to return HTTP 200 with an empty body."
            ),
        )
    return result


# ── Consultation endpoint ─────────────────────────────────────────────────────

@router.post("/consultations", response_model=ConsultationResponse)
@limiter.limit("60/minute")
async def consult_graeae(request: Request, body: ConsultationRequest, user: UserContext = Depends(get_current_user)):
    """Consult GRAEAE multi-provider consensus engine.

    Creates a hash-chained audit entry and records any injected memories.
    Returns raw provider responses (full, best, or truncated per format param).
    """
    logger.info(
        f"[CONSULTATION] {user.user_id}: {body.task_type} "
        f"(limit_chars={body.limit_chars}, format={body.format})"
    )
    try:
        from mnemos.domain.graeae.engine import get_graeae_engine
        engine = get_graeae_engine()

        # v3.2 Custom Query mode: resolve the caller's lineup from the
        # three optional selectors on the request body. Precedence:
        # models > providers > tier > auto. `_resolve_selection` is
        # HTTPException-raising on bad input (unknown provider, unknown
        # model_id, unknown tier, empty tier result set).
        selection = await _resolve_selection(
            engine=engine,
            models=body.models,
            providers=body.providers,
            tier=body.tier,
        )

        result = await engine.consult(
            body.prompt, body.task_type, selection=selection, mode=body.mode,
        )
        result = _require_non_empty_consultation_result(result, body.mode)

        if body.limit_chars and result.get("all_responses"):
            for provider, resp in result["all_responses"].items():
                if isinstance(resp.get("response_text"), str):
                    original_len = len(resp["response_text"])
                    resp["response_text"] = resp["response_text"][:body.limit_chars]
                    resp["truncated"] = original_len > body.limit_chars

        if body.format == "best" and result.get("all_responses"):
            best = max(result["all_responses"].items(), key=lambda x: x[1].get("final_score", 0))
            result["all_responses"] = {best[0]: best[1]}

        consultation_id = None
        memory_ids = _extract_memory_ids(result)
        delivery_ids: list[str] = []
        if _lc._pool and result.get("all_responses"):
            # Persistence reads consensus fields FROM THE ENGINE return
            # dict instead of re-deriving them locally. The engine's
            # _compute_consensus is the single source of truth for
            # winning_muse / consensus_response / consensus_score /
            # cost / latency_ms; previously this block ran its own
            # max() over all_responses, which diverged from the engine
            # whenever scoring rules changed and produced nonsense
            # rows on all-failure (max() of a dict with only errored
            # entries picks an arbitrary error).
            #
            # On all-failure _compute_consensus returns
            # consensus_response="" / consensus_score=0.0 /
            # winning_muse=None / cost=0.0 / latency_ms=0, all safe to
            # persist. The response row still lands so the caller
            # has a stable consultation_id and the audit chain is
            # unbroken.
            consensus_response = result.get("consensus_response", "") or ""
            consensus_score = float(result.get("consensus_score", 0.0) or 0.0)
            winning_muse = result.get("winning_muse")
            engine_cost = float(result.get("cost", 0.0) or 0.0)
            engine_latency_ms = int(result.get("latency_ms", 0) or 0)

            # All three writes — consultation row, audit entry, memory refs —
            # must commit as a single unit. If the audit write fails we MUST
            # abort the consultation row: tamper-evidence requires that a
            # committed consultation implies a committed audit chain link.
            try:
                backend = _lc._persistence_backend
                if backend is None:
                    raise RuntimeError("Persistence backend not available")
                async with backend.transactional() as tx:
                    conn = tx.conn
                    row = await conn.fetchrow(
                        """INSERT INTO graeae_consultations
                            (prompt, task_type, consensus_response, consensus_score,
                             winning_muse, cost, latency_ms, mode, owner_id, namespace)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                           RETURNING id""",
                        body.prompt,
                        body.task_type,
                        consensus_response[:500],
                        consensus_score,
                        winning_muse,
                        engine_cost,
                        engine_latency_ms,
                        body.mode,
                        user.user_id,
                        user.namespace,
                    )
                    consultation_id = row["id"] if row else None

                    await _write_audit_entry_on_conn(
                        conn=conn,
                        consultation_id=consultation_id,
                        prompt=body.prompt,
                        response=consensus_response,
                        task_type=body.task_type or "reasoning",
                        provider=winning_muse,
                        quality_score=consensus_score,
                    )
                    await _write_memory_refs_on_conn(
                        conn=conn,
                        consultation_id=consultation_id,
                        memory_ids=memory_ids,
                    )
                    delivery_ids = await backend.webhooks.dispatch_event(
                        tx,
                        "consultation.completed",
                        {
                            "consultation_id": str(consultation_id),
                            "task_type": body.task_type,
                            "winning_muse": result.get("winning_muse"),
                            "consensus_score": result.get("consensus_score"),
                            "owner_id": user.user_id,
                            "namespace": user.namespace,
                        },
                        owner_id=user.user_id,
                        namespace=user.namespace,
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[CONSULTATION] persist failed — aborting: {e}", exc_info=True)
                raise HTTPException(
                    status_code=503,
                    detail="Consultation persistence failed; audit trail is required.",
                )

        _schedule_outbox_deliveries(delivery_ids)
        if consultation_id is not None:
            from mnemos.nats import publish_event as _nats_publish_event
            from mnemos.nats.client import get_node_name as _nats_get_node_name
            safe_ns = (user.namespace or "default").replace(".", "_")
            await _nats_publish_event(
                f"mnemos.consultation.completed.{safe_ns}",
                {
                    "consultation_id": str(consultation_id),
                    "task_type": body.task_type,
                    "mode": body.mode,
                    "winning_muse": result.get("winning_muse"),
                    "consensus_score": result.get("consensus_score"),
                    "namespace": user.namespace,
                    "user_id": user.user_id,
                    "source_node": _nats_get_node_name(),
                },
                msg_id=f"{consultation_id}.completed",
            )

        return ConsultationResponse(
            # asyncpg returns UUID columns as uuid.UUID objects, not strings.
            # ConsultationResponse.consultation_id is typed str, so coerce.
            consultation_id=str(consultation_id) if consultation_id is not None else None,
            all_responses=result.get("all_responses", {}),
            consensus_response=result.get("consensus_response"),
            consensus_score=result.get("consensus_score"),
            winning_muse=result.get("winning_muse"),
            cost=result.get("cost"),
            latency_ms=result.get("latency_ms"),
            mode=body.mode,
            timestamp=result.get("timestamp", ""),
            round_1=result.get("round_1"),
            round_2=result.get("round_2"),
            quorum_reached=result.get("quorum_reached"),
            quorum_threshold=result.get("quorum_threshold"),
            similarity_pairs=result.get("similarity_pairs"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONSULTATION] Error: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Consultation failed — see server logs for details")


# ── Audit log endpoints (declared before dynamic /{consultation_id} to prevent
#    'audit' string being matched as a UUID path param) ───────────────────────

@router.get("/consultations/audit", response_model=List[AuditLogEntry])
@limiter.limit("30/minute")
async def list_audit_log(
    request: Request,
    limit: int = Query(20, le=100),
    offset: int = 0,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """List GRAEAE audit log entries (newest first).

    Non-root callers only see audit rows for their own consultations. Root
    callers keep the operational global view.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_ns = scope_namespace(user, namespace)
    async with _lc.get_pool_manager().acquire() as conn:
        root = is_root(user)
        if root and namespace is None:
            rows = await conn.fetch(
                "SELECT id, sequence_num, consultation_id, prompt_hash, response_hash, "
                "chain_hash, prev_id, task_type, provider, quality_score, created_at "
                "FROM graeae_audit_log ORDER BY sequence_num DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )
        elif root:
            rows = await conn.fetch(
                "SELECT al.id, al.sequence_num, al.consultation_id, al.prompt_hash, al.response_hash, "
                "al.chain_hash, al.prev_id, al.task_type, al.provider, al.quality_score, al.created_at "
                "FROM graeae_audit_log al "
                "JOIN graeae_consultations c ON c.id = al.consultation_id "
                "WHERE c.namespace = $1 "
                "ORDER BY al.sequence_num DESC LIMIT $2 OFFSET $3",
                target_ns, limit, offset,
            )
        else:
            rows = await conn.fetch(
                "WITH visible AS ("
                "SELECT al.id, al.sequence_num AS global_sequence_num, "
                "al.consultation_id, al.prompt_hash, al.response_hash, "
                "al.task_type, al.provider, al.quality_score, "
                "al.created_at, "
                "ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
                "LAG(al.id) OVER (ORDER BY al.sequence_num ASC) AS scoped_prev_id "
                "FROM graeae_audit_log al "
                "JOIN graeae_consultations c ON c.id = al.consultation_id "
                "WHERE c.owner_id = $1 AND c.namespace = $2 "
                ") "
                "SELECT id, scoped_sequence_num AS sequence_num, consultation_id, "
                "prompt_hash, response_hash, NULL::text AS chain_hash, "
                "scoped_prev_id AS prev_id, "
                "task_type, provider, quality_score, created_at "
                "FROM visible "
                "ORDER BY global_sequence_num DESC LIMIT $3 OFFSET $4",
                user.user_id, target_ns, limit, offset,
            )
    return [
        AuditLogEntry(
            id=str(r["id"]),
            sequence_num=r["sequence_num"],
            consultation_id=str(r["consultation_id"]) if r["consultation_id"] else None,
            prompt_hash=r["prompt_hash"],
            response_hash=r["response_hash"],
            chain_hash=r["chain_hash"] if root else None,
            prev_id=str(r["prev_id"]) if r["prev_id"] else None,
            task_type=r.get("task_type"),
            provider=r.get("provider"),
            quality_score=r.get("quality_score"),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.get("/consultations/audit/verify", response_model=AuditVerifyResponse)
@limiter.limit("5/minute")
async def verify_audit_chain(
    request: Request,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Verify the integrity of the hash chain in the GRAEAE audit log.

    Root walks the entire chain from genesis, verifying each link. Non-root
    callers verify only rows attached to their own consultations while deriving
    each predecessor from the immediate previous global sequence row, not from
    the row's tamperable prev_id. Rate-limited because the cost grows linearly
    with audit-log size. Returns details of any broken sequences.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_ns = scope_namespace(user, namespace)
    verify_global_chain = is_root(user) and namespace is None
    async with _lc.get_pool_manager().acquire() as conn:
        if verify_global_chain:
            rows = await conn.fetch(
                "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id "
                "FROM graeae_audit_log ORDER BY sequence_num ASC"
            )
        elif is_root(user):
            rows = await conn.fetch(
                "SELECT al.sequence_num, "
                "ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
                "al.prompt_hash, al.response_hash, "
                "al.chain_hash, al.prev_id, al.prev_chain_hash, "
                "prev.chain_hash AS expected_prev_hash "
                "FROM graeae_audit_log al "
                "JOIN graeae_consultations c ON c.id = al.consultation_id "
                "LEFT JOIN LATERAL ("
                "SELECT chain_hash "
                "FROM graeae_audit_log "
                "WHERE sequence_num < al.sequence_num "
                "ORDER BY sequence_num DESC "
                "LIMIT 1"
                ") prev ON TRUE "
                "WHERE c.namespace = $1 "
                "ORDER BY al.sequence_num ASC",
                target_ns,
            )
        else:
            rows = await conn.fetch(
                "SELECT al.sequence_num, "
                "ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
                "al.prompt_hash, al.response_hash, "
                "al.chain_hash, al.prev_id, al.prev_chain_hash, "
                "prev.chain_hash AS expected_prev_hash "
                "FROM graeae_audit_log al "
                "JOIN graeae_consultations c ON c.id = al.consultation_id "
                "LEFT JOIN LATERAL ("
                "SELECT chain_hash "
                "FROM graeae_audit_log "
                "WHERE sequence_num < al.sequence_num "
                "ORDER BY sequence_num DESC "
                "LIMIT 1"
                ") prev ON TRUE "
                "WHERE c.owner_id = $1 AND c.namespace = $2 "
                "ORDER BY al.sequence_num ASC",
                user.user_id, target_ns,
            )

    if not rows:
        return AuditVerifyResponse(
            valid=True,
            entries_checked=0,
            message=(
                "Audit log is empty"
                if verify_global_chain
                else "No audit entries visible for caller"
            ),
        )

    if verify_global_chain:
        prev_chain = _GENESIS_HASH
        failures: dict[int, str] = {}
        for row in rows:
            expected = hashlib.sha256(
                (prev_chain + row["prompt_hash"] + row["response_hash"]).encode()
            ).hexdigest()
            if expected != row["chain_hash"]:
                failures.setdefault(
                    row["sequence_num"],
                    f"Chain broken at sequence {row['sequence_num']}: "
                    f"expected {expected[:16]}…, stored {row['chain_hash'][:16]}…",
                )
            prev_chain = row["chain_hash"]

        if failures:
            entries_failed = sorted(failures)
            first_broken_sequence = entries_failed[0]
            message = failures[first_broken_sequence]
            if len(entries_failed) > 1:
                message = f"{message}; {len(entries_failed)} entries failed verification"
            return AuditVerifyResponse(
                valid=False,
                entries_checked=len(rows),
                first_broken_sequence=first_broken_sequence,
                entries_failed=entries_failed,
                message=message,
            )

        return AuditVerifyResponse(
            valid=True,
            entries_checked=len(rows),
            message=f"All {len(rows)} entries verified — chain intact",
        )

    failures: dict[int, str] = {}
    for row in rows:
        scoped_sequence_num = row["scoped_sequence_num"]
        stored_prev_chain = row["prev_chain_hash"]
        prev_chain = row["expected_prev_hash"] or _GENESIS_HASH
        if stored_prev_chain and stored_prev_chain != prev_chain:
            logger.warning(
                "Scoped audit predecessor mismatch for user=%s scoped_row=%s "
                "global_sequence=%s expected_prev_hash=%s stored_prev_hash=%s",
                user.user_id,
                scoped_sequence_num,
                row["sequence_num"],
                prev_chain,
                stored_prev_chain,
            )
            failures.setdefault(
                scoped_sequence_num,
                f"Scoped chain broken at row {scoped_sequence_num}: "
                "stored previous hash does not match actual previous row",
            )
        expected = hashlib.sha256(
            (prev_chain + row["prompt_hash"] + row["response_hash"]).encode()
        ).hexdigest()
        if expected != row["chain_hash"]:
            logger.warning(
                "Scoped audit hash mismatch for user=%s scoped_row=%s "
                "global_sequence=%s expected_hash=%s stored_hash=%s",
                user.user_id,
                scoped_sequence_num,
                row["sequence_num"],
                expected,
                row["chain_hash"],
            )
            failures.setdefault(
                scoped_sequence_num,
                f"Hash mismatch at row {scoped_sequence_num}",
            )

    if failures:
        entries_failed = sorted(failures)
        first_broken_sequence = entries_failed[0]
        message = failures[first_broken_sequence]
        if len(entries_failed) > 1:
            message = f"{message}; {len(entries_failed)} scoped entries failed verification"
        return AuditVerifyResponse(
            valid=False,
            entries_checked=len(rows),
            first_broken_sequence=first_broken_sequence,
            entries_failed=entries_failed,
            message=message,
        )

    return AuditVerifyResponse(
        valid=True,
        entries_checked=len(rows),
        message=f"All {len(rows)} scoped entries verified - chain intact",
    )


# ── Static muse / mode listings ────────────────────────────────────────────────
# MUST be declared BEFORE the parametric /{consultation_id} route below so
# FastAPI doesn't try to parse "muses" or "modes" as a UUID and 500 in asyncpg.

@router.get("/consultations/muses")
async def list_muses(_: UserContext = Depends(get_current_user)):
    """List the live GRAEAE muse manifest.

    Pulls model + weight + api shape from the engine's in-memory provider
    map, which is auto-refreshed from model_registry at startup and on
    /admin/graeae/reload-providers. Operators can use this to confirm the
    daily provider sync rotated to current model_ids.
    """
    from mnemos.domain.graeae.engine import get_graeae_engine
    engine = get_graeae_engine()
    muses = []
    for name, cfg in engine.providers.items():
        muses.append({
            "name": name,
            "model": cfg.get("model"),
            "weight": cfg.get("weight"),
            "api": cfg.get("api"),
            "key_name": cfg.get("key_name"),
        })
    return {"count": len(muses), "muses": muses}


@router.get("/consultations/modes")
async def list_modes(_: UserContext = Depends(get_current_user)):
    """List supported consultation routing and reasoning-shape modes."""
    return {
        "modes": [
            {
                "name": "auto",
                "description": "engine picks the default routing strategy based on task_type",
            },
            {
                "name": "local",
                "description": "force local-only muses where configured (no commercial APIs)",
            },
            {
                "name": "external",
                "description": "force external commercial muses where configured",
            },
            {
                "name": "all",
                "description": "fan out to every available muse",
            },
            {
                "name": "single",
                "description": "pick exactly one highest-weighted muse; fastest and lowest-cost path",
            },
            {
                "name": "debate",
                "description": "run a two-round cross-muse debate and return the refined round",
            },
            {
                "name": "majority",
                "description": "fan out to up to three muses and report whether quorum agreement was reached",
            },
        ],
        "validation": {
            "supported": list(SUPPORTED_CONSULTATION_MODES),
            "unknown_mode_status": 422,
            "unknown_mode_detail": (
                "The request schema validates mode with a Pydantic Literal; "
                "unknown modes are rejected before business logic runs."
            ),
        },
    }


# ── Dynamic /{consultation_id} routes (declared after static /audit above) ────

@router.get("/consultations/{consultation_id}")
async def get_consultation(
    consultation_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Retrieve a consultation by ID.

    Scoped to the calling user: non-root callers only see their own
    consultations. Not-yours and not-exists both return 404 so we don't
    leak which consultation IDs are in use across users.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_ns = scope_namespace(user, namespace)

    async with _lc.get_pool_manager().acquire() as conn:
        if is_root(user) and namespace is None:
            row = await conn.fetchrow(
                "SELECT id, prompt, task_type, consensus_response, consensus_score, "
                "winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id = $1",
                consultation_id,
            )
        elif is_root(user):
            row = await conn.fetchrow(
                "SELECT id, prompt, task_type, consensus_response, consensus_score, "
                "winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id = $1 AND namespace = $2",
                consultation_id, target_ns,
            )
        else:
            row = await conn.fetchrow(
                "SELECT id, prompt, task_type, consensus_response, consensus_score, "
                "winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id = $1 AND owner_id = $2 AND namespace = $3",
                consultation_id, user.user_id, target_ns,
            )

    if not row:
        raise HTTPException(status_code=404, detail="Consultation not found")

    return {
        "id": str(row["id"]),
        "prompt": row["prompt"],
        "task_type": row["task_type"],
        "consensus_response": row["consensus_response"],
        "consensus_score": row["consensus_score"],
        "winning_muse": row["winning_muse"],
        "cost": row["cost"],
        "latency_ms": row["latency_ms"],
        "mode": row["mode"],
        "created_at": row["created"].isoformat(),
    }


@router.get("/consultations/{consultation_id}/artifacts")
async def get_consultation_artifacts(
    consultation_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Retrieve structured outputs and citations from a consultation."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_ns = scope_namespace(user, namespace)

    async with _lc.get_pool_manager().acquire() as conn:
        # Get consultation — scoped to caller unless root.
        if is_root(user) and namespace is None:
            consultation = await conn.fetchrow(
                "SELECT id, created FROM graeae_consultations WHERE id = $1",
                consultation_id,
            )
        elif is_root(user):
            consultation = await conn.fetchrow(
                "SELECT id, created FROM graeae_consultations "
                "WHERE id = $1 AND namespace = $2",
                consultation_id, target_ns,
            )
        else:
            consultation = await conn.fetchrow(
                "SELECT id, created FROM graeae_consultations "
                "WHERE id = $1 AND owner_id = $2 AND namespace = $3",
                consultation_id, user.user_id, target_ns,
            )
        if not consultation:
            raise HTTPException(status_code=404, detail="Consultation not found")

        # Get referenced memories
        memory_refs = await conn.fetch(
            "SELECT memory_id, injected_at FROM consultation_memory_refs "
            "WHERE consultation_id = $1 ORDER BY injected_at",
            consultation_id,
        )

    return ConsultationArtifact(
        consultation_id=str(consultation["id"]),
        citations=[str(ref["memory_id"]) for ref in memory_refs],
        memory_refs=[
            {
                "memory_id": str(ref["memory_id"]),
                "injected_at": ref["injected_at"].isoformat(),
            }
            for ref in memory_refs
        ],
        created_at=consultation["created"].isoformat(),
    )

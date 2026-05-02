"""MORPHEUS dream-state admin/observability endpoints.

  GET    /v1/morpheus/runs                — list dream runs (newest first)
  GET    /v1/morpheus/runs/{run_id}        — single run details
  POST   /admin/morpheus/runs              — manually trigger a dream
                                             (root only — runs synchronously
                                             so the caller sees the final
                                             state)
  DELETE /admin/morpheus/runs/{run_id}     — roll back a run by deleting
                                             run-created rows and restoring
                                             tagged in-place mutations
                                             (root only)

Slice 1 shipped the surface. Slice 2 filled in
REPLAY/CLUSTER/SYNTHESISE. Slice 3 adds optional CONSOLIDATE.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.routes._postgres_only import _require_postgres_backend
from mnemos.domain.morpheus.runner import rollback_run, run_dream

logger = logging.getLogger(__name__)
router = APIRouter(tags=["morpheus"])


class MorpheusRun(BaseModel):
    id: str
    started_at: str
    finished_at: Optional[str] = None
    status: str
    phase: Optional[str] = None
    triggered_by: str
    window_started_at: Optional[str] = None
    window_ended_at: Optional[str] = None
    window_hours: int
    cluster_min_size: int
    memories_scanned: int
    clusters_found: int
    summaries_created: int
    memories_consolidated: int = 0
    clusters_consolidated: int = 0
    error: Optional[str] = None
    config: dict = Field(default_factory=dict)
    namespace: Optional[str] = None


class MorpheusRunList(BaseModel):
    count: int
    runs: List[MorpheusRun]


class MorpheusTriggerRequest(BaseModel):
    window_hours: int = Field(168, ge=1, le=8760)        # 1h … 1 year
    cluster_min_size: int = Field(3, ge=2, le=100)
    consolidate: bool = Field(
        False,
        description=(
            "Enable the optional CONSOLIDATE phase between CLUSTER and "
            "SYNTHESISE for this run."
        ),
    )
    config: dict = Field(default_factory=dict)
    namespace: Optional[str] = Field(
        None,
        description=(
            "Optional tenant scope. When set, the run only considers "
            "memories with this namespace. Default = all namespaces."
        ),
    )


class MorpheusRollbackResponse(BaseModel):
    run_id: str
    memories_deleted: int
    run_status: str = "rolled_back"


class MorpheusCluster(BaseModel):
    cluster_id: int
    member_memory_ids: List[str]
    member_count: int
    synthesised_memory_id: Optional[str] = None


class MorpheusClusterList(BaseModel):
    run_id: str
    count: int
    clusters: List[MorpheusCluster]


def _row_to_run(r) -> MorpheusRun:
    keys = r.keys()
    return MorpheusRun(
        id=str(r["id"]),
        started_at=r["started_at"].isoformat() if r["started_at"] else "",
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        status=r["status"],
        phase=r["phase"],
        triggered_by=r["triggered_by"],
        window_started_at=(r["window_started_at"].isoformat()
                           if r["window_started_at"] else None),
        window_ended_at=(r["window_ended_at"].isoformat()
                         if r["window_ended_at"] else None),
        window_hours=r["window_hours"],
        cluster_min_size=r["cluster_min_size"],
        memories_scanned=r["memories_scanned"],
        clusters_found=r["clusters_found"],
        summaries_created=r["summaries_created"],
        memories_consolidated=(
            r["memories_consolidated"] if "memories_consolidated" in keys else 0
        ),
        clusters_consolidated=(
            r["clusters_consolidated"] if "clusters_consolidated" in keys else 0
        ),
        error=r["error"],
        config=dict(r["config"]) if isinstance(r["config"], dict) else {},
        namespace=r["namespace"] if "namespace" in keys else None,
    )


@router.get("/v1/morpheus/runs", response_model=MorpheusRunList)
async def list_runs(
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(None, pattern=r"^(running|success|failed|rolled_back)$"),
    _: UserContext = Depends(require_root),
):
    """List MORPHEUS runs newest-first.

    Operator-only telemetry endpoint. Returned rows can include tenant
    namespaces, configs, errors, and synthesized memory IDs.
    """
    _require_postgres_backend()
    where = ""
    args: list = []
    if status:
        args.append(status)
        where = f" WHERE status = ${len(args)}"
    args.append(limit)
    sql = (
        "SELECT id, started_at, finished_at, status, phase, triggered_by, "
        "       window_started_at, window_ended_at, window_hours, "
        "       cluster_min_size, memories_scanned, clusters_found, "
        "       summaries_created, memories_consolidated, "
        "       clusters_consolidated, error, config, namespace "
        f"FROM morpheus_runs{where} "
        f"ORDER BY started_at DESC LIMIT ${len(args)}"
    )
    async with _lc.get_pool_manager().acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return MorpheusRunList(count=len(rows), runs=[_row_to_run(r) for r in rows])


@router.get("/v1/morpheus/runs/{run_id}", response_model=MorpheusRun)
async def get_run(run_id: str, _: UserContext = Depends(require_root)):
    """Return one MORPHEUS run. Operator-only telemetry."""
    _require_postgres_backend()
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, started_at, finished_at, status, phase, triggered_by, "
            "       window_started_at, window_ended_at, window_hours, "
            "       cluster_min_size, memories_scanned, clusters_found, "
            "       summaries_created, memories_consolidated, "
            "       clusters_consolidated, error, config, namespace "
            "FROM morpheus_runs WHERE id=$1::uuid",
            run_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"morpheus run {run_id} not found")
    return _row_to_run(row)


@router.get(
    "/v1/morpheus/runs/{run_id}/clusters",
    response_model=MorpheusClusterList,
)
async def list_clusters(run_id: str, _: UserContext = Depends(require_root)):
    """Read out the cluster grouping a MORPHEUS run produced.

    Operator-only telemetry: cluster members expose raw memory IDs.

    Slice 2's phase_cluster persists clusters to morpheus_runs.config
    under the "clusters" key as a JSONB list of
    {cluster_id, member_memory_ids: [...]}. This endpoint pulls that
    payload and joins it against the synthesised memories so each
    cluster can be inspected with its summary id.

    Returns 404 if the run doesn't exist. Returns an empty list if
    the run never reached the cluster phase or produced zero clusters
    above cluster_min_size.
    """
    _require_postgres_backend()
    async with _lc.get_pool_manager().acquire() as conn:
        config_raw = await conn.fetchval(
            "SELECT config FROM morpheus_runs WHERE id=$1::uuid", run_id,
        )
        if config_raw is None:
            # Disambiguate "run not found" from "run found, no clusters yet".
            exists = await conn.fetchval(
                "SELECT 1 FROM morpheus_runs WHERE id=$1::uuid", run_id,
            )
            if not exists:
                raise HTTPException(
                    status_code=404,
                    detail=f"morpheus run {run_id} not found",
                )
            return MorpheusClusterList(run_id=run_id, count=0, clusters=[])

        # Read synthesised memories for this run so we can attach
        # synthesised_memory_id to each cluster.
        synth_rows = await conn.fetch(
            "SELECT id, source_memories FROM memories "
            "WHERE morpheus_run_id=$1::uuid "
            "  AND provenance='morpheus_local' "
            "  AND deleted_at IS NULL",
            run_id,
        )

    config = config_raw if isinstance(config_raw, dict) else {}
    raw_clusters = config.get("clusters") or []

    # Build a quick lookup: any synthesised memory whose source set
    # exactly matches a cluster's member set is that cluster's summary.
    synth_by_sources: dict = {}
    for sr in synth_rows:
        sources = tuple(sorted(sr["source_memories"] or []))
        if sources:
            synth_by_sources[sources] = sr["id"]

    out: List[MorpheusCluster] = []
    for c in raw_clusters:
        members = list(c.get("member_memory_ids") or [])
        synth_id = synth_by_sources.get(tuple(sorted(members)))
        out.append(MorpheusCluster(
            cluster_id=int(c.get("cluster_id", len(out))),
            member_memory_ids=members,
            member_count=len(members),
            synthesised_memory_id=synth_id,
        ))
    return MorpheusClusterList(run_id=run_id, count=len(out), clusters=out)


@router.post("/admin/morpheus/runs", response_model=MorpheusRun, status_code=201)
async def trigger_run(
    request: MorpheusTriggerRequest,
    _: UserContext = Depends(require_root),
):
    """Manually trigger a MORPHEUS run.

    Runs synchronously so the caller sees the final state. For long
    windows (e.g. 7-day = 168h) the LLM pass in slice 2 may take
    minutes — at which point the trigger should move to a background
    task with the caller polling /v1/morpheus/runs/{id}. Slice 1's
    runner is a no-op so this returns near-instantly.
    """
    _require_postgres_backend()
    run_config = dict(request.config)
    if request.consolidate:
        run_config["consolidate"] = True
    else:
        run_config.setdefault("consolidate", False)
    run_id = await run_dream(
        _lc._pool,
        triggered_by="api",
        window_hours=request.window_hours,
        cluster_min_size=request.cluster_min_size,
        config=run_config,
        namespace=request.namespace,
    )
    async with _lc.get_pool_manager().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, started_at, finished_at, status, phase, triggered_by, "
            "       window_started_at, window_ended_at, window_hours, "
            "       cluster_min_size, memories_scanned, clusters_found, "
            "       summaries_created, memories_consolidated, "
            "       clusters_consolidated, error, config, namespace "
            "FROM morpheus_runs WHERE id=$1::uuid",
            run_id,
        )
    return _row_to_run(row)


@router.delete("/admin/morpheus/runs/{run_id}", response_model=MorpheusRollbackResponse)
async def rollback(
    run_id: str,
    _: UserContext = Depends(require_root),
):
    """Roll back a MORPHEUS run.

    Run-created synthesis rows are deleted. Consolidated originals are
    restored in place from their metadata audit. Idempotent: running
    rollback on an already-rolled-back run returns `memories_deleted: 0`
    and leaves the run status at 'rolled_back'. Returns 404 if the
    run_id doesn't exist.
    """
    _require_postgres_backend()
    async with _lc.get_pool_manager().acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM morpheus_runs WHERE id=$1::uuid", run_id,
        )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"morpheus run {run_id} not found")
    try:
        n_deleted, _n_run = await rollback_run(_lc._pool, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return MorpheusRollbackResponse(run_id=run_id, memories_deleted=n_deleted)

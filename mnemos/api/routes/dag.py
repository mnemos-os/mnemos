"""DAG (Directed Acyclic Graph) endpoints for memory versioning.

Implements git-like operations on memory history:
- log: Walk commit DAG from HEAD to root
- branches: List all branches for a memory
- branch: Create new branch from HEAD or specific commit
- checkout: Fetch commit content by hash
- merge: Merge source_branch into target_branch
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.api.routes._postgres_only import _require_postgres_backend
from mnemos.api.routes.memories import _schedule_outbox_deliveries
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _branch_advisory_lock_key(memory_id: str, branch: str, _hashlib_mod=None) -> int:
    """Stable signed-int64 advisory lock key for a (memory_id, branch).

    All DAG writers — merge_branch + revert_memory feature-branch path —
    must take pg_advisory_xact_lock on the same key for the same
    (memory, branch) pair so they serialize against each other and
    a revert can't orphan a concurrent merge (or vice versa). The
    "dag-branch" prefix is generic across mutation kinds; the
    function lives here in dag.py and is imported by
    api/handlers/versions.py.
    """
    if _hashlib_mod is None:
        import hashlib as _hashlib_mod
    digest = _hashlib_mod.sha256(
        f"dag-branch:{memory_id}:{branch}".encode("utf-8")
    ).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    if key >= 2**63:
        key -= 2**64
    return key


async def _assert_memory_writable(conn, memory_id: str, user: UserContext) -> None:
    """Ensure the caller can modify this memory. Raises 404 otherwise.

    Root can access any memory; non-root writers are scoped to exact
    owner_id AND namespace. We return 404 to avoid leaking existence.
    """
    row = await conn.fetchrow(
        "SELECT owner_id, namespace FROM memories WHERE id = $1", memory_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    if user.role != "root" and (
        row["owner_id"] != user.user_id
        or row["namespace"] != user.namespace
    ):
        raise HTTPException(status_code=404, detail="Memory not found")


async def _assert_memory_readable(conn, memory_id: str, user: UserContext) -> None:
    """Ensure DAG read endpoints match memory read visibility semantics."""
    if user.role == "root":
        row = await conn.fetchrow("SELECT 1 FROM memories WHERE id = $1", memory_id)
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        return

    visibility = VisibilityFilter.for_read(user, namespace=user.namespace)
    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        row = await conn.fetchrow("SELECT 1 FROM memories WHERE id = $1", memory_id)
    else:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM memories
            WHERE id = $1
              AND namespace = $2
              AND (
                    owner_id = $3
                 OR (permission_mode % 10) >= 4
                 OR ((permission_mode / 10) % 10) >= 4 AND group_id = ANY($4::text[])
              )
            """,
            memory_id,
            visibility.namespace,
            visibility.user_id,
            list(visibility.group_ids),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/memories", tags=["dag"])


# ────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ────────────────────────────────────────────────────────────────────────────

class CommitInfo(BaseModel):
    commit_hash: str
    version_num: int
    parent_hash: Optional[str] = None
    branch: str
    content: str
    category: str
    subcategory: Optional[str] = None
    snapshot_at: str
    snapshot_by: Optional[str] = None
    change_type: str  # create, update, delete


class BranchInfo(BaseModel):
    name: str
    head_commit_hash: str
    created_at: str
    created_by: Optional[str] = None


class BranchCreateRequest(BaseModel):
    name: str
    from_commit: Optional[str] = None  # commit hash; default = HEAD


class MergeRequest(BaseModel):
    source_branch: str
    strategy: str = "latest-wins"  # latest-wins or manual


class MergeResult(BaseModel):
    success: bool
    new_commit_hash: Optional[str] = None
    message: str


def _require_pool(*, route_label: str = "/v1/dag"):
    """Return the asyncpg pool or emit a profile-aware 503.

    Wraps ``require_postgres_pool_or_503`` so DAG endpoints share
    the canonical SQLite/edge-profile detail with the rest of the
    Postgres-only routes.
    """
    require_postgres_pool_or_503(route_label=route_label)
    return _lc._pool


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────

@router.get("/{memory_id}/log", response_model=List[CommitInfo])
async def get_memory_log(
    memory_id: str,
    branch: str = Query("main", description="Branch to walk from HEAD"),
    limit: int = Query(50, le=500),
    user: UserContext = Depends(get_current_user),
):
    """Walk commit DAG from branch HEAD to root.

    Returns commit history (commits reachable from HEAD via parent pointers).
    Equivalent to `git log`.
    """
    pool = _require_pool(route_label="GET /v1/memories/{memory_id}/log")

    try:
        async with pool.acquire() as conn:
            await _assert_memory_readable(conn, memory_id, user)
            # Recursive CTE: walk from HEAD backward through parent_version_id.
            # Carries owner_id/namespace/permission_mode and the actual parent
            # identity through both arms so the post-walk filter can drop
            # snapshots the caller can't read without inventing parent edges.
            rows = await conn.fetch(
                """
                WITH RECURSIVE commit_walk AS (
                    -- Base: START from branch HEAD
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        parent_mv.commit_hash AS parent_commit_hash,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        1 AS depth
                    FROM memory_versions mv
                    LEFT JOIN memory_versions parent_mv
                        ON parent_mv.id = mv.parent_version_id
                       AND parent_mv.memory_id = mv.memory_id
                    INNER JOIN memory_branches mb ON (
                        mb.memory_id = mv.memory_id AND
                        mb.name = $2 AND
                        mb.head_version_id = mv.id
                    )
                    WHERE mv.memory_id = $1
                    UNION ALL
                    -- Recursive: WALK backward via parent_version_id.
                    -- Same-memory predicate (mv.memory_id =
                    -- cw.memory_id) prevents corrupt parent_version_id
                    -- from pulling another memory's version into this
                    -- memory's log (round-38 finding). Cross-memory
                    -- parent edges silently drop out of the walk; the
                    -- HTTP log surfaces only intra-memory ancestry.
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        parent_mv.commit_hash AS parent_commit_hash,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        cw.depth + 1
                    FROM memory_versions mv
                    LEFT JOIN memory_versions parent_mv
                        ON parent_mv.id = mv.parent_version_id
                       AND parent_mv.memory_id = mv.memory_id
                    INNER JOIN commit_walk cw
                        ON mv.id = cw.parent_version_id
                       AND mv.memory_id = cw.memory_id
                    WHERE cw.depth < $3
                )
                SELECT
                    id, commit_hash, parent_version_id, parent_commit_hash,
                    version_num, branch, content, category, subcategory,
                    snapshot_at, snapshot_by, change_type,
                    owner_id, namespace, permission_mode
                FROM commit_walk
                ORDER BY depth ASC
                LIMIT $3
                """,
                memory_id,
                branch,
                limit,
            )

            if not rows:
                raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found")

            # Per-snapshot tenancy filter (slice 2 round 15). Applied
            # client-side because the recursive CTE doesn't compose
            # cleanly with a snapshot-level WHERE. A memory created
            # private (mode 600) → snapshotted into v1 → relaxed to
            # public (mode 644) MUST NOT expose v1 to readers who
            # only became authorized after the permission flip.
            # Mirrors api/handlers/versions.py + mnemos/db/mcp_repo.py.
            if user.role != "root":
                def _snap_visible(r) -> bool:
                    if r["namespace"] != user.namespace:
                        return False
                    return (
                        r["owner_id"] == user.user_id
                        or (r["permission_mode"] % 10) >= 4
                    )
                rows = [r for r in rows if _snap_visible(r)]

            # Assemble with parent hashes. The parent edge is reported only if
            # the actual immediate parent survived the same visibility filter.
            # Do not chain to the next visible row: an invisible snapshot in
            # between means the visible child has no visible parent edge.
            visible_ids = {r["id"] for r in rows}
            commits = []
            for row in rows:
                parent_hash = (
                    row["parent_commit_hash"]
                    if row["parent_version_id"] in visible_ids
                    else None
                )
                commits.append(
                    CommitInfo(
                        commit_hash=row["commit_hash"],
                        version_num=row["version_num"],
                        parent_hash=parent_hash,
                        branch=row["branch"],
                        content=row["content"],
                        category=row["category"],
                        subcategory=row["subcategory"],
                        snapshot_at=row["snapshot_at"].isoformat(),
                        snapshot_by=row["snapshot_by"],
                        change_type=row["change_type"],
                    )
                )

            logger.info(f"[DAG] Log: {memory_id}/{branch} returned {len(commits)} commits")
            return commits

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Log failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")


@router.get("/{memory_id}/branches", response_model=List[BranchInfo])
async def get_memory_branches(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """List all branches for a memory."""
    pool = _require_pool(route_label="GET /v1/memories/{memory_id}/branches")

    try:
        async with pool.acquire() as conn:
            await _assert_memory_readable(conn, memory_id, user)
            # Scope the JOIN by memory_id too — a stale/corrupt
            # branch row pointing at another memory's version_id
            # would otherwise leak that other memory's commit_hash
            # through `head_commit_hash` (round-37 finding). With
            # the scoped JOIN, mismatched rows return NULL for
            # commit_hash; the `head_commit_hash` field then
            # surfaces the corruption rather than papering over it.
            if user.role == "root":
                branches = await conn.fetch(
                    """
                    SELECT
                        mb.name, mv.commit_hash, mb.created_at, mb.created_by
                    FROM memory_branches mb
                    LEFT JOIN memory_versions mv
                        ON mv.id = mb.head_version_id
                       AND mv.memory_id = mb.memory_id
                    WHERE mb.memory_id = $1
                    ORDER BY mb.created_at DESC
                    """,
                    memory_id,
                )
            else:
                from mnemos.core.visibility import version_visibility_predicate

                vis_clause, vis_params = version_visibility_predicate(
                    user.user_id, start_param_idx=2, table_alias="mv",
                )
                ns_ph = f"${len(vis_params) + 2}"
                branches = await conn.fetch(
                    f"""
                    SELECT
                        mb.name, mv.commit_hash, mb.created_at, mb.created_by
                    FROM memory_branches mb
                    LEFT JOIN memory_versions mv
                        ON mv.id = mb.head_version_id
                       AND mv.memory_id = mb.memory_id
                       AND {vis_clause}
                       AND mv.namespace = {ns_ph}
                    WHERE mb.memory_id = $1
                    ORDER BY mb.created_at DESC
                    """,
                    memory_id, *vis_params, user.namespace,
                )

            # Filter out rows where the scoped JOIN returned NULL
            # commit_hash — those are branches pointing at a
            # foreign memory's version_id (or no row at all),
            # i.e. stale/corrupt pointers that this endpoint
            # MUST NOT silently surface. Log so the operator can
            # repair.
            result = []
            for b in branches:
                if b["commit_hash"] is None:
                    logger.error(
                        f"[DAG] Corrupt branch '{b['name']}' for memory "
                        f"{memory_id}: head_version_id points outside "
                        f"this memory or is not visible to the caller; "
                        f"omitting from response. Operator reconciliation "
                        f"may be required."
                    )
                    continue
                result.append(BranchInfo(
                    name=b["name"],
                    head_commit_hash=b["commit_hash"],
                    created_at=b["created_at"].isoformat(),
                    created_by=b["created_by"],
                ))
            return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Branches failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")


@router.post("/{memory_id}/branch", response_model=BranchInfo)
async def create_branch(
    memory_id: str,
    request: BranchCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create new branch from HEAD or specific commit hash."""
    pool = _require_pool(route_label="POST /v1/memories/{memory_id}/branch")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _assert_memory_writable(conn, memory_id, user)
                # The preflight check above is not authoritative for the
                # write: lock and re-check the live memory row in the same
                # transaction that resolves the start commit and inserts the
                # branch. This closes the HTTP/MCP TOCTOU asymmetry where a
                # memory owner or namespace can change between auth and write.
                if user.role == "root":
                    live = await conn.fetchrow(
                        "SELECT 1 FROM memories WHERE id = $1 FOR SHARE",
                        memory_id,
                    )
                else:
                    live = await conn.fetchrow(
                        "SELECT 1 FROM memories WHERE id = $1 "
                        "AND owner_id = $2 AND namespace = $3 FOR SHARE",
                        memory_id,
                        user.user_id,
                        user.namespace,
                    )
                if not live:
                    raise HTTPException(status_code=404, detail="Memory not found")

                # Resolve starting point (HEAD or specific commit) only after
                # the locked ownership/namespace re-check succeeds.
                if request.from_commit:
                    if user.role == "root":
                        start_version = await conn.fetchrow(
                            "SELECT id, commit_hash, created_at FROM memory_versions "
                            "WHERE memory_id = $1 AND commit_hash = $2",
                            memory_id,
                            request.from_commit,
                        )
                    else:
                        from mnemos.core.visibility import version_visibility_predicate

                        vis_clause, vis_params = version_visibility_predicate(
                            user.user_id, start_param_idx=3,
                        )
                        ns_ph = f"${len(vis_params) + 3}"
                        start_version = await conn.fetchrow(
                            "SELECT id, commit_hash, created_at FROM memory_versions "
                            "WHERE memory_id = $1 AND commit_hash = $2 "
                            f"AND {vis_clause} AND namespace = {ns_ph}",
                            memory_id, request.from_commit, *vis_params, user.namespace,
                        )
                    if not start_version:
                        raise HTTPException(status_code=404, detail="Commit hash not found")
                else:
                    # Default: use current main branch HEAD
                    if user.role == "root":
                        start_version = await conn.fetchrow(
                            """
                            SELECT mv.id, mv.commit_hash, mv.created_at
                            FROM memory_versions mv
                            INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                            WHERE mv.memory_id = $1 AND mb.name = 'main'
                            """,
                            memory_id,
                        )
                    else:
                        from mnemos.core.visibility import version_visibility_predicate

                        vis_clause, vis_params = version_visibility_predicate(
                            user.user_id, start_param_idx=2, table_alias="mv",
                        )
                        ns_ph = f"${len(vis_params) + 2}"
                        start_version = await conn.fetchrow(
                            f"""
                            SELECT mv.id, mv.commit_hash, mv.created_at
                            FROM memory_versions mv
                            INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                            WHERE mv.memory_id = $1 AND mb.name = 'main'
                              AND {vis_clause} AND mv.namespace = {ns_ph}
                            """,
                            memory_id, *vis_params, user.namespace,
                        )
                    if not start_version:
                        raise HTTPException(status_code=404, detail="main branch HEAD not found")

                # Race-safe insert: a concurrent creator for the same branch
                # yields no RETURNING row and is reported as a duplicate.
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (memory_id, name) DO NOTHING
                    RETURNING id
                    """,
                    memory_id,
                    request.name,
                    start_version["id"],
                    user.user_id,
                )
                if inserted is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Branch '{request.name}' already exists",
                    )

            logger.info(f"[DAG] Branch '{request.name}' created for {memory_id}")

            return BranchInfo(
                name=request.name,
                head_commit_hash=start_version["commit_hash"],
                created_at=start_version["created_at"].isoformat(),
                created_by=user.user_id,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Branch creation failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")


@router.get("/{memory_id}/commits/{commit_hash}", response_model=CommitInfo)
async def get_commit(
    memory_id: str,
    commit_hash: str,
    user: UserContext = Depends(get_current_user),
):
    """Fetch commit content by hash."""
    pool = _require_pool(route_label="GET /v1/memories/{memory_id}/commits/{commit_hash}")

    try:
        async with pool.acquire() as conn:
            await _assert_memory_readable(conn, memory_id, user)
            # Per-snapshot tenancy gate (slice 2 round 15). The
            # live-memory check above is necessary but not sufficient
            # — a snapshot taken when the row was private must remain
            # private to readers who can only access the now-public
            # live row.
            if user.role == "root":
                row = await conn.fetchrow(
                    """
                    SELECT
                        mv.commit_hash, mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        (SELECT commit_hash FROM memory_versions mv2
                         WHERE mv2.id = mv.parent_version_id
                           AND mv2.memory_id = mv.memory_id) AS parent_hash
                    FROM memory_versions mv
                    WHERE mv.memory_id = $1 AND mv.commit_hash = $2
                    """,
                    memory_id, commit_hash,
                )
            else:
                from mnemos.core.visibility import version_visibility_predicate
                # Two visibility predicates: one for the requested
                # row (alias mv), one for the parent subquery
                # (alias mv2). Codex round 16 flagged the parent
                # subquery as ungated — a public child whose parent
                # was a private snapshot would still leak the
                # parent's commit_hash, exposing hidden DAG
                # topology. Gate the parent equally; emit NULL for
                # parent_hash when the parent is invisible.
                vis_mv, vis_mv_params = version_visibility_predicate(
                    user.user_id, start_param_idx=3, table_alias="mv",
                )
                ns_mv_ph = f"${len(vis_mv_params) + 3}"
                # Parent predicate takes the same user_id; placeholder
                # offsets continue after the row's namespace param.
                vis_mv2, vis_mv2_params = version_visibility_predicate(
                    user.user_id,
                    start_param_idx=len(vis_mv_params) + 4,
                    table_alias="mv2",
                )
                ns_mv2_ph = f"${len(vis_mv_params) + len(vis_mv2_params) + 4}"
                row = await conn.fetchrow(
                    f"""
                    SELECT
                        mv.commit_hash, mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        (SELECT mv2.commit_hash FROM memory_versions mv2
                         WHERE mv2.id = mv.parent_version_id
                           AND mv2.memory_id = mv.memory_id
                           AND {vis_mv2} AND mv2.namespace = {ns_mv2_ph}) AS parent_hash
                    FROM memory_versions mv
                    WHERE mv.memory_id = $1 AND mv.commit_hash = $2
                      AND {vis_mv} AND mv.namespace = {ns_mv_ph}
                    """,
                    memory_id, commit_hash,
                    *vis_mv_params, user.namespace,
                    *vis_mv2_params, user.namespace,
                )

            if not row:
                raise HTTPException(status_code=404, detail="Commit not found")

            return CommitInfo(
                commit_hash=row["commit_hash"],
                version_num=row["version_num"],
                parent_hash=row["parent_hash"],
                branch=row["branch"],
                content=row["content"],
                category=row["category"],
                subcategory=row["subcategory"],
                snapshot_at=row["snapshot_at"].isoformat(),
                snapshot_by=row["snapshot_by"],
                change_type=row["change_type"],
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Commit fetch failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")


@router.post("/{memory_id}/merge", response_model=MergeResult)
async def merge_branch(
    memory_id: str,
    request: MergeRequest,
    target_branch: str = Query("main"),
    user: UserContext = Depends(get_current_user),
):
    """Merge source_branch into target_branch."""
    _require_pool(route_label="POST /v1/memories/{memory_id}/merge")
    backend = _require_postgres_backend()
    if request.strategy not in ("latest-wins", "manual"):
        raise HTTPException(status_code=400, detail="Invalid merge strategy")

    import hashlib as _hashlib

    lock_key = _branch_advisory_lock_key(memory_id, target_branch, _hashlib)
    delivery_ids: list[str] = []
    live_tracks_target = False
    merge_hash: str | None = None
    source_head = None
    merge_owner_id = user.user_id
    merge_namespace = user.namespace

    try:
        async with backend.transactional() as tx:
            conn = tx.conn
            await _assert_memory_writable(conn, memory_id, user)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

            if request.strategy == "manual":
                return MergeResult(success=False, message="Manual merge strategy not yet implemented")

            if user.role == "root":
                live = await conn.fetchrow(
                    "SELECT id, owner_id, namespace, permission_mode, content, category, "
                    "subcategory, metadata, verbatim_content FROM memories WHERE id = $1 FOR UPDATE",
                    memory_id,
                )
            else:
                live = await conn.fetchrow(
                    "SELECT id, owner_id, namespace, permission_mode, content, category, "
                    "subcategory, metadata, verbatim_content FROM memories "
                    "WHERE id = $1 AND owner_id = $2 AND namespace = $3 FOR UPDATE",
                    memory_id,
                    user.user_id,
                    user.namespace,
                )
            if live is None:
                raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")

            if user.role == "root":
                source_head = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.commit_hash, mv.content, mv.version_num,
                           mv.category, mv.subcategory, mv.metadata, mv.verbatim_content,
                           mv.source_model, mv.source_provider, mv.source_session, mv.source_agent
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = $2
                    """,
                    memory_id,
                    request.source_branch,
                )
            else:
                from mnemos.core.visibility import version_visibility_predicate

                vis_clause, vis_params = version_visibility_predicate(
                    user.user_id,
                    start_param_idx=3,
                    table_alias="mv",
                )
                ns_ph = f"${len(vis_params) + 3}"
                source_head = await conn.fetchrow(
                    f"""
                    SELECT mv.id, mv.commit_hash, mv.content, mv.version_num,
                           mv.category, mv.subcategory, mv.metadata, mv.verbatim_content,
                           mv.source_model, mv.source_provider, mv.source_session, mv.source_agent
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = $2
                      AND {vis_clause} AND mv.namespace = {ns_ph}
                    """,
                    memory_id,
                    request.source_branch,
                    *vis_params,
                    user.namespace,
                )
            if not source_head:
                raise HTTPException(status_code=404, detail=f"Source branch '{request.source_branch}' not found")

            target_branch_row = await conn.fetchrow(
                "SELECT head_version_id FROM memory_branches WHERE memory_id = $1 AND name = $2 FOR UPDATE",
                memory_id,
                target_branch,
            )
            if target_branch_row is None or target_branch_row["head_version_id"] is None:
                raise HTTPException(status_code=404, detail=f"Target branch '{target_branch}' not found")
            target_head_id = target_branch_row["head_version_id"]

            from mnemos.core.visibility import _assert_target_head_visible

            await _assert_target_head_visible(
                conn,
                target_head_id,
                user,
                f"Target branch '{target_branch}' not found",
            )
            target_head = await conn.fetchrow(
                """
                SELECT id, version_num, commit_hash, content, category, subcategory,
                       metadata, verbatim_content, owner_id, namespace, permission_mode
                FROM memory_versions
                WHERE id = $1 AND memory_id = $2
                """,
                target_head_id,
                memory_id,
            )
            if not target_head:
                raise HTTPException(status_code=404, detail=f"Target branch '{target_branch}' not found")

            if (
                source_head["content"] == target_head["content"]
                and source_head["category"] == target_head["category"]
                and source_head["subcategory"] == target_head["subcategory"]
                and source_head["metadata"] == target_head["metadata"]
                and source_head["verbatim_content"] == target_head["verbatim_content"]
            ):
                return MergeResult(
                    success=False,
                    new_commit_hash=target_head["commit_hash"],
                    message=(
                        f"Source branch '{request.source_branch}' has no versioned changes vs "
                        f"'{target_branch}'; no merge commit created"
                    ),
                )

            merge_owner_id = live["owner_id"]
            merge_namespace = live["namespace"]
            live_tracks_target = target_branch == "main"

            import hashlib as _hashlib_local
            import time as _time_local

            next_version = target_head["version_num"] + 1
            merge_hash = _hashlib_local.sha256(
                f"{memory_id}|{next_version}|{source_head['content']}|"
                f"merge-{request.source_branch}->{target_branch}-{int(_time_local.time() * 1_000_000)}".encode()
            ).hexdigest()

            meta_val = source_head["metadata"]
            if isinstance(meta_val, str):
                meta_str = meta_val
            elif meta_val is not None:
                import json as _json

                meta_str = _json.dumps(dict(meta_val))
            else:
                meta_str = "{}"

            new_version_id = await conn.fetchval(
                """
                INSERT INTO memory_versions (
                    memory_id, version_num, content, category, subcategory,
                    metadata, verbatim_content, owner_id, namespace, permission_mode,
                    source_model, source_provider, source_session, source_agent,
                    branch, commit_hash, parent_version_id, snapshot_by, change_type
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, 'update'
                )
                RETURNING id
                """,
                memory_id,
                next_version,
                source_head["content"],
                source_head["category"],
                source_head["subcategory"],
                meta_str,
                source_head["verbatim_content"],
                target_head["owner_id"],
                target_head["namespace"],
                target_head["permission_mode"],
                source_head["source_model"],
                source_head["source_provider"],
                source_head["source_session"],
                source_head["source_agent"],
                target_branch,
                merge_hash,
                target_head["id"],
                user.user_id,
            )
            await conn.execute(
                "UPDATE memory_branches SET head_version_id = $1 WHERE memory_id = $2 AND name = $3",
                new_version_id,
                memory_id,
                target_branch,
            )

            if live_tracks_target:
                if (
                    live["content"] != target_head["content"]
                    or live["category"] != target_head["category"]
                    or live["subcategory"] != target_head["subcategory"]
                    or live["metadata"] != target_head["metadata"]
                    or live["verbatim_content"] != target_head["verbatim_content"]
                    or live["owner_id"] != target_head["owner_id"]
                    or live["namespace"] != target_head["namespace"]
                    or live["permission_mode"] != target_head["permission_mode"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Live memory row has drifted from main HEAD; manual reconciliation required before merge into main",
                    )
                await conn.execute("SELECT set_config('mnemos.suppress_version_snapshot', '1', true)")
                await conn.execute(
                    """
                    UPDATE memories SET
                        content = $1, category = $2, subcategory = $3,
                        metadata = $4::jsonb, verbatim_content = $5,
                        source_model = $6, source_provider = $7,
                        source_session = $8, source_agent = $9, updated = NOW()
                    WHERE id = $10
                    """,
                    source_head["content"],
                    source_head["category"],
                    source_head["subcategory"],
                    meta_str,
                    source_head["verbatim_content"],
                    source_head["source_model"],
                    source_head["source_provider"],
                    source_head["source_session"],
                    source_head["source_agent"],
                    memory_id,
                )
                delivery_ids = await backend.webhooks.dispatch_event(
                    tx,
                    "memory.updated",
                    {
                        "memory_id": memory_id,
                        "category": source_head["category"],
                        "subcategory": source_head["subcategory"],
                        "content": source_head["content"],
                        "owner_id": merge_owner_id,
                        "namespace": merge_namespace,
                        "merge_source": request.source_branch,
                        "merge_target": target_branch,
                    },
                    owner_id=merge_owner_id,
                    namespace=merge_namespace,
                )

        if live_tracks_target:
            if _lc._cache:
                try:
                    await _lc._cache.delete("stats:global")
                    try:
                        async for key in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                            await _lc._cache.delete(key)
                    except Exception:
                        pass
                except Exception:
                    pass
            _schedule_outbox_deliveries(delivery_ids)

        logger.info(
            f"[DAG] Merged {request.source_branch} -> {target_branch} for {memory_id} "
            f"(merge_hash={merge_hash[:12] if merge_hash else '?'}...)"
        )
        return MergeResult(
            success=True,
            new_commit_hash=merge_hash,
            message=f"Merged {request.source_branch} into {target_branch}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Merge failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")

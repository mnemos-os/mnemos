"""DAG (Directed Acyclic Graph) endpoints for memory versioning.

Implements git-like operations on memory history:
- log: Walk commit DAG from HEAD to root
- branches: List all branches for a memory
- branch: Create new branch from HEAD or specific commit
- checkout: Fetch commit content by hash
- merge: Merge source_branch into target_branch
"""

import logging
import time as _time
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user


async def _assert_memory_access(conn, memory_id: str, user: UserContext) -> None:
    """Ensure the caller can read/modify this memory. Raises 404 otherwise.

    Root can access any memory; non-root callers are scoped to both their
    owner_id AND their namespace — matching the two-dimensional tenancy
    gate that list/get/search/KG handlers apply (v3.1.2 Tier 3). We
    return 404 (not 403) to avoid leaking existence of memories belonging
    to other tenants.
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


def _require_pool():
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
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
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
            # Recursive CTE: walk from HEAD backward through parent_version_id.
            # Carries owner_id/namespace/permission_mode through both arms so
            # the post-walk filter can drop snapshots the caller can't read.
            rows = await conn.fetch(
                """
                WITH RECURSIVE commit_walk AS (
                    -- Base: START from branch HEAD
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        1 AS depth
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON (
                        mb.memory_id = mv.memory_id AND
                        mb.name = $2 AND
                        mb.head_version_id = mv.id
                    )
                    WHERE mv.memory_id = $1
                    UNION ALL
                    -- Recursive: WALK backward via parent_version_id
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        cw.depth + 1
                    FROM memory_versions mv
                    INNER JOIN commit_walk cw ON mv.id = cw.parent_version_id
                    WHERE cw.depth < $3
                )
                SELECT
                    commit_hash, version_num, branch, content, category, subcategory,
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
            # Mirrors api/handlers/versions.py + api/mcp_tools.py.
            if user.role != "root":
                def _snap_visible(r) -> bool:
                    if r["namespace"] != user.namespace:
                        return False
                    return (
                        r["owner_id"] == user.user_id
                        or (r["permission_mode"] % 10) >= 4
                    )
                rows = [r for r in rows if _snap_visible(r)]

            # Assemble with parent hashes
            commits = []
            for i, row in enumerate(rows):
                parent_hash = rows[i + 1]["commit_hash"] if i + 1 < len(rows) else None
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
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
            branches = await conn.fetch(
                """
                SELECT
                    mb.name, mv.commit_hash, mb.created_at, mb.created_by
                FROM memory_branches mb
                LEFT JOIN memory_versions mv ON mv.id = mb.head_version_id
                WHERE mb.memory_id = $1
                ORDER BY mb.created_at DESC
                """,
                memory_id,
            )

            return [
                BranchInfo(
                    name=b["name"],
                    head_commit_hash=b["commit_hash"],
                    created_at=b["created_at"].isoformat(),
                    created_by=b["created_by"],
                )
                for b in branches
            ]

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
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
            # Resolve starting point (HEAD or specific commit)
            if request.from_commit:
                start_version = await conn.fetchrow(
                    "SELECT id, commit_hash, created_at FROM memory_versions WHERE memory_id = $1 AND commit_hash = $2",
                    memory_id,
                    request.from_commit,
                )
                if not start_version:
                    raise HTTPException(status_code=404, detail="Commit hash not found")
            else:
                # Default: use current main branch HEAD
                start_version = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.commit_hash, mv.created_at
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = 'main'
                    """,
                    memory_id,
                )
                if not start_version:
                    raise HTTPException(status_code=404, detail="main branch HEAD not found")

            # Refuse to silently overwrite an existing branch — previous
            # behaviour (ON CONFLICT DO UPDATE) let any caller hijack a named
            # branch. Callers that want to move a branch head should merge
            # instead.
            existing = await conn.fetchrow(
                "SELECT id FROM memory_branches WHERE memory_id = $1 AND name = $2",
                memory_id, request.name,
            )
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Branch '{request.name}' already exists",
                )
            await conn.fetchval(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                memory_id,
                request.name,
                start_version["id"],
                user.user_id,
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
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
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
                         WHERE mv2.id = mv.parent_version_id) AS parent_hash
                    FROM memory_versions mv
                    WHERE mv.memory_id = $1 AND mv.commit_hash = $2
                    """,
                    memory_id, commit_hash,
                )
            else:
                from api.visibility import version_visibility_predicate
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
    """Merge source_branch into target_branch.

    Strategy 'latest-wins' takes source_branch HEAD content.
    Strategy 'manual' requires manual conflict resolution (not implemented yet).
    """
    pool = _require_pool()

    if request.strategy not in ("latest-wins", "manual"):
        raise HTTPException(status_code=400, detail="Invalid merge strategy")

    # Pre-compute advisory lock key from (memory_id, target_branch) so concurrent
    # merges against the same branch serialize. Signed int64 range for postgres.
    import hashlib as _hashlib
    _lock_bytes = _hashlib.sha256(
        f"dag-merge:{memory_id}:{target_branch}".encode("utf-8")
    ).digest()[:8]
    _lock_key = int.from_bytes(_lock_bytes, "big", signed=False)
    if _lock_key >= 2**63:
        _lock_key -= 2**64

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
            async with conn.transaction():
                # Serialize concurrent merges on this (memory, branch).
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _lock_key)

                # Per-snapshot tenancy gate on the SOURCE head (slice
                # 2 round 15). Merge copies the source snapshot's
                # content into a new target-branch version — if the
                # caller can't read the source snapshot directly via
                # get_commit, they can't be allowed to copy its
                # content via merge. Target head needs no per-snapshot
                # gate because we're WRITING new content, not exposing
                # the existing target HEAD.
                if user.role == "root":
                    source_head = await conn.fetchrow(
                        """
                        SELECT mv.id, mv.commit_hash, mv.content, mv.version_num
                        FROM memory_versions mv
                        INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                        WHERE mv.memory_id = $1 AND mb.name = $2
                        """,
                        memory_id, request.source_branch,
                    )
                else:
                    from api.visibility import version_visibility_predicate
                    vis_clause, vis_params = version_visibility_predicate(
                        user.user_id, start_param_idx=3, table_alias="mv",
                    )
                    ns_ph = f"${len(vis_params) + 3}"
                    source_head = await conn.fetchrow(
                        f"""
                        SELECT mv.id, mv.commit_hash, mv.content, mv.version_num
                        FROM memory_versions mv
                        INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                        WHERE mv.memory_id = $1 AND mb.name = $2
                          AND {vis_clause} AND mv.namespace = {ns_ph}
                        """,
                        memory_id, request.source_branch,
                        *vis_params, user.namespace,
                    )
                target_head = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.version_num, mv.commit_hash
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = $2
                    """,
                    memory_id, target_branch,
                )
                if not source_head:
                    raise HTTPException(status_code=404, detail=f"Source branch '{request.source_branch}' not found")
                if not target_head:
                    raise HTTPException(status_code=404, detail=f"Target branch '{target_branch}' not found")

                if request.strategy == "latest-wins":
                    next_version = target_head["version_num"] + 1
                    # Full SHA-256 (64 hex chars) — previously truncated to 16 which
                    # reduced collision tolerance from ~2^128 to ~2^32.
                    merge_hash = _hashlib.sha256(
                        f"{source_head['commit_hash']}"
                        f"{target_head['commit_hash']}"
                        f"{int(_time.time() * 1_000_000)}".encode()
                    ).hexdigest()

                    # Pre-existing bug Codex flagged in round 16:
                    #   - target_head SELECT was missing commit_hash
                    #     (merge_hash now reads it; fixed above).
                    #   - INSERT omitted memory_versions NOT NULL
                    #     columns owner_id, namespace, permission_mode
                    #     — every merge would have FK/constraint-failed
                    #     since v2 versioning landed.
                    #   - change_type='merge' violated the CHECK
                    #     constraint (allowed values: 'create' /
                    #     'update' / 'delete'). Use 'update' — merge
                    #     IS an update to the target branch, just
                    #     with content copied from a source. Adding
                    #     'merge' to the CHECK is a separate schema
                    #     migration if/when callers need to filter
                    #     merges from regular updates.
                    #   - Tenancy on the merge commit: copy
                    #     owner_id/namespace/permission_mode from the
                    #     source snapshot. Slice 2 round-15 already
                    #     ensured the caller can read the source
                    #     snapshot (per-snapshot gate); copying the
                    #     source's tenancy is consistent with that.
                    new_commit_id = await conn.fetchval(
                        """
                        INSERT INTO memory_versions (
                            memory_id, version_num, content, category, subcategory,
                            owner_id, namespace, permission_mode,
                            branch, commit_hash, parent_version_id, snapshot_by, change_type
                        )
                        SELECT
                            $1, $2, $3, category, subcategory,
                            owner_id, namespace, permission_mode,
                            $4, $5, $6, $7, 'update'
                        FROM memory_versions WHERE id = $8
                        RETURNING id
                        """,
                        memory_id, next_version, source_head["content"],
                        target_branch, merge_hash, target_head["id"],
                        user.user_id, source_head["id"],
                    )

                    await conn.execute(
                        "UPDATE memory_branches SET head_version_id = $1 "
                        "WHERE memory_id = $2 AND name = $3",
                        new_commit_id, memory_id, target_branch,
                    )

                    logger.info(
                        f"[DAG] Merged {request.source_branch} -> {target_branch} "
                        f"for {memory_id} (merge_hash={merge_hash[:12]}...)"
                    )
                    return MergeResult(
                        success=True,
                        new_commit_hash=merge_hash,
                        message=f"Merged {request.source_branch} into {target_branch}",
                    )
                else:  # manual
                    return MergeResult(
                        success=False,
                        message="Manual merge strategy not yet implemented",
                    )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Merge failed: {e}")
        raise HTTPException(status_code=500, detail="internal server error")

"""DAG (Directed Acyclic Graph) endpoints for memory versioning.

Implements git-like operations on memory history:
- log: Walk commit DAG from HEAD to root
- branches: List all branches for a memory
- branch: Create new branch from HEAD or specific commit
- checkout: Fetch commit content by hash
- merge: Merge source_branch into target_branch
"""

import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user


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
                    -- Recursive: WALK backward via parent_version_id.
                    -- Same-memory predicate (mv.memory_id =
                    -- cw.memory_id) prevents corrupt parent_version_id
                    -- from pulling another memory's version into this
                    -- memory's log (round-38 finding). Cross-memory
                    -- parent edges silently drop out of the walk; the
                    -- HTTP log surfaces only intra-memory ancestry.
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        cw.depth + 1
                    FROM memory_versions mv
                    INNER JOIN commit_walk cw
                        ON mv.id = cw.parent_version_id
                       AND mv.memory_id = cw.memory_id
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
            # Scope the JOIN by memory_id too — a stale/corrupt
            # branch row pointing at another memory's version_id
            # would otherwise leak that other memory's commit_hash
            # through `head_commit_hash` (round-37 finding). With
            # the scoped JOIN, mismatched rows return NULL for
            # commit_hash; the `head_commit_hash` field then
            # surfaces the corruption rather than papering over it.
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
                        f"this memory; omitting from response. Operator "
                        f"reconciliation required."
                    )
                    continue
                result.append(BranchInfo(
                    name=b["name"],
                    head_commit_hash=b["commit_hash"],
                    created_at=b["created_at"].isoformat(),
                    created_by=b["created_by"],
                ))
            return result

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
                    INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
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
                         WHERE mv2.id = mv.parent_version_id
                           AND mv2.memory_id = mv.memory_id) AS parent_hash
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
    """Merge source_branch into target_branch.

    Strategy 'latest-wins' takes source_branch HEAD content.
    Strategy 'manual' requires manual conflict resolution (not implemented yet).
    """
    pool = _require_pool()

    if request.strategy not in ("latest-wins", "manual"):
        raise HTTPException(status_code=400, detail="Invalid merge strategy")

    # Pre-compute advisory lock key from (memory_id, target_branch) so all
    # DAG writers (merge AND revert) serialize on the same branch.
    # Round-26: previous prefix "dag-merge" was merge-specific and didn't
    # collide with revert's locks; a feature-branch revert could orphan
    # a concurrent merge into the same branch (or vice versa). Generic
    # "dag-branch" prefix means both paths compute the same key for the
    # same (memory_id, branch) and pg_advisory_xact_lock serializes
    # them. The revert path in api/handlers/versions.py uses an
    # identical key.
    import hashlib as _hashlib
    _lock_key = _branch_advisory_lock_key(memory_id, target_branch, _hashlib)

    try:
        async with pool.acquire() as conn:
            await _assert_memory_access(conn, memory_id, user)
            async with conn.transaction():
                # Lock acquisition order: advisory FIRST, row lock
                # SECOND. Both DAG writers (merge_branch + feature-
                # branch revert) follow this order so they can't
                # deadlock against each other.
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _lock_key)

                if request.strategy == "manual":
                    return MergeResult(
                        success=False,
                        message="Manual merge strategy not yet implemented",
                    )

                # latest-wins from here. Acquire the row lock + read
                # both source_head and target_head UNDER the lock so
                # neither can race a concurrent writer (rounds 28,
                # 29, 30). The advisory lock above also serializes
                # against other DAG writers on the same branch.
                # Pull versioned fields from the live row including
                # tenancy fields (round 33). The drift guard before
                # the destructive UPDATE compares ALL of the
                # trigger's versioned fields — content / category /
                # subcategory / metadata / verbatim_content PLUS
                # owner_id / namespace / permission_mode. Otherwise
                # a permission-mode drift between live (private) and
                # target_head (public) would let us publish a public
                # version-log entry whose content is currently held
                # private in `memories`.
                if user.role == "root":
                    live = await conn.fetchrow(
                        "SELECT id, owner_id, namespace, permission_mode, "
                        "content, category, subcategory, metadata, "
                        "verbatim_content "
                        "FROM memories WHERE id = $1 FOR UPDATE",
                        memory_id,
                    )
                else:
                    live = await conn.fetchrow(
                        "SELECT id, owner_id, namespace, permission_mode, "
                        "content, category, subcategory, metadata, "
                        "verbatim_content "
                        "FROM memories WHERE id = $1 "
                        "AND owner_id = $2 AND namespace = $3 FOR UPDATE",
                        memory_id, user.user_id, user.namespace,
                    )
                if live is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Memory {memory_id} not found",
                    )

                # Per-snapshot tenancy on source_head — caller must
                # be allowed to read the source snapshot directly
                # via get_commit before merge can copy its content.
                # Pull source_* provenance fields (round 18). Now
                # under the row lock so source_head can't drift
                # mid-transaction (round 30).
                if user.role == "root":
                    source_head = await conn.fetchrow(
                        """
                        SELECT mv.id, mv.commit_hash, mv.content, mv.version_num,
                               mv.category, mv.subcategory, mv.metadata,
                               mv.verbatim_content,
                               mv.source_model, mv.source_provider,
                               mv.source_session, mv.source_agent
                        FROM memory_versions mv
                        INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
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
                        SELECT mv.id, mv.commit_hash, mv.content, mv.version_num,
                               mv.category, mv.subcategory, mv.metadata,
                               mv.verbatim_content,
                               mv.source_model, mv.source_provider,
                               mv.source_session, mv.source_agent
                        FROM memory_versions mv
                        INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                        WHERE mv.memory_id = $1 AND mb.name = $2
                          AND {vis_clause} AND mv.namespace = {ns_ph}
                        """,
                        memory_id, request.source_branch,
                        *vis_params, user.namespace,
                    )
                if not source_head:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Source branch '{request.source_branch}' not found",
                    )

                # target_head also under the lock. Reading it AFTER
                # FOR UPDATE on memories serializes against
                # update_memory and main-revert's row-level locks
                # (round 28). source vs target both freshly read
                # at the same serialization point.
                target_head = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.version_num, mv.commit_hash,
                           mv.content, mv.category, mv.subcategory,
                           mv.metadata, mv.verbatim_content,
                           mv.owner_id, mv.namespace, mv.permission_mode
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = $2
                    """,
                    memory_id, target_branch,
                )
                if not target_head:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Target branch '{target_branch}' not found",
                    )

                # latest-wins continues here — manual already
                # returned at the top after lock acquisition.

                # Authoritative no-op check (round-29 fix). Run
                # this AFTER acquiring the row lock and re-reading
                # target_head so the decision reflects the current
                # state of main, not a pre-lock snapshot. Compare
                # ALL of the trigger's versioned fields (round-23
                # extended this from content-only to the full
                # set: content / category / subcategory /
                # metadata / verbatim_content).
                if (source_head["content"] == target_head["content"]
                        and source_head["category"] == target_head["category"]
                        and source_head["subcategory"] == target_head["subcategory"]
                        and source_head["metadata"] == target_head["metadata"]
                        and source_head["verbatim_content"] == target_head["verbatim_content"]):
                    logger.info(
                        f"[DAG] Merge no-op (locked): "
                        f"{request.source_branch} -> {target_branch} for "
                        f"{memory_id} (versioned fields identical at "
                        f"locked target_head)"
                    )
                    return MergeResult(
                        success=False,
                        new_commit_hash=target_head["commit_hash"],
                        message=(
                            f"Source branch '{request.source_branch}' has "
                            f"no versioned changes vs '{target_branch}'; "
                            f"no merge commit created"
                        ),
                    )

                merge_owner_id = live["owner_id"]
                merge_namespace = live["namespace"]
                # Materialized-branch identity is determined by
                # NAME, not by content equality. The MNEMOS
                # convention is `memories` always tracks 'main':
                # v1 of every memory is created on 'main' by
                # the trigger; update_memory writes via the
                # trigger with mnemos.current_branch GUC default
                # 'main'. Feature branches diverge in
                # memory_versions / memory_branches but never in
                # `memories`. So the live row is updated only
                # when target_branch is 'main'. Earlier rounds
                # tried to infer this from content equality
                # (round 22) and full versioned-field equality
                # (round 23); both are false-positive on
                # newly-branched-from-main memories where the
                # branch initially points at the same versioned
                # state as main.
                live_tracks_target = (target_branch == "main")

                # Compute merge commit_hash. Same shape as the
                # mnemos_version_snapshot trigger
                # (sha256 over id|version|content|now) so the
                # commit identity is consistent regardless of
                # whether the trigger or this handler created it.
                import hashlib as _hashlib_local
                next_version = target_head["version_num"] + 1
                merge_hash = _hashlib_local.sha256(
                    f"{memory_id}|{next_version}|{source_head['content']}|"
                    f"merge-{request.source_branch}->{target_branch}-{int(__import__('time').time() * 1_000_000)}"
                    .encode()
                ).hexdigest()

                meta_val = source_head["metadata"]
                if isinstance(meta_val, str):
                    meta_str = meta_val
                elif meta_val is not None:
                    import json as _json
                    meta_str = _json.dumps(dict(meta_val))
                else:
                    meta_str = "{}"

                # Explicit INSERT into memory_versions for the
                # merge commit. Pulls owner_id/namespace/
                # permission_mode from the source snapshot
                # (per slice-2 round-15 source-head visibility
                # gate), copies content + provenance from SOURCE,
                # but tenancy fields (owner_id / namespace /
                # permission_mode) from TARGET. Round-33 fix: the
                # earlier code copied tenancy from the source
                # snapshot, which let merging from a public branch
                # (forked while public) into a now-private main
                # publish content under the source's stale public
                # permission_mode. Tenancy must follow the target
                # branch so the new commit's visibility matches
                # what the destination actually owns.
                # change_type='update' (the v2 CHECK constraint
                # doesn't allow 'merge'; see migration_v2_versioning).
                new_version_id = await conn.fetchval(
                    """
                    INSERT INTO memory_versions (
                        memory_id, version_num, content, category, subcategory,
                        metadata, verbatim_content,
                        owner_id, namespace, permission_mode,
                        source_model, source_provider, source_session, source_agent,
                        branch, commit_hash, parent_version_id,
                        snapshot_by, change_type
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6::jsonb, $7,
                        $8, $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17, $18, 'update'
                    )
                    RETURNING id
                    """,
                    memory_id, next_version,
                    source_head["content"], source_head["category"],
                    source_head["subcategory"],
                    meta_str, source_head["verbatim_content"],
                    target_head["owner_id"], target_head["namespace"],
                    target_head["permission_mode"],
                    source_head["source_model"],
                    source_head["source_provider"],
                    source_head["source_session"],
                    source_head["source_agent"],
                    target_branch, merge_hash, target_head["id"],
                    user.user_id,
                )

                # Advance the target branch HEAD pointer.
                await conn.execute(
                    "UPDATE memory_branches SET head_version_id = $1 "
                    "WHERE memory_id = $2 AND name = $3",
                    new_version_id, memory_id, target_branch,
                )

                # If the live row was tracking target_branch
                # (its content matched target_head's), advance
                # the live row too so the live-row/HEAD invariant
                # holds for the now-materialized branch.
                # Suppress the version-snapshot trigger here so
                # we don't double-version (we already did the
                # explicit INSERT above). Use the existing
                # mnemos.suppress_version_snapshot GUC which the
                # trigger consults via its WHEN clause (per
                # migrations_charon_trigger_guard).
                if live_tracks_target:
                    # Drift guard (rounds 32 + 33). Compare ALL of
                    # the trigger's versioned fields between live
                    # and target_head — content + category +
                    # subcategory + metadata + verbatim_content
                    # PLUS owner_id, namespace, permission_mode.
                    # Tenancy drift would be especially dangerous:
                    # if main was made private (mode 600) but the
                    # version log still shows public (mode 644),
                    # merging would publish a public commit whose
                    # content the live row holds privately.
                    if (live["content"] != target_head["content"]
                            or live["category"] != target_head["category"]
                            or live["subcategory"] != target_head["subcategory"]
                            or live["metadata"] != target_head["metadata"]
                            or live["verbatim_content"] != target_head["verbatim_content"]
                            or live["owner_id"] != target_head["owner_id"]
                            or live["namespace"] != target_head["namespace"]
                            or live["permission_mode"] != target_head["permission_mode"]):
                        logger.error(
                            f"[DAG] Merge aborted: live memory row for "
                            f"{memory_id} has drifted from main HEAD "
                            f"({target_head['commit_hash'][:12]}). Refusing "
                            f"to overwrite live state silently. Operator "
                            f"must reconcile via revert or update before "
                            f"this merge can run."
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Live memory row has drifted from main "
                                f"HEAD; manual reconciliation required "
                                f"before merge into main"
                            ),
                        )
                    await conn.execute(
                        "SELECT set_config('mnemos.suppress_version_snapshot', '1', true)"
                    )
                    await conn.execute(
                        """
                        UPDATE memories SET
                            content = $1, category = $2, subcategory = $3,
                            metadata = $4::jsonb, verbatim_content = $5,
                            source_model = $6, source_provider = $7,
                            source_session = $8, source_agent = $9,
                            updated = NOW()
                        WHERE id = $10
                        """,
                        source_head["content"], source_head["category"],
                        source_head["subcategory"],
                        meta_str, source_head["verbatim_content"],
                        source_head["source_model"],
                        source_head["source_provider"],
                        source_head["source_session"],
                        source_head["source_agent"],
                        memory_id,
                    )

        # Cache invalidation + memory.updated webhook ONLY when the
        # live row was actually mutated. Branch-only merges (where
        # live_tracks_target was False) advanced memory_branches
        # HEAD but left `memories` untouched — the live state did
        # NOT change, so /memories/search results don't need
        # invalidation and external subscribers must not be told
        # the live row was updated. Cross-branch merges are pure
        # DAG-level operations that callers monitor via the version/
        # log endpoints, not memory.updated webhooks (round-23
        # finding).
        if live_tracks_target:
            if _lc._cache:
                try:
                    await _lc._cache.delete("stats:global")
                    try:
                        async for _k in _lc._cache.scan_iter(
                            match="mnemos:search:*", count=500,
                        ):
                            await _lc._cache.delete(_k)
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                from api.webhook_dispatcher import dispatch as _dispatch_webhook
                async with _lc._pool.acquire() as _wh_conn:
                    await _dispatch_webhook(_wh_conn, "memory.updated", {
                        "memory_id": memory_id,
                        "category": source_head["category"],
                        "subcategory": source_head["subcategory"],
                        "content": source_head["content"],
                        "owner_id": merge_owner_id,
                        "namespace": merge_namespace,
                        "merge_source": request.source_branch,
                        "merge_target": target_branch,
                    }, owner_id=merge_owner_id, namespace=merge_namespace)
            except Exception:
                logger.warning(
                    "webhook dispatch failed for memory.updated %s",
                    memory_id, exc_info=True,
                )

        logger.info(
            f"[DAG] Merged {request.source_branch} -> {target_branch} "
            f"for {memory_id} (merge_hash={merge_hash[:12] if merge_hash else '?'}...)"
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

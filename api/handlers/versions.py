"""Memory version history, diff, and revert endpoints."""
import difflib
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.models import MemoryItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["versions"])


# ── Models ────────────────────────────────────────────────────────────────────

from pydantic import BaseModel  # noqa: E402


class MemoryVersion(BaseModel):
    id: str
    memory_id: str
    version_num: int
    content: str
    category: str
    subcategory: Optional[str] = None
    metadata: Optional[dict] = None
    verbatim_content: Optional[str] = None
    owner_id: str
    namespace: str
    permission_mode: int
    source_model: Optional[str] = None
    source_provider: Optional[str] = None
    source_session: Optional[str] = None
    source_agent: Optional[str] = None
    snapshot_at: str
    snapshot_by: Optional[str] = None
    change_type: str   # create | update | delete


class VersionSummary(BaseModel):
    version_num: int
    snapshot_at: str
    snapshot_by: Optional[str] = None
    change_type: str
    content_preview: str   # first 120 chars
    branch: Optional[str] = None  # branch name (Phase 3 DAG)


class DiffResponse(BaseModel):
    memory_id: str
    from_version: int
    to_version: int
    diff: str   # unified diff text; empty string if identical


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_version(row) -> MemoryVersion:
    raw_meta = row.get("metadata")
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = None
    elif not isinstance(raw_meta, dict):
        raw_meta = None
    return MemoryVersion(
        id=str(row["id"]),
        memory_id=row["memory_id"],
        version_num=row["version_num"],
        content=row["content"],
        category=row["category"],
        subcategory=row.get("subcategory"),
        metadata=raw_meta,
        verbatim_content=row.get("verbatim_content"),
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        permission_mode=row["permission_mode"],
        source_model=row.get("source_model"),
        source_provider=row.get("source_provider"),
        source_session=row.get("source_session"),
        source_agent=row.get("source_agent"),
        snapshot_at=row["snapshot_at"].isoformat(),
        snapshot_by=row.get("snapshot_by"),
        change_type=row["change_type"],
    )


async def _assert_memory_exists(conn, memory_id: str) -> None:
    """Raise 404 if memory_id has no version history (i.e. never existed)."""
    row = await conn.fetchrow(
        "SELECT 1 FROM memory_versions WHERE memory_id = $1 LIMIT 1", memory_id
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


async def _assert_memory_readable(conn, memory_id: str, user: UserContext) -> None:
    """Tenancy gate for version-history reads.

    Version snapshots in memory_versions inherit the live memory's
    tenancy — if a non-root caller can't read the live memory via
    list/get/search/rehydrate, they must not see its history,
    diffs, or per-version content here either. Older code only
    checked existence in memory_versions, which let any authenticated
    caller read every other tenant's full history by guessing
    memory_id.

    Root bypasses; non-root must pass the same shared
    read_visibility_predicate that gates list/get/search/rehydrate
    PLUS the namespace pin.
    """
    if _is_root(user):
        # Root still needs the existence check so we 404 cleanly.
        await _assert_memory_exists(conn, memory_id)
        return

    from api.visibility import read_visibility_predicate
    vis_clause, vis_params = read_visibility_predicate(
        user.user_id, list(user.group_ids), start_param_idx=2,
    )
    # $1 = memory_id; $2..$N = visibility params; $N+1 = namespace
    ns_ph = f"${len(vis_params) + 2}"
    row = await conn.fetchrow(
        f"SELECT 1 FROM memories WHERE id = $1 "
        f"AND {vis_clause} AND namespace = {ns_ph} LIMIT 1",
        memory_id, *vis_params, user.namespace,
    )
    if not row:
        # 404 (not 403) keeps cross-tenant existence invisible.
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/memories/{memory_id}/versions", response_model=List[VersionSummary])
async def list_versions(
    memory_id: str,
    branch: str = Query("main", description="Branch name (default: main)"),
    user: UserContext = Depends(get_current_user),
):
    """List version history for a memory on a specific branch (oldest first).

    Query parameter branch defaults to 'main'. For feature branches, specify branch=name.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        await _assert_memory_readable(conn, memory_id, user)
        # Per-snapshot tenancy: filter the version rows by THEIR own
        # owner/namespace/permission_mode, not the live memory's.
        # A memory that was private at v1 and made public at v2 must
        # NOT expose v1 to readers who only became authorized after
        # the permission flip.
        if _is_root(user):
            rows = await conn.fetch(
                "SELECT version_num, snapshot_at, snapshot_by, change_type, content, branch "
                "FROM memory_versions WHERE memory_id = $1 AND branch = $2 ORDER BY version_num ASC",
                memory_id,
                branch,
            )
        else:
            from api.visibility import version_visibility_predicate
            vis_clause, vis_params = version_visibility_predicate(
                user.user_id, start_param_idx=3,
            )
            ns_ph = f"${len(vis_params) + 3}"
            rows = await conn.fetch(
                f"SELECT version_num, snapshot_at, snapshot_by, change_type, content, branch "
                f"FROM memory_versions WHERE memory_id = $1 AND branch = $2 "
                f"AND {vis_clause} AND namespace = {ns_ph} "
                f"ORDER BY version_num ASC",
                memory_id, branch, *vis_params, user.namespace,
            )
    return [
        VersionSummary(
            version_num=r["version_num"],
            snapshot_at=r["snapshot_at"].isoformat(),
            snapshot_by=r.get("snapshot_by"),
            change_type=r["change_type"],
            content_preview=r["content"][:120],
            branch=r.get("branch"),
        )
        for r in rows
    ]


@router.get("/memories/{memory_id}/versions/{version_num}", response_model=MemoryVersion)
async def get_version(
    memory_id: str,
    version_num: int,
    branch: str = Query("main", description="Branch name (default: main)"),
    user: UserContext = Depends(get_current_user),
):
    """Retrieve memory content at a specific version on a branch."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        await _assert_memory_readable(conn, memory_id, user)
        if _is_root(user):
            row = await conn.fetchrow(
                "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
                "verbatim_content, owner_id, namespace, permission_mode, "
                "source_model, source_provider, source_session, source_agent, "
                "snapshot_at, snapshot_by, change_type "
                "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3",
                memory_id, version_num, branch,
            )
        else:
            # Per-snapshot tenancy on the row itself.
            from api.visibility import version_visibility_predicate
            vis_clause, vis_params = version_visibility_predicate(
                user.user_id, start_param_idx=4,
            )
            ns_ph = f"${len(vis_params) + 4}"
            row = await conn.fetchrow(
                "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
                "verbatim_content, owner_id, namespace, permission_mode, "
                "source_model, source_provider, source_session, source_agent, "
                "snapshot_at, snapshot_by, change_type "
                f"FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3 "
                f"AND {vis_clause} AND namespace = {ns_ph}",
                memory_id, version_num, branch, *vis_params, user.namespace,
            )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_num} not found for memory {memory_id} on branch '{branch}'",
        )
    return _row_to_version(row)


@router.get("/memories/{memory_id}/diff", response_model=DiffResponse)
async def diff_versions(
    memory_id: str,
    from_version: int = Query(..., alias="from"),
    to_version: int = Query(..., alias="to"),
    branch: str = Query("main", description="Branch name (default: main)"),
    user: UserContext = Depends(get_current_user),
):
    """Return a unified diff between two versions on a branch.

    Both versions must exist on the specified branch.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        await _assert_memory_readable(conn, memory_id, user)
        if _is_root(user):
            rows = await conn.fetch(
                "SELECT version_num, content FROM memory_versions "
                "WHERE memory_id = $1 AND version_num = ANY($2::int[]) AND branch = $3",
                memory_id, [from_version, to_version], branch,
            )
        else:
            from api.visibility import version_visibility_predicate
            vis_clause, vis_params = version_visibility_predicate(
                user.user_id, start_param_idx=4,
            )
            ns_ph = f"${len(vis_params) + 4}"
            rows = await conn.fetch(
                f"SELECT version_num, content FROM memory_versions "
                f"WHERE memory_id = $1 AND version_num = ANY($2::int[]) AND branch = $3 "
                f"AND {vis_clause} AND namespace = {ns_ph}",
                memory_id, [from_version, to_version], branch,
                *vis_params, user.namespace,
            )
    versions = {r["version_num"]: r["content"] for r in rows}
    if from_version not in versions:
        raise HTTPException(status_code=404, detail=f"Version {from_version} not found on branch '{branch}'")
    if to_version not in versions:
        raise HTTPException(status_code=404, detail=f"Version {to_version} not found on branch '{branch}'")

    # Ensure trailing newline so unified_diff doesn't concatenate last lines
    a = (versions[from_version] + "\n").splitlines(keepends=True)
    b = (versions[to_version] + "\n").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        a, b,
        fromfile=f"{branch}/v{from_version}",
        tofile=f"{branch}/v{to_version}",
    ))
    return DiffResponse(
        memory_id=memory_id,
        from_version=from_version,
        to_version=to_version,
        diff="".join(diff_lines),
    )


@router.post("/memories/{memory_id}/revert/{version_num}", response_model=MemoryItem)
async def revert_memory(
    memory_id: str,
    version_num: int,
    branch: str = Query("main", description="Branch name (default: main)"),
    user: UserContext = Depends(get_current_user),
):
    """Restore a memory to the content of a previous version on a branch.

    Creates a new version snapshot on the same branch so the revert itself
    is part of the audit trail. Updates the live memory record.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        # Tenancy gate first — fail-closed before any version SELECT.
        await _assert_memory_readable(conn, memory_id, user)

        # Per-snapshot tenancy on the source version too: prevents
        # reverting TO a private historical snapshot that the caller
        # wouldn't be allowed to read directly via get_version.
        if _is_root(user):
            ver_row = await conn.fetchrow(
                "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
                "verbatim_content, owner_id, namespace, permission_mode, "
                "source_model, source_provider, source_session, source_agent, "
                "snapshot_at, snapshot_by, change_type "
                "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3",
                memory_id, version_num, branch,
            )
        else:
            from api.visibility import version_visibility_predicate
            vis_clause, vis_params = version_visibility_predicate(
                user.user_id, start_param_idx=4,
            )
            ns_ph = f"${len(vis_params) + 4}"
            ver_row = await conn.fetchrow(
                "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
                "verbatim_content, owner_id, namespace, permission_mode, "
                "source_model, source_provider, source_session, source_agent, "
                "snapshot_at, snapshot_by, change_type "
                f"FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3 "
                f"AND {vis_clause} AND namespace = {ns_ph}",
                memory_id, version_num, branch, *vis_params, user.namespace,
            )
        if not ver_row:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_num} not found for memory {memory_id} on branch '{branch}'",
            )

        meta_val = ver_row["metadata"]
        if isinstance(meta_val, str):
            meta_str = meta_val
        elif meta_val is not None:
            meta_str = json.dumps(dict(meta_val))
        else:
            meta_str = "{}"

        async with conn.transaction():
            # Authorize against the live row up front — atomic with
            # the write below.
            if _is_root(user):
                live = await conn.fetchrow(
                    f"SELECT {_lc._MEMORY_COLS} FROM memories "
                    "WHERE id=$1 FOR UPDATE",
                    memory_id,
                )
            else:
                live = await conn.fetchrow(
                    f"SELECT {_lc._MEMORY_COLS} FROM memories "
                    "WHERE id=$1 AND owner_id=$2 AND namespace=$3 FOR UPDATE",
                    memory_id, user.user_id, user.namespace,
                )
            if live is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Memory {memory_id} not found",
                )

            if branch == "main":
                # Main-branch revert: UPDATE memories under the main
                # GUC and let mnemos_version_snapshot create the
                # revert version row + advance memory_branches HEAD
                # for main. Preserves the live-row/main-HEAD invariant.
                await conn.execute(
                    "SELECT set_config('mnemos.current_branch', 'main', true)"
                )
                row = await conn.fetchrow(
                    "UPDATE memories SET "
                    "content=$1, category=$2, subcategory=$3, metadata=$4::jsonb, "
                    "verbatim_content=$5, updated=NOW() "
                    f"WHERE id=$6 RETURNING {_lc._MEMORY_COLS}",
                    ver_row["content"],
                    ver_row["category"],
                    ver_row["subcategory"],
                    meta_str,
                    ver_row["verbatim_content"],
                    memory_id,
                )
            else:
                # Feature-branch revert: PURE DAG operation (per
                # round-25 fix). MNEMOS convention is `memories`
                # always tracks main; feature branches diverge only
                # in memory_versions + memory_branches. Reverting on
                # a feature branch must NOT mutate the live row, or
                # we re-introduce the branch-skew bug Codex flagged
                # in dag.py merge. Instead: explicit INSERT of the
                # revert version row + advance memory_branches HEAD
                # for the feature branch.
                import hashlib as _hashlib_local
                import time as _time_local
                next_version_num = await conn.fetchval(
                    "SELECT COALESCE(MAX(version_num), 0) + 1 "
                    "FROM memory_versions WHERE memory_id = $1 AND branch = $2",
                    memory_id, branch,
                )
                # Get current HEAD for parent linkage
                target_head_id = await conn.fetchval(
                    "SELECT head_version_id FROM memory_branches "
                    "WHERE memory_id = $1 AND name = $2",
                    memory_id, branch,
                )
                if target_head_id is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Branch '{branch}' not found",
                    )
                revert_hash = _hashlib_local.sha256(
                    f"{memory_id}|{next_version_num}|{ver_row['content']}|"
                    f"revert-to-v{version_num}-{int(_time_local.time() * 1_000_000)}"
                    .encode()
                ).hexdigest()
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
                    memory_id, next_version_num,
                    ver_row["content"], ver_row["category"], ver_row["subcategory"],
                    meta_str, ver_row["verbatim_content"],
                    ver_row["owner_id"], ver_row["namespace"], ver_row["permission_mode"],
                    ver_row["source_model"], ver_row["source_provider"],
                    ver_row["source_session"], ver_row["source_agent"],
                    branch, revert_hash, target_head_id, user.user_id,
                )
                await conn.execute(
                    "UPDATE memory_branches SET head_version_id = $1 "
                    "WHERE memory_id = $2 AND name = $3",
                    new_version_id, memory_id, branch,
                )
                row = live  # live row unchanged; return the existing live state

    logger.info(
        f"[VERSION] Reverted {memory_id} to v{version_num} on branch '{branch}' "
        f"by {user.user_id or 'default'}"
    )
    return _lc._row_to_memory(row)

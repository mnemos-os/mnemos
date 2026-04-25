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
        await _assert_memory_exists(conn, memory_id)
        rows = await conn.fetch(
            "SELECT version_num, snapshot_at, snapshot_by, change_type, content, branch "
            "FROM memory_versions WHERE memory_id = $1 AND branch = $2 ORDER BY version_num ASC",
            memory_id,
            branch,
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
        row = await conn.fetchrow(
            "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
            "verbatim_content, owner_id, namespace, permission_mode, "
            "source_model, source_provider, source_session, source_agent, "
            "snapshot_at, snapshot_by, change_type "
            "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3",
            memory_id, version_num, branch,
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
        rows = await conn.fetch(
            "SELECT version_num, content FROM memory_versions "
            "WHERE memory_id = $1 AND version_num = ANY($2::int[]) AND branch = $3",
            memory_id, [from_version, to_version], branch,
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
        ver_row = await conn.fetchrow(
            "SELECT id, memory_id, version_num, content, category, subcategory, metadata, "
            "verbatim_content, owner_id, namespace, permission_mode, "
            "source_model, source_provider, source_session, source_agent, "
            "snapshot_at, snapshot_by, change_type "
            "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3",
            memory_id, version_num, branch,
        )
        if not ver_row:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_num} not found for memory {memory_id} on branch '{branch}'",
            )
        # Confirm the live memory still exists
        live = await conn.fetchrow(
            "SELECT id FROM memories WHERE id = $1", memory_id
        )
        if not live:
            raise HTTPException(
                status_code=409,
                detail=f"Memory {memory_id} has been deleted; cannot revert",
            )

        meta_val = ver_row["metadata"]
        if isinstance(meta_val, str):
            meta_str = meta_val
        elif meta_val is not None:
            meta_str = json.dumps(dict(meta_val))
        else:
            meta_str = "{}"

        async with conn.transaction():
            # Transaction-local GUC for the version-snapshot trigger.
            # Uses set_config(name, value, true) — the `true` flag scopes
            # the setting to the current transaction, equivalent to
            # SET LOCAL. Prior code used plain `SET ...` interpolated via
            # f-string, which (a) leaked the branch onto the pooled
            # connection for subsequent requests, and (b) bypassed
            # parameter binding. Caught in the GUC audit before v3.4 tag.
            await conn.execute(
                "SELECT set_config('mnemos.current_branch', $1, true)",
                branch,
            )

            await conn.execute(
                "UPDATE memories SET "
                "content=$1, category=$2, subcategory=$3, metadata=$4::jsonb, "
                "verbatim_content=$5, updated=NOW() "
                "WHERE id=$6",
                ver_row["content"],
                ver_row["category"],
                ver_row["subcategory"],
                meta_str,
                ver_row["verbatim_content"],
                memory_id,
            )
            row = await conn.fetchrow(
                f"SELECT {_lc._MEMORY_COLS} FROM memories WHERE id=$1", memory_id
            )

    logger.info(
        f"[VERSION] Reverted {memory_id} to v{version_num} on branch '{branch}' "
        f"by {user.user_id or 'default'}"
    )
    return _lc._row_to_memory(row)

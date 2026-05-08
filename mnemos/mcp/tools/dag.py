"""MCP DAG/versioning tool handlers."""

from __future__ import annotations

import difflib
import logging
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.auth_context import UserContext
from mnemos.db import mcp_repo

from ._runtime import (
    MCP_DEFAULT_LIMIT_MAX,
    _bounded_int,
    _mcp_assert_memory_readable,
    _mcp_user_required,
    _rest_get,
    _rest_post,
    _safe_path_segment,
    _tool,
)
from ._security import _mcp_enforce_write_rate_limit

logger = logging.getLogger(__name__)
BRANCH_MEMORY_RATE_LIMIT_PER_MINUTE = 30


async def tool_log_memory(
    memory_id: str,
    branch: str = "main",
    limit: int = 50,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Walk commit DAG from branch HEAD to root."""
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    _safe_path_segment(branch, label="branch")
    limit = _bounded_int(
        limit, label="limit", minimum=1, maximum=MCP_DEFAULT_LIMIT_MAX,
    )
    if user is None:
        commits = await _rest_get(
            f"/v1/memories/{safe_id}/log",
            params={"branch": branch, "limit": limit},
        )
        return {
            "success": True,
            "memory_id": memory_id,
            "branch": branch,
            "commits": [
                {
                    "hash": c.get("commit_hash"),
                    "version": c.get("version_num"),
                    "type": c.get("change_type"),
                    "category": c.get("category"),
                    "timestamp": c.get("snapshot_at"),
                    "author": c.get("snapshot_by"),
                }
                for c in commits
            ],
            "count": len(commits),
        }

    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            try:
                await _mcp_assert_memory_readable(conn, memory_id, user)
            except PermissionError as e:
                return {"success": False, "error": str(e)}

            rows = await mcp_repo.fetch_memory_log(conn, memory_id, branch, limit, user)
            return {
                "success": True,
                "memory_id": memory_id,
                "branch": branch,
                "commits": [
                    {
                        "hash": r["commit_hash"],
                        "version": r["version_num"],
                        "type": r["change_type"],
                        "category": r["category"],
                        "timestamp": r["snapshot_at"].isoformat(),
                        "author": r["snapshot_by"],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }

    except Exception as e:
        logger.error(f"[MCP] log_memory failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def tool_branch_memory(
    memory_id: str,
    name: str,
    from_commit: str | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Create new branch from HEAD or a specific commit."""
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    _safe_path_segment(name, label="name")
    if from_commit:
        _safe_path_segment(from_commit, label="from_commit")
    if user is None:
        body: dict[str, Any] = {"name": name}
        if from_commit:
            body["from_commit"] = from_commit
        branch_info = await _rest_post(f"/v1/memories/{safe_id}/branch", body)
        return {
            "success": True,
            "memory_id": memory_id,
            "branch": branch_info.get("name", name),
            "commit_hash": branch_info.get("head_commit_hash"),
            "created_by": branch_info.get("created_by"),
        }

    try:
        user = _mcp_user_required(user)
        await _mcp_enforce_write_rate_limit(
            tool_name="branch_memory",
            user=user,
            limit=BRANCH_MEMORY_RATE_LIMIT_PER_MINUTE,
        )
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            return await mcp_repo.create_memory_branch(conn, memory_id, name, from_commit, user)

    except Exception as e:
        logger.error(f"[MCP] branch_memory failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def tool_diff_memory_commits(
    memory_id: str,
    commit_a: str,
    commit_b: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Generate unified diff between two commits."""
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    safe_a = _safe_path_segment(commit_a, label="commit_a")
    safe_b = _safe_path_segment(commit_b, label="commit_b")
    if user is None:
        commit_a_row = await _rest_get(f"/v1/memories/{safe_id}/commits/{safe_a}")
        commit_b_row = await _rest_get(f"/v1/memories/{safe_id}/commits/{safe_b}")
        diff = difflib.unified_diff(
            commit_a_row["content"].splitlines(keepends=True),
            commit_b_row["content"].splitlines(keepends=True),
            fromfile=f"{commit_a[:8]} (v{commit_a_row['version_num']})",
            tofile=f"{commit_b[:8]} (v{commit_b_row['version_num']})",
            lineterm="",
        )
        return {
            "success": True,
            "memory_id": memory_id,
            "from_commit": commit_a,
            "to_commit": commit_b,
            "diff": "".join(diff),
        }

    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            try:
                await _mcp_assert_memory_readable(conn, memory_id, user)
            except PermissionError as e:
                return {"success": False, "error": str(e)}

            commit_a_row, commit_b_row = await mcp_repo.fetch_diff_commit_pair(
                conn,
                memory_id,
                commit_a,
                commit_b,
                user,
            )
            if not commit_a_row or not commit_b_row:
                return {"success": False, "error": "One or both commits not found"}

            diff = difflib.unified_diff(
                commit_a_row["content"].splitlines(keepends=True),
                commit_b_row["content"].splitlines(keepends=True),
                fromfile=f"{commit_a[:8]} (v{commit_a_row['version_num']})",
                tofile=f"{commit_b[:8]} (v{commit_b_row['version_num']})",
                lineterm="",
            )

            return {
                "success": True,
                "memory_id": memory_id,
                "from_commit": commit_a,
                "to_commit": commit_b,
                "diff": "".join(diff),
            }

    except Exception as e:
        logger.error(f"[MCP] diff_memory_commits failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def tool_checkout_memory(
    memory_id: str,
    commit_hash: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Fetch commit content and metadata by hash."""
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    safe_hash = _safe_path_segment(commit_hash, label="commit_hash")
    if user is None:
        row = await _rest_get(f"/v1/memories/{safe_id}/commits/{safe_hash}")
        return {
            "success": True,
            "memory_id": memory_id,
            "commit": {
                "hash": row.get("commit_hash"),
                "version": row.get("version_num"),
                "branch": row.get("branch"),
                "type": row.get("change_type"),
                "category": row.get("category"),
                "subcategory": row.get("subcategory"),
                "timestamp": row.get("snapshot_at"),
                "author": row.get("snapshot_by"),
            },
            "content": row.get("content"),
        }

    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            try:
                await _mcp_assert_memory_readable(conn, memory_id, user)
            except PermissionError as e:
                return {"success": False, "error": str(e)}

            row = await mcp_repo.fetch_checkout_commit(conn, memory_id, commit_hash, user)
            if not row:
                return {"success": False, "error": "Commit not found"}

            return {
                "success": True,
                "memory_id": memory_id,
                "commit": {
                    "hash": row["commit_hash"],
                    "version": row["version_num"],
                    "branch": row["branch"],
                    "type": row["change_type"],
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "timestamp": row["snapshot_at"].isoformat(),
                    "author": row["snapshot_by"],
                },
                "content": row["content"],
            }

    except Exception as e:
        logger.error(f"[MCP] checkout_memory failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


TOOLS: dict[str, dict[str, Any]] = {
    "log_memory": _tool(
        "Walk commit DAG from branch HEAD to root.",
        {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "branch": {"type": "string", "description": "Branch name (default: main)", "maxLength": 128},
            "limit": {
                "type": "integer",
                "description": "Max commits (default: 50)",
                "minimum": 1,
                "maximum": MCP_DEFAULT_LIMIT_MAX,
            },
        },
        ["memory_id"],
        tool_log_memory,
    ),
    "branch_memory": _tool(
        "Create new branch from HEAD or specific commit.",
        {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "name": {"type": "string", "description": "New branch name", "maxLength": 128},
            "from_commit": {"type": "string", "description": "Commit hash (default: main HEAD)", "maxLength": 128},
        },
        ["memory_id", "name"],
        tool_branch_memory,
    ),
    "diff_memory_commits": _tool(
        "Generate unified diff between two commits.",
        {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_a": {"type": "string", "description": "First commit hash (older)", "maxLength": 128},
            "commit_b": {"type": "string", "description": "Second commit hash (newer)", "maxLength": 128},
        },
        ["memory_id", "commit_a", "commit_b"],
        tool_diff_memory_commits,
    ),
    "checkout_memory": _tool(
        "Fetch commit content and metadata by hash.",
        {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_hash": {"type": "string", "description": "Commit hash to fetch", "maxLength": 128},
        },
        ["memory_id", "commit_hash"],
        tool_checkout_memory,
    ),
}

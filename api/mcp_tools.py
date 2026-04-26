"""MCP (Memory Context Protocol) tools for MNEMOS.

Exposes key MNEMOS functionality as tools accessible from OpenClaw and other agents:
- log_memory: Walk DAG commit history
- branch_memory: Create named branches
- diff_memory_commits: Unified diff between commits
- checkout_memory: Fetch commit content by hash
- recommend_model: Query cost optimizer
"""

import logging
from typing import Dict, Any, Optional

import api.lifecycle as _lc
from api.auth import UserContext


def _mcp_user_required(user: Optional[UserContext]) -> UserContext:
    """MCP version tools used to accept user=None silently. The HTTP
    surface always has an authenticated user; the MCP surface MUST
    too — slice 2 round 11 found this as a parallel cross-tenant
    read/write hole."""
    if user is None or not user.authenticated:
        raise PermissionError("authenticated user required for version tools")
    return user


def _mcp_is_root(user: UserContext) -> bool:
    return user.role == "root"


async def _mcp_assert_memory_readable(conn, memory_id: str, user: UserContext) -> None:
    """Same chokepoint as api/handlers/versions._assert_memory_readable.
    Inlined here to avoid circular import; logic must stay in sync."""
    if _mcp_is_root(user):
        row = await conn.fetchrow(
            "SELECT 1 FROM memory_versions WHERE memory_id = $1 LIMIT 1", memory_id,
        )
        if not row:
            raise PermissionError(f"Memory {memory_id} not found")
        return
    from api.visibility import read_visibility_predicate
    vis_clause, vis_params = read_visibility_predicate(
        user.user_id, list(user.group_ids), start_param_idx=2,
    )
    ns_ph = f"${len(vis_params) + 2}"
    row = await conn.fetchrow(
        f"SELECT 1 FROM memories WHERE id = $1 "
        f"AND {vis_clause} AND namespace = {ns_ph} LIMIT 1",
        memory_id, *vis_params, user.namespace,
    )
    if not row:
        raise PermissionError(f"Memory {memory_id} not found")

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Tool Implementations
# ────────────────────────────────────────────────────────────────────────────

async def tool_log_memory(
    memory_id: str,
    branch: str = "main",
    limit: int = 50,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Walk commit DAG from branch HEAD to root.

    Returns list of commits with hashes, change types, and metadata.
    Equivalent to `git log`.

    Args:
        memory_id: Memory ID to walk
        branch: Branch name (default: main)
        limit: Max commits to return (default: 50)
        user: User context for auth (optional)

    Returns:
        Dict with commits list and metadata
    """
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
            rows = await conn.fetch(
                """
                WITH RECURSIVE commit_walk AS (
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.change_type, mv.snapshot_at, mv.snapshot_by,
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
                    -- Same-memory predicate (mv.memory_id =
                    -- cw.memory_id) prevents corrupt
                    -- parent_version_id from pulling another
                    -- memory's version into this memory's log
                    -- (round-38 finding). Mirrors the HTTP log
                    -- handler in api/handlers/dag.py.
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.change_type, mv.snapshot_at, mv.snapshot_by,
                        mv.owner_id, mv.namespace, mv.permission_mode,
                        cw.depth + 1
                    FROM memory_versions mv
                    INNER JOIN commit_walk cw
                        ON mv.id = cw.parent_version_id
                       AND mv.memory_id = cw.memory_id
                    WHERE cw.depth < $4
                )
                SELECT
                    commit_hash, version_num, branch, category, change_type,
                    snapshot_at, snapshot_by, owner_id, namespace, permission_mode
                FROM commit_walk
                ORDER BY depth ASC
                LIMIT $4
                """,
                memory_id,
                branch,
                memory_id,
                limit,
            )
            # Per-snapshot filter applied client-side because the
            # recursive CTE doesn't compose cleanly with a WHERE on
            # the snapshot's own owner/permission_mode. Caller is
            # already gated by _mcp_assert_memory_readable above;
            # this is the historical-private-snapshot defense from
            # round 11.
            if not _mcp_is_root(user):
                def _snap_visible(r) -> bool:
                    if r["namespace"] != user.namespace:
                        return False
                    return (
                        r["owner_id"] == user.user_id
                        or (r["permission_mode"] % 10) >= 4
                    )
                rows = [r for r in rows if _snap_visible(r)]

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
        logger.error(f"[MCP] log_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_branch_memory(
    memory_id: str,
    name: str,
    from_commit: Optional[str] = None,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Create new branch from HEAD or specific commit.

    Args:
        memory_id: Memory ID to branch
        name: New branch name
        from_commit: Commit hash to branch from (default: main HEAD)
        user: User context for auth

    Returns:
        Dict with branch creation status and details
    """
    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Lock the live memory row for the duration of the
                # transaction. Pessimistic lock closes the round-14
                # TOCTOU: between auth check and INSERT, an
                # admin/import path could reassign owner/namespace
                # and the old owner could still create a branch row
                # on a memory they no longer own. SELECT ... FOR
                # SHARE blocks concurrent UPDATE on the parent for
                # the txn lifetime; the auth assertion below applies
                # to the locked row, and the INSERT runs in the same
                # transaction.
                if _mcp_is_root(user):
                    live = await conn.fetchrow(
                        "SELECT 1 FROM memories WHERE id = $1 FOR SHARE",
                        memory_id,
                    )
                else:
                    live = await conn.fetchrow(
                        "SELECT 1 FROM memories WHERE id = $1 "
                        "AND owner_id = $2 AND namespace = $3 FOR SHARE",
                        memory_id, user.user_id, user.namespace,
                    )
                if not live:
                    return {"success": False, "error": f"Memory {memory_id} not found"}

                # Resolve starting point
                if from_commit:
                    start = await conn.fetchrow(
                        "SELECT id, commit_hash FROM memory_versions "
                        "WHERE memory_id = $1 AND commit_hash = $2",
                        memory_id, from_commit,
                    )
                    if not start:
                        return {"success": False, "error": "Commit not found"}
                else:
                    start = await conn.fetchrow(
                        """
                        SELECT mv.id, mv.commit_hash
                        FROM memory_versions mv
                        INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                        WHERE mv.memory_id = $1 AND mb.name = 'main'
                        """,
                        memory_id,
                    )
                    if not start:
                        return {"success": False, "error": "main branch not found"}

                # Race-safe insert: ON CONFLICT DO NOTHING RETURNING.
                # If the row already exists (concurrent retry won),
                # RETURNING is empty and we re-read to classify.
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (memory_id, name) DO NOTHING
                    RETURNING head_version_id
                    """,
                    memory_id, name, start["id"], user.user_id,
                )

            if inserted is None:
                # Scope the JOIN by mb.memory_id = mv.memory_id
                # too. A stale branch row for THIS memory pointing
                # at ANOTHER memory's version_id would otherwise
                # let an idempotent retry return that foreign
                # commit_hash and silently legitimize the corrupt
                # branch (round-37 finding).
                existing = await conn.fetchrow(
                    "SELECT mb.head_version_id, mv.commit_hash "
                    "FROM memory_branches mb "
                    "INNER JOIN memory_versions mv "
                    "    ON mv.id = mb.head_version_id "
                    "   AND mv.memory_id = mb.memory_id "
                    "WHERE mb.memory_id = $1 AND mb.name = $2",
                    memory_id, name,
                )
                if existing is None:
                    # Either the branch row was missing (race) or
                    # it was a corrupt cross-memory pointer that
                    # the scoped JOIN excluded. Either way: don't
                    # claim idempotent success.
                    return {
                        "success": False,
                        "error": (
                            "branch exists but points at a foreign "
                            "memory version; reconciliation required"
                        ),
                    }
                # Implicit-HEAD retries (from_commit=None) are
                # idempotent regardless of whether main has advanced
                # since the original create — the caller didn't ask
                # for a specific commit, so any existing branch head
                # satisfies them. Explicit from_commit retries
                # require an exact match (otherwise it's a real
                # conflict on the caller's stated intent).
                head_matches = existing["head_version_id"] == start["id"]
                if from_commit is None or head_matches:
                    return {
                        "success": True,
                        "memory_id": memory_id,
                        "branch": name,
                        "commit_hash": existing["commit_hash"],
                        "created_by": user.user_id,
                        "idempotent": True,
                    }
                return {
                    "success": False,
                    "error": (
                        f"branch '{name}' already exists at a different "
                        f"head; refusing to silently move it"
                    ),
                }

            return {
                "success": True,
                "memory_id": memory_id,
                "branch": name,
                "commit_hash": start["commit_hash"],
                "created_by": user.user_id,
            }

    except Exception as e:
        logger.error(f"[MCP] branch_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_diff_memory_commits(
    memory_id: str,
    commit_a: str,
    commit_b: str,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Generate unified diff between two commits.

    Args:
        memory_id: Memory ID
        commit_a: First commit hash (older)
        commit_b: Second commit hash (newer)
        user: User context for auth

    Returns:
        Dict with unified diff and metadata
    """
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

            if _mcp_is_root(user):
                base_sql = (
                    "SELECT content, version_num FROM memory_versions "
                    "WHERE memory_id = $1 AND commit_hash = $2"
                )
                commit_a_row = await conn.fetchrow(base_sql, memory_id, commit_a)
                commit_b_row = await conn.fetchrow(base_sql, memory_id, commit_b)
            else:
                from api.visibility import version_visibility_predicate
                vis_clause, vis_params = version_visibility_predicate(
                    user.user_id, start_param_idx=3,
                )
                ns_ph = f"${len(vis_params) + 3}"
                gated_sql = (
                    "SELECT content, version_num FROM memory_versions "
                    "WHERE memory_id = $1 AND commit_hash = $2 "
                    f"AND {vis_clause} AND namespace = {ns_ph}"
                )
                commit_a_row = await conn.fetchrow(
                    gated_sql, memory_id, commit_a, *vis_params, user.namespace,
                )
                commit_b_row = await conn.fetchrow(
                    gated_sql, memory_id, commit_b, *vis_params, user.namespace,
                )

            if not commit_a_row or not commit_b_row:
                return {"success": False, "error": "One or both commits not found"}

            # Generate simple unified diff
            import difflib
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
        logger.error(f"[MCP] diff_memory_commits failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_checkout_memory(
    memory_id: str,
    commit_hash: str,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Fetch commit content and metadata by hash.

    Args:
        memory_id: Memory ID
        commit_hash: Commit hash to fetch
        user: User context for auth

    Returns:
        Dict with commit content and metadata
    """
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
            # Per-snapshot tenancy gate on the actual ver row.
            if _mcp_is_root(user):
                row = await conn.fetchrow(
                    """
                    SELECT
                        commit_hash, version_num, branch, category, subcategory,
                        content, change_type, snapshot_at, snapshot_by
                    FROM memory_versions
                    WHERE memory_id = $1 AND commit_hash = $2
                    """,
                    memory_id, commit_hash,
                )
            else:
                from api.visibility import version_visibility_predicate
                vis_clause, vis_params = version_visibility_predicate(
                    user.user_id, start_param_idx=3,
                )
                ns_ph = f"${len(vis_params) + 3}"
                row = await conn.fetchrow(
                    f"""
                    SELECT
                        commit_hash, version_num, branch, category, subcategory,
                        content, change_type, snapshot_at, snapshot_by
                    FROM memory_versions
                    WHERE memory_id = $1 AND commit_hash = $2
                      AND {vis_clause} AND namespace = {ns_ph}
                    """,
                    memory_id, commit_hash, *vis_params, user.namespace,
                )

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
        logger.error(f"[MCP] checkout_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_recommend_model(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Query model optimizer for cost-aware recommendation.

    Args:
        task_type: Task type (code_generation, reasoning, architecture_design, etc.)
        cost_budget: Max cost per 1M tokens (default: $10)
        quality_floor: Min quality score (default: 0.85)
        user: User context for auth

    Returns:
        Dict with recommended model and reasoning
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        # Map task types to capabilities
        capability_map = {
            "code_generation": ["coding"],
            "reasoning": ["reasoning", "logic"],
            "architecture_design": ["reasoning"],
            "summarization": ["reasoning"],
            "web_search": ["online", "search"],
        }
        required_caps = capability_map.get(task_type, ["reasoning"])

        async with pool.acquire() as conn:
            # Find models meeting criteria
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name,
                    input_cost_per_mtok, output_cost_per_mtok,
                    graeae_weight, context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND graeae_weight >= $1
                AND (input_cost_per_mtok + output_cost_per_mtok) / 2.0 <= $2
                AND capabilities @> $3
                ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                LIMIT 1
                """,
                quality_floor,
                cost_budget,
                required_caps,
            )

            if not models:
                # Fallback
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name,
                        input_cost_per_mtok, output_cost_per_mtok,
                        graeae_weight, context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                    LIMIT 1
                    """
                )

            if not models:
                return {"success": False, "error": "No models available"}

            model = models[0]
            avg_cost = (model["input_cost_per_mtok"] + model["output_cost_per_mtok"]) / 2.0

            return {
                "success": True,
                "task_type": task_type,
                "recommended": {
                    "provider": model["provider"],
                    "model_id": model["model_id"],
                    "display_name": model.get("display_name"),
                    "cost_per_mtok": float(avg_cost),
                    "quality_score": float(model["graeae_weight"]),
                    "context_window": model.get("context_window"),
                },
                "reasoning": f"Cheapest model with {', '.join(required_caps)} capability above quality floor {quality_floor}",
                "budget_met": avg_cost <= cost_budget,
            }

    except Exception as e:
        logger.error(f"[MCP] recommend_model failed: {e}")
        return {"success": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────────
# Tool Registry
# ────────────────────────────────────────────────────────────────────────────

TOOLS = {
    "log_memory": {
        "description": "Walk commit DAG from branch HEAD to root",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "branch": {"type": "string", "description": "Branch name (default: main)"},
            "limit": {"type": "integer", "description": "Max commits (default: 50)"},
        },
        "required": ["memory_id"],
        "handler": tool_log_memory,
    },
    "branch_memory": {
        "description": "Create new branch from HEAD or specific commit",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "name": {"type": "string", "description": "New branch name"},
            "from_commit": {"type": "string", "description": "Commit hash (default: main HEAD)"},
        },
        "required": ["memory_id", "name"],
        "handler": tool_branch_memory,
    },
    "diff_memory_commits": {
        "description": "Generate unified diff between two commits",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_a": {"type": "string", "description": "First commit hash (older)"},
            "commit_b": {"type": "string", "description": "Second commit hash (newer)"},
        },
        "required": ["memory_id", "commit_a", "commit_b"],
        "handler": tool_diff_memory_commits,
    },
    "checkout_memory": {
        "description": "Fetch commit content and metadata by hash",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_hash": {"type": "string", "description": "Commit hash to fetch"},
        },
        "required": ["memory_id", "commit_hash"],
        "handler": tool_checkout_memory,
    },
    "recommend_model": {
        "description": "Query model optimizer for cost-aware recommendation",
        "parameters": {
            "task_type": {
                "type": "string",
                "description": "Task type (code_generation, reasoning, architecture_design, etc.)",
            },
            "cost_budget": {"type": "number", "description": "Max $/MTok (default: 10.0)"},
            "quality_floor": {"type": "number", "description": "Min quality score (default: 0.85)"},
        },
        "required": ["task_type"],
        "handler": tool_recommend_model,
    },
}


async def execute_tool(
    tool_name: str,
    parameters: Dict[str, Any],
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Execute an MCP tool.

    Args:
        tool_name: Name of tool to execute
        parameters: Tool parameters
        user: User context for auth

    Returns:
        Tool result dict
    """
    if tool_name not in TOOLS:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    tool_info = TOOLS[tool_name]
    handler = tool_info["handler"]

    # Add user context to parameters
    parameters["user"] = user

    try:
        result = await handler(**parameters)
        logger.info(f"[MCP] Tool {tool_name} executed successfully")
        return result
    except Exception as e:
        logger.error(f"[MCP] Tool {tool_name} failed: {e}")
        return {"success": False, "error": str(e)}

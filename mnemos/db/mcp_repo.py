"""SQL helpers for MCP tool handlers."""

from __future__ import annotations

from typing import Any

from mnemos.core.auth_context import UserContext
from mnemos.core.visibility import read_visibility_predicate, version_visibility_predicate


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        if hasattr(row, "get"):
            return row.get(key, default)
        return default


async def assert_memory_readable(conn: Any, memory_id: str, user: UserContext) -> None:
    """Same chokepoint as api/routes/versions._assert_memory_readable."""
    if _is_root(user):
        row = await conn.fetchrow(
            "SELECT 1 FROM memory_versions WHERE memory_id = $1 LIMIT 1",
            memory_id,
        )
        if not row:
            raise PermissionError(f"Memory {memory_id} not found")
        return

    vis_clause, vis_params = read_visibility_predicate(
        user.user_id,
        list(user.group_ids),
        start_param_idx=2,
    )
    ns_ph = f"${len(vis_params) + 2}"
    row = await conn.fetchrow(
        f"SELECT 1 FROM memories WHERE id = $1 "
        f"AND {vis_clause} AND namespace = {ns_ph} LIMIT 1",
        memory_id,
        *vis_params,
        user.namespace,
    )
    if not row:
        raise PermissionError(f"Memory {memory_id} not found")


async def fetch_memory_log(
    conn: Any,
    memory_id: str,
    branch: str,
    limit: int,
    user: UserContext,
) -> list[Any]:
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
            -- Same-memory predicate (mv.memory_id = cw.memory_id)
            -- prevents corrupt parent_version_id from pulling another
            -- memory's version into this memory's log. Mirrors the HTTP
            -- log handler in api/routes/dag.py.
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

    if _is_root(user):
        return list(rows)

    def _snap_visible(row: Any) -> bool:
        if row["namespace"] != user.namespace:
            return False
        return row["owner_id"] == user.user_id or (row["permission_mode"] % 10) >= 4

    return [row for row in rows if _snap_visible(row)]


async def create_memory_branch(
    conn: Any,
    memory_id: str,
    name: str,
    from_commit: str | None,
    user: UserContext,
) -> dict[str, Any]:
    async with conn.transaction():
        # Lock the live memory row for the duration of the transaction.
        # This closes the TOCTOU between auth check and branch insert.
        if _is_root(user):
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
            return {"success": False, "error": f"Memory {memory_id} not found"}

        if from_commit:
            start = await _fetch_branch_start_by_commit(conn, memory_id, from_commit, user)
            if not start:
                return {"success": False, "error": "Commit not found"}
        else:
            start = await _fetch_main_branch_start(conn, memory_id, user)
            if not start:
                return {"success": False, "error": "main branch not found"}

        inserted = await conn.fetchrow(
            """
            INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (memory_id, name) DO NOTHING
            RETURNING head_version_id
            """,
            memory_id,
            name,
            start["id"],
            user.user_id,
        )

    if inserted is None:
        return await _handle_existing_branch(conn, memory_id, name, from_commit, start, user)

    return {
        "success": True,
        "memory_id": memory_id,
        "branch": name,
        "commit_hash": start["commit_hash"],
        "created_by": user.user_id,
    }


async def _fetch_branch_start_by_commit(
    conn: Any,
    memory_id: str,
    from_commit: str,
    user: UserContext,
) -> Any | None:
    if _is_root(user):
        return await conn.fetchrow(
            "SELECT id, commit_hash FROM memory_versions "
            "WHERE memory_id = $1 AND commit_hash = $2",
            memory_id,
            from_commit,
        )

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        start_param_idx=3,
    )
    ns_ph = f"${len(vis_params) + 3}"
    return await conn.fetchrow(
        "SELECT id, commit_hash FROM memory_versions "
        "WHERE memory_id = $1 AND commit_hash = $2 "
        f"AND {vis_clause} AND namespace = {ns_ph}",
        memory_id,
        from_commit,
        *vis_params,
        user.namespace,
    )


async def _fetch_main_branch_start(conn: Any, memory_id: str, user: UserContext) -> Any | None:
    if _is_root(user):
        return await conn.fetchrow(
            """
            SELECT mv.id, mv.commit_hash
            FROM memory_versions mv
            INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
            WHERE mv.memory_id = $1 AND mb.name = 'main'
            """,
            memory_id,
        )

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        start_param_idx=2,
        table_alias="mv",
    )
    ns_ph = f"${len(vis_params) + 2}"
    return await conn.fetchrow(
        f"""
        SELECT mv.id, mv.commit_hash
        FROM memory_versions mv
        INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
        WHERE mv.memory_id = $1 AND mb.name = 'main'
          AND {vis_clause} AND mv.namespace = {ns_ph}
        """,
        memory_id,
        *vis_params,
        user.namespace,
    )


async def _handle_existing_branch(
    conn: Any,
    memory_id: str,
    name: str,
    from_commit: str | None,
    start: Any,
    user: UserContext,
) -> dict[str, Any]:
    existing = await _fetch_existing_branch(conn, memory_id, name, user)
    if existing is None:
        return {
            "success": False,
            "error": (
                "branch exists but its head is not visible "
                "or points at a foreign memory version; "
                "reconciliation required"
            ),
        }

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


async def _fetch_existing_branch(
    conn: Any,
    memory_id: str,
    name: str,
    user: UserContext,
) -> Any | None:
    if _is_root(user):
        return await conn.fetchrow(
            "SELECT mb.head_version_id, mv.commit_hash "
            "FROM memory_branches mb "
            "INNER JOIN memory_versions mv "
            "    ON mv.id = mb.head_version_id "
            "   AND mv.memory_id = mb.memory_id "
            "WHERE mb.memory_id = $1 AND mb.name = $2",
            memory_id,
            name,
        )

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        start_param_idx=3,
        table_alias="mv",
    )
    ns_ph = f"${len(vis_params) + 3}"
    return await conn.fetchrow(
        "SELECT mb.head_version_id, mv.commit_hash "
        "FROM memory_branches mb "
        "INNER JOIN memory_versions mv "
        "    ON mv.id = mb.head_version_id "
        "   AND mv.memory_id = mb.memory_id "
        f"   AND {vis_clause} "
        f"   AND mv.namespace = {ns_ph} "
        "WHERE mb.memory_id = $1 AND mb.name = $2",
        memory_id,
        name,
        *vis_params,
        user.namespace,
    )


async def fetch_diff_commit_pair(
    conn: Any,
    memory_id: str,
    commit_a: str,
    commit_b: str,
    user: UserContext,
) -> tuple[Any | None, Any | None]:
    if _is_root(user):
        base_sql = (
            "SELECT content, version_num FROM memory_versions "
            "WHERE memory_id = $1 AND commit_hash = $2"
        )
        return (
            await conn.fetchrow(base_sql, memory_id, commit_a),
            await conn.fetchrow(base_sql, memory_id, commit_b),
        )

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        start_param_idx=3,
    )
    ns_ph = f"${len(vis_params) + 3}"
    gated_sql = (
        "SELECT content, version_num FROM memory_versions "
        "WHERE memory_id = $1 AND commit_hash = $2 "
        f"AND {vis_clause} AND namespace = {ns_ph}"
    )
    return (
        await conn.fetchrow(gated_sql, memory_id, commit_a, *vis_params, user.namespace),
        await conn.fetchrow(gated_sql, memory_id, commit_b, *vis_params, user.namespace),
    )


async def fetch_checkout_commit(
    conn: Any,
    memory_id: str,
    commit_hash: str,
    user: UserContext,
) -> Any | None:
    if _is_root(user):
        return await conn.fetchrow(
            """
            SELECT
                commit_hash, version_num, branch, category, subcategory,
                content, change_type, snapshot_at, snapshot_by
            FROM memory_versions
            WHERE memory_id = $1 AND commit_hash = $2
            """,
            memory_id,
            commit_hash,
        )

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        start_param_idx=3,
    )
    ns_ph = f"${len(vis_params) + 3}"
    return await conn.fetchrow(
        f"""
        SELECT
            commit_hash, version_num, branch, category, subcategory,
            content, change_type, snapshot_at, snapshot_by
        FROM memory_versions
        WHERE memory_id = $1 AND commit_hash = $2
          AND {vis_clause} AND namespace = {ns_ph}
        """,
        memory_id,
        commit_hash,
        *vis_params,
        user.namespace,
    )


async def fetch_recommended_model(
    conn: Any,
    task_type: str,
    cost_budget: float,
    quality_floor: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    capability_map = {
        "code_generation": ["coding"],
        "reasoning": ["reasoning", "logic"],
        "architecture_design": ["reasoning"],
        "summarization": ["reasoning"],
        "web_search": ["online", "search"],
    }
    required_caps = capability_map.get(task_type, ["reasoning"])

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
        return None, required_caps

    model = models[0]
    avg_cost = (
        _row_get(model, "input_cost_per_mtok")
        + _row_get(model, "output_cost_per_mtok")
    ) / 2.0
    return {
        "provider": _row_get(model, "provider"),
        "model_id": _row_get(model, "model_id"),
        "display_name": _row_get(model, "display_name"),
        "cost_per_mtok": float(avg_cost),
        "quality_score": float(_row_get(model, "graeae_weight")),
        "context_window": _row_get(model, "context_window"),
    }, required_caps

"""Shared read-visibility predicate for non-root callers.

Mirrors the active PostgreSQL RLS read policies (see
db/migrations_v1_multiuser.sql and follow-up policy migrations) at
the app layer. The same predicate must be applied across every read
surface — list, get, search, rehydrate, gateway context — because
PostgreSQL combines RLS with the handler's WHERE via AND. RLS cannot
re-add rows the handler has already excluded; if one read path uses a
narrower predicate than another, that path silently hides rows the
rest of the contract admits.

Mutation paths (update, delete) deliberately do NOT use this
predicate — writes stay strictly owner-scoped so a non-owner can't
edit a world/group-readable row they happen to be able to read.
"""

from __future__ import annotations

from typing import List, NoReturn, Tuple

from fastapi import HTTPException


def handle_trigger_pgerror(exc: Exception) -> NoReturn:
    """Translate trigger-raised Postgres errors into API conflicts."""
    if getattr(exc, "sqlstate", None) == "MN001":
        raise HTTPException(
            status_code=409,
            detail=(
                "Memory branch state is inconsistent: "
                "the branch row is missing, has a NULL "
                "head_version_id, or points to a version from "
                "another memory. Reconcile memory_branches and "
                "memory_versions for this memory before retrying."
            ),
        ) from exc
    raise exc


def read_visibility_predicate(
    user_id: str,
    group_ids: List[str],
    start_param_idx: int,
    table_alias: str = "",
) -> Tuple[str, list]:
    """Build the read-visibility WHERE clause + its params.

    Returns ``(clause, params)`` where ``clause`` is a parenthesized
    SQL fragment using $-placeholders starting at
    ``start_param_idx``, and ``params`` is the list of values to
    extend the caller's params list with (in the order the
    placeholders appear).

    Branches mirror the active RLS read policies:

    - ``mnemos_owner_select``  → ``owner_id = $caller``
    - ``federation`` (v3.2 H1) → ``federation_source IS NOT NULL``
    - ``mnemos_world_select``  → ``(permission_mode % 10) >= 4``
      (extract Unix-style world bits via ones-digit)
    - ``mnemos_group_select``  → ``((permission_mode / 10) % 10) >= 4
                                   AND group_id IS NOT NULL
                                   AND group_id = ANY($groups)``
      (extract Unix-style group bits via tens-digit; permission_mode
      = 700 has group bits = 0, so the row is owner-only even though
      the owner bit is readable).

    ``group_ids`` is sourced from ``UserContext.group_ids`` (resolved
    at auth time) rather than re-querying ``user_groups`` via EXISTS;
    same authoritative source the RLS policy uses, just pre-resolved.

    ``table_alias`` is prepended to every column reference (e.g.
    ``"m"`` → ``m.owner_id``) for queries that join multiple tables
    and need disambiguation. Default empty produces unqualified
    column names suitable for single-table queries.

    """
    n = start_param_idx
    p = f"{table_alias}." if table_alias else ""
    clause = (
        "("
        f"{p}owner_id=${n}"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL "
        f"AND {p}group_id = ANY(${n + 1}::text[]))"
        ")"
    )
    return clause, [user_id, list(group_ids)]


def version_visibility_predicate(
    user_id: str,
    start_param_idx: int,
    table_alias: str = "",
) -> Tuple[str, list]:
    """Per-snapshot visibility predicate for ``memory_versions`` rows.

    Snapshot tenancy is evaluated against THE SNAPSHOT's own
    ``owner_id`` / ``namespace`` / ``permission_mode`` columns, NOT
    the live memory's. This closes a class of bug Codex flagged
    where a memory created private (mode 600), snapshotted into v1,
    later relaxed to public (mode 644) lets every reader of v2+ also
    fetch the v1 private snapshot via ``list_versions`` /
    ``get_version`` / ``diff_versions``.

    Narrower than ``read_visibility_predicate`` because
    ``memory_versions`` does NOT carry ``group_id`` or
    ``federation_source`` columns (introduced after v2 versioning).
    Snapshots that were group-readable or federated at the time
    they were taken are NOT visible per-version — fail-closed
    against missing historical fields. Backfilling those columns
    onto ``memory_versions`` is a separate migration decision.

    Branches:
    - owner: ``owner_id = $caller``
    - world: ``(permission_mode % 10) >= 4``

    The namespace pin (a separate ``namespace = $`` predicate) is
    expected to be added by the caller alongside this clause.
    """
    n = start_param_idx
    p = f"{table_alias}." if table_alias else ""
    clause = (
        "("
        f"{p}owner_id=${n}"
        f" OR ({p}permission_mode % 10) >= 4"
        ")"
    )
    return clause, [user_id]


async def _assert_target_head_visible(
    conn,
    head_version_id: str,
    user,
    not_found_detail: str,
) -> None:
    """Fail closed when a write target HEAD is invisible to the caller.

    DAG write paths copy tenancy from the target branch HEAD into the new
    commit. Non-root callers must therefore be able to read that target
    snapshot directly before it can define the tenancy of a new version.
    Callers are expected to run this after locking the branch row that yielded
    ``head_version_id``.
    """
    if getattr(user, "role", None) == "root":
        return

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id, start_param_idx=2,
    )
    ns_ph = f"${len(vis_params) + 2}"
    row = await conn.fetchrow(
        f"SELECT 1 FROM memory_versions "
        f"WHERE id = $1 "
        f"AND {vis_clause} AND namespace = {ns_ph}",
        head_version_id, *vis_params, user.namespace,
    )
    if not row:
        raise HTTPException(status_code=404, detail=not_found_detail)

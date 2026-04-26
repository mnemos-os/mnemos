"""Shared read-visibility predicate for non-root callers.

Mirrors the v1_multiuser RLS policies (see
db/migrations_v1_multiuser.sql) at the app layer. The same predicate
must be applied across every read surface — list, get, search,
rehydrate, gateway context — because PostgreSQL combines RLS with
the handler's WHERE via AND. RLS cannot re-add rows the handler has
already excluded; if one read path uses a narrower predicate than
another, that path silently hides rows the rest of the contract
admits.

Mutation paths (update, delete) deliberately do NOT use this
predicate — writes stay strictly owner-scoped so a non-owner can't
edit a world/group-readable row they happen to be able to read.
"""

from __future__ import annotations

from typing import List, Tuple


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

    Branches mirror the v1_multiuser RLS policies:

    - ``mnemos_owner_select``  → ``owner_id = $caller``
    - ``federation`` (v3.2 H1) → ``federation_source IS NOT NULL``
    - ``mnemos_world_select``  → ``(permission_mode % 10) >= 4``
    - ``mnemos_group_select``  → ``permission_mode >= 640
                                   AND group_id IS NOT NULL
                                   AND group_id = ANY($groups)``

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
        f" OR ({p}permission_mode >= 640 AND {p}group_id IS NOT NULL "
        f"AND {p}group_id = ANY(${n + 1}::text[]))"
        ")"
    )
    return clause, [user_id, list(group_ids)]

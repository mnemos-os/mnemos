"""Helpers for GDPR deletion-log audit rows."""

from __future__ import annotations

from typing import Any

VALID_REQUEST_KINDS = {"gdpr_wipe", "admin_purge", "tombstone_collected"}


def _validate_request_kind(request_kind: str) -> str:
    if request_kind not in VALID_REQUEST_KINDS:
        raise ValueError(f"invalid deletion_log request_kind: {request_kind!r}")
    return request_kind


def _source_array(source: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if source is None:
        return None
    return [str(item) for item in source]


# #192: removed `_row_get`, `_sha256_hex`, `_looks_like_sqlite_conn`
# — all defined but never called inside this module nor imported
# anywhere. The live deletion-log writers (log_target_memory_
# deletions, log_morpheus_run_memory_deletions) compute content
# hashes via PostgreSQL `digest(..., 'sha256')` directly and don't
# need a Python-side hex helper. The sqlite-conn detector is
# duplicated as a live function in mcp_audit_repo.py.


# #184: removed `log_deleted_memory_row` (single-row variant) —
# defined but never called. The live deletion-log writers are
# `log_target_memory_deletions` (set-scope, used by the deletion
# request worker) and `log_morpheus_run_memory_deletions` (set-
# scope by morpheus run id). If a single-row writer is ever needed,
# wrap one of those with a 1-row argument list rather than
# resurrecting this dead variant.


async def log_target_memory_deletions(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    *,
    requested_by: str,
    requested_at: Any = None,
    request_kind: str = "tombstone_collected",
    reason: str | None = None,
    source: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Audit all soft-deleted memories in an owner/namespace wipe scope."""
    await conn.execute(
        """
        INSERT INTO deletion_log (
            memory_id, content_hash, owner_id, namespace,
            requested_by, requested_at, request_kind, reason, source
        )
        SELECT
            id,
            encode(digest(COALESCE(content, ''), 'sha256'), 'hex'),
            owner_id,
            namespace,
            $3,
            COALESCE($4::timestamptz, NOW()),
            $5,
            $6,
            $7::text[]
          FROM memories
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
        target_user_id,
        target_namespace,
        requested_by,
        requested_at,
        _validate_request_kind(request_kind),
        reason,
        _source_array(source),
    )


async def log_morpheus_run_memory_deletions(
    conn: Any,
    run_id: str,
    *,
    requested_by: str,
    requested_at: Any = None,
    request_kind: str = "admin_purge",
    reason: str | None = None,
    source: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Audit run-created MORPHEUS memories before rollback deletes them."""
    await conn.execute(
        """
        INSERT INTO deletion_log (
            memory_id, content_hash, owner_id, namespace,
            requested_by, requested_at, request_kind, reason, source
        )
        SELECT
            id,
            encode(digest(COALESCE(content, ''), 'sha256'), 'hex'),
            owner_id,
            namespace,
            $2,
            COALESCE($3::timestamptz, NOW()),
            $4,
            $5,
            $6::text[]
          FROM memories
         WHERE morpheus_run_id = $1::uuid
           AND provenance = 'morpheus_local'
           AND deleted_at IS NULL
        """,
        run_id,
        requested_by,
        requested_at,
        _validate_request_kind(request_kind),
        reason,
        _source_array(source),
    )


async def fetch_deletion_log(
    conn: Any,
    *,
    from_ts: Any,
    to_ts: Any,
    owner_id: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Any], int]:
    clauses = ["requested_at >= $1", "requested_at <= $2"]
    args: list[Any] = [from_ts, to_ts]
    if owner_id:
        args.append(owner_id)
        clauses.append(f"owner_id = ${len(args)}")

    where = " AND ".join(clauses)
    total = int(
        await conn.fetchval(
            f"SELECT COUNT(*) FROM deletion_log WHERE {where}",
            *args,
        )
        or 0
    )
    limit_param = len(args) + 1
    offset_param = len(args) + 2
    rows = await conn.fetch(
        f"""
        SELECT *
          FROM deletion_log
         WHERE {where}
         ORDER BY requested_at DESC, executed_at DESC, id DESC
         LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *args,
        page_size,
        (page - 1) * page_size,
    )
    return list(rows), total

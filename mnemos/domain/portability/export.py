"""MPF export orchestration."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from mnemos.core.security import is_root
from mnemos.db import portability_repo as repo


def _encode_deletion_log_cursor(
    executed_at: str,
    row_id: str,
    *,
    export_as_of: str,
    deletion_log_from: Optional[str] = None,
    deletion_log_to: Optional[str] = None,
    effective_owner: Optional[str] = None,
    effective_ns: Optional[str] = None,
) -> str:
    """Pack the cursor state into an opaque base64-JSON token.

    Cursor carries:
    - executed_at, id: the keyset position (last row of the prior page)
    - export_as_of: snapshot anchor — DB-side `SELECT now()` from the
      first page's transaction. Every page filters
      `executed_at <= export_as_of` so rows committed during the
      export loop don't bleed in. Note: this is best-effort, not a
      true MVCC snapshot — see export_memories docstring for the
      late-commit caveat.
    - deletion_log_from, deletion_log_to: original operator window.
      Always packed (explicit null for unbounded sides). Subsequent
      pages derive both bounds SOLELY from the cursor; combining
      cursor + request-side window params is rejected at the route.
    - effective_owner, effective_ns: the page-1 tenant scope. Always
      packed (explicit null for cross-tenant root exports). Subsequent
      pages derive scope SOLELY from the cursor; combining cursor +
      request-side owner_id/namespace is rejected at the route. This
      prevents an attack where a root operator paginates with a
      cursor for owner=A and silently switches to owner=B's data.

    Cursor is opaque; operators round-trip the string verbatim.
    """
    # #160: defense-in-depth alongside the decoder check. Empty-string
    # scope is ambiguous (cursors use null for unscoped exports, never
    # an empty string), and packing one would round-trip to a 400 from
    # the decoder on the next page. Catch it at the source so the bug
    # can't propagate into a cursor in the first place.
    for name, val in (("effective_owner", effective_owner),
                      ("effective_ns", effective_ns)):
        if val == "":
            raise ValueError(
                f"_encode_deletion_log_cursor: {name} must be a non-empty "
                f"string or None; empty string is not a valid scope value"
            )

    # Always pack window AND scope fields, even when None, so the
    # cursor is fully self-contained. The route enforces that combining
    # cursor with window or scope params is rejected.
    payload = {
        "executed_at": executed_at,
        "id": row_id,
        "export_as_of": export_as_of,
        "deletion_log_from": deletion_log_from,
        "deletion_log_to": deletion_log_to,
        "effective_owner": effective_owner,
        "effective_ns": effective_ns,
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii").rstrip("=")


def _validate_cursor_uuid(value: str) -> str:
    """Strict UUID validation. Raises ValueError on malformed input."""
    import uuid as _uuid

    if not isinstance(value, str) or not value:
        raise ValueError("cursor id must be a non-empty UUID string")
    # uuid.UUID() raises on malformed inputs.
    parsed = _uuid.UUID(value)
    return str(parsed)


def _validate_cursor_iso_datetime(value: str) -> str:
    """Strict timezone-aware ISO datetime validation."""
    if not isinstance(value, str) or not value:
        raise ValueError("cursor timestamp must be a non-empty ISO-8601 string")
    # Python's fromisoformat handles +00:00 and ...Z (3.11+) shapes.
    # Normalize trailing 'Z' to '+00:00' for older Python compat.
    candidate = value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError(
            "cursor timestamp must be timezone-aware "
            "(e.g. ...Z or ...+00:00)"
        )
    return value


def _decode_deletion_log_cursor(token: str) -> Dict[str, Any]:
    """Unpack an opaque cursor back into a dict.

    Required keys: executed_at, id, export_as_of.
    Always-present keys: deletion_log_from, deletion_log_to,
    effective_owner, effective_ns (None for unbounded / cross-tenant
    sides). Cursor is fully self-contained — no falling back to
    request params for window or scope.

    Strict validation: rejects malformed b64, non-JSON payload, missing
    required fields, empty strings, non-UUID id, naive/non-ISO
    timestamps. Each failure surfaces HTTP 400 with the specific reason.
    """
    try:
        # Pad back to a multiple of 4 for b64 decode.
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        data = json.loads(decoded)
        if not isinstance(data, dict):
            raise ValueError("cursor payload must be a JSON object")
        executed_at_raw = data.get("executed_at")
        row_id_raw = data.get("id")
        export_as_of_raw = data.get("export_as_of")
        if executed_at_raw is None or row_id_raw is None or export_as_of_raw is None:
            raise ValueError(
                "cursor must contain executed_at, id, and export_as_of"
            )
        # Window-preservation fields are always present on cursors
        # produced by this server. Tolerate missing keys for older
        # cursors (forward-compat), but a present key with a value MUST
        # validate as ISO datetime.
        dl_from_raw = data.get("deletion_log_from")
        dl_to_raw = data.get("deletion_log_to")
        # Tenant-scope binding: effective_owner / effective_ns from
        # page-1. Tolerate missing keys for forward-compat with older
        # cursors. Validate types when present (string or null).
        # #159: also reject empty strings — they're ambiguous and
        # bypass the per-tenant guard for root callers (the SQL would
        # filter on owner_id="" / namespace="" which never matches a
        # real memory but is still an unexpected query shape).
        # Cursors written by this server use null for unscoped, never
        # an empty string.
        owner_raw = data.get("effective_owner")
        ns_raw = data.get("effective_ns")
        for name, val in (("effective_owner", owner_raw), ("effective_ns", ns_raw)):
            if val is not None and not isinstance(val, str):
                raise ValueError(f"cursor {name} must be a string or null")
            if isinstance(val, str) and val == "":
                raise ValueError(
                    f"cursor {name} must be a non-empty string or null; "
                    f"empty string is not a valid scope value"
                )
        result: Dict[str, Any] = {
            "executed_at": _validate_cursor_iso_datetime(executed_at_raw),
            "id": _validate_cursor_uuid(row_id_raw),
            "export_as_of": _validate_cursor_iso_datetime(export_as_of_raw),
            "deletion_log_from": (
                _validate_cursor_iso_datetime(dl_from_raw)
                if dl_from_raw is not None
                else None
            ),
            "deletion_log_to": (
                _validate_cursor_iso_datetime(dl_to_raw)
                if dl_to_raw is not None
                else None
            ),
            "effective_owner": owner_raw,
            "effective_ns": ns_raw,
            # Sentinel for forward-compat: distinguishes a legacy cursor
            # (no scope keys, fall back to request-derived scope) from
            # a current cursor (scope keys present, used as sole source).
            "_has_scope": "effective_owner" in data or "effective_ns" in data,
        }
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"deletion_log_cursor is malformed ({exc!s}). The cursor "
                "is an opaque token returned by a previous /v1/export "
                "call as `deletion_log_next_cursor`; pass it back "
                "verbatim, do not edit."
            ),
        )

from .schemas import (
    MPF_VERSION,
    MPF_VERSION_V0_2,
    SOURCE_SYSTEM,
    SOURCE_VERSION,
    MPFEnvelope,
)
from .serializers import (
    _compression_variant_to_entry,
    _deletion_log_to_entry,
    _kg_triple_to_entry,
    _memory_to_record,
    _memory_version_to_entry,
)
from .version_topology import _topo_sort_versions

_EXPORT_HARD_LIMIT = 10_000
_EXPORT_SIDECAR_HARD_LIMIT = 50_000


def _enforce_sidecar_cap(rows, surface: str) -> None:
    if len(rows) > _EXPORT_SIDECAR_HARD_LIMIT:
        # deletion_log scope is not memory-bound (it tracks deleted
        # rows), so category/limit/offset narrowing on the live records
        # query doesn't shrink it. Surface this distinctly so an
        # operator with >50k tombstones for one owner/namespace gets
        # accurate guidance.
        if surface == "deletion_log":
            detail = (
                f"deletion_log export exceeds the per-surface hard limit "
                f"of {_EXPORT_SIDECAR_HARD_LIMIT} rows AND no stable "
                f"keyset cursor could be derived (e.g., the row at the "
                f"cap boundary lacks executed_at or id). Normally the "
                f"server emits `deletion_log_next_cursor` and slices the "
                f"page to the cap; this 413 only fires as a defensive "
                f"fallback. Investigate the deletion_log table for rows "
                f"with NULL executed_at or id."
            )
        else:
            detail = (
                f"{surface} export exceeds the per-surface hard limit of "
                f"{_EXPORT_SIDECAR_HARD_LIMIT} rows for one envelope. "
                f"Narrow the slice (filter by category, owner_id, "
                f"namespace, or a smaller `limit`) and re-export, or "
                f"split the export into multiple chunks."
            )
        raise HTTPException(status_code=413, detail=detail)


def _resolve_export_version(requested: Optional[str]) -> str:
    """Map a requested mpf_version string to the actual emission version.

    Acceptable inputs: None / "0.1" / "0.1.x" → emit v0.1.1 (default,
    backward-compatible). "0.2" / "0.2.x" → emit v0.2.0. Anything else
    is rejected with 400 so operators don't get a silent default.

    Match is anchored on whole-segment major.minor — `0.10.0` is rejected
    rather than treated as a 0.1.x variant.
    """
    if requested is None:
        return MPF_VERSION
    raw = requested.strip()
    # Exact "0.1" / "0.2" or "0.1.<digits...>" / "0.2.<digits...>".
    if raw == "0.1" or raw.startswith("0.1."):
        return MPF_VERSION
    if raw == "0.2" or raw.startswith("0.2."):
        return MPF_VERSION_V0_2
    raise HTTPException(
        status_code=400,
        detail=(
            f"Unsupported mpf_version={requested!r}. Supported: 0.1.x "
            "(default, legacy), 0.2.x (PROV-DM provenance + bi-temporal)."
        ),
    )


async def export_memories(
    conn,
    *,
    user,
    category: Optional[str],
    limit: int,
    offset: int,
    owner_id: Optional[str],
    namespace: Optional[str],
    include_sidecars: bool,
    include_unattached_kg: bool = False,
    mpf_version: Optional[str] = None,
    deletion_log_from: Optional[str] = None,
    deletion_log_to: Optional[str] = None,
    deletion_log_cursor: Optional[str] = None,
) -> MPFEnvelope:
    """Render an MPF v0.1 or v0.2 envelope for the requested scope.

    Snapshot semantics for the deletion_log sidecar (v0.2 + paginated):

    - WITHIN a single export call, all reads are consistent: the call
      runs under `repeatable_read` isolation, so the memories,
      sidecars, and deletion_log rows reflect one MVCC snapshot.
    - ACROSS paginated calls, the cursor's `export_as_of` field caps
      the upper bound on `executed_at`. This is **best-effort, not a
      true cross-call snapshot.** A deletion transaction that started
      before page 1, wrote `executed_at = now()` (transaction-start
      time), and committed *after* page 1 has its row become visible
      on later pages — but its `executed_at` may sort before the
      cursor's keyset position, causing it to be silently skipped.
      For audit-strict deployments that need true cross-call
      consistency, the proper fix is to materialize the export job
      (held snapshot via `pg_export_snapshot()` + `SET TRANSACTION
      SNAPSHOT` on subsequent pages, or a server-side job that pins
      the snapshot for the duration of the export). That is not
      shipped in v0.2; operators paginating on a quiescent system or
      tolerating a small late-commit window are the supported case.

    Late-commit visibility window in practice: the time between
    `BEGIN` of the deletion transaction and `COMMIT` of that
    transaction. For typical wipe operations measured in
    milliseconds, this is negligible. For long-running batch
    deletions, it could be seconds. Operators should run pages
    back-to-back to minimize exposure.

    Operational mitigation for audit-grade exports (until v0.3 ships
    materialized-snapshot pagination):

    1. **Quiesce deletes during the export.** Block /v1/memories
       DELETE during the paginated export window. This is the only
       way to guarantee zero omitted tombstones with the current
       executed_at-keyset shape.
    2. **Run pages back-to-back.** Don't checkpoint the cursor and
       resume hours later — every minute the cursor is held is
       another minute a late-committed transaction can land below
       the keyset position. Drain to completion in one operator
       session.
    3. **Cross-check by count.** If audit-completeness matters,
       run `SELECT count(*) FROM deletion_log WHERE executed_at <=
       <export_as_of>` against the same DB after the export and
       compare to the sum of paginated page sizes.

    The full fix (held snapshot via pg_export_snapshot + materialized
    export jobs) is roadmap'd as part of MPF v0.3 — this caveat is
    explicitly accepted for v0.2 per codex round-3 and round-6
    review.
    """
    emit_version = _resolve_export_version(mpf_version)
    if is_root(user):
        effective_owner = owner_id
        effective_ns = namespace
    else:
        if owner_id and owner_id != user.user_id:
            raise HTTPException(status_code=403, detail="cross-owner export requires root")
        if namespace and namespace != user.namespace:
            raise HTTPException(status_code=403, detail="cross-namespace export requires root")
        effective_owner = user.user_id
        effective_ns = user.namespace

    # Cursor decode + scope authorization. Done BEFORE the transaction
    # so the cursor's bound scope governs every surface in the
    # envelope (records, KG, sidecars, deletion_log) — not just the
    # deletion_log query. Without this, a root paginated export with
    # cursor-bound deletion_log scope would still fetch records etc.
    # cross-tenant on page 2+ (codex round-5 HIGH).
    cursor_data: Optional[Dict[str, Any]] = None
    cursor_dl_from: Optional[str] = None
    cursor_dl_to: Optional[str] = None
    cursor_export_as_of: Optional[str] = None
    cursor_executed_at: Optional[str] = None
    cursor_row_id: Optional[str] = None
    # Treat presence as `is not None` — an empty string `""` would
    # otherwise bypass the v0.2-only guard, scope binding, and
    # forgery-rejection (codex round-6 medium). For root page-2
    # exports that correctly omit owner_id/namespace because the
    # cursor is supposed to carry scope, a blank token would
    # silently fall back to unscoped root export.
    if deletion_log_cursor is not None:
        if deletion_log_cursor == "":
            raise HTTPException(
                status_code=400,
                detail=(
                    "deletion_log_cursor must be a non-empty opaque "
                    "token. Omit the parameter entirely to start "
                    "pagination from page 1."
                ),
            )
        if not emit_version.startswith("0.2"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "deletion_log_cursor is a v0.2-only field; pass "
                    "mpf_version=0.2 to use cursor pagination."
                ),
            )
        cursor_data = _decode_deletion_log_cursor(deletion_log_cursor)
        # Reject legacy scope-less cursors with a clear 400 — operators
        # restart pagination after the round-4 cutover. Allowing the
        # fallback would broaden the deletion_log to the request-derived
        # scope (which the route also rejects in combination), creating
        # a no-cursor-scope visibility gap.
        if not cursor_data.get("_has_scope"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "deletion_log_cursor is from a pre-round-4 export "
                    "and lacks tenant-scope binding. Restart pagination "
                    "from page 1 (omit deletion_log_cursor) to obtain "
                    "a scope-bound cursor for subsequent pages."
                ),
            )
        # Authorization: for non-root users, the cursor's tenant scope
        # MUST equal the authenticated user's. This blocks the
        # forge-a-cursor-with-victim-tenant attack — without this
        # check, the cursor scope is unsigned base64-JSON that any
        # caller can mint. For root, accept the cursor scope as
        # canonical (root has explicit cross-tenant authority).
        if not is_root(user):
            if cursor_data.get("effective_owner") != user.user_id:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "deletion_log_cursor scope does not match "
                        "authenticated user; cross-owner cursor reuse "
                        "requires root."
                    ),
                )
            if cursor_data.get("effective_ns") != user.namespace:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "deletion_log_cursor scope does not match "
                        "authenticated user; cross-namespace cursor "
                        "reuse requires root."
                    ),
                )
        # Cursor is the sole source of truth for window AND scope on
        # subsequent pages — apply BEFORE we fetch any sidecar so all
        # envelope surfaces stay in the cursor-bound scope.
        cursor_dl_from = cursor_data["deletion_log_from"]
        cursor_dl_to = cursor_data["deletion_log_to"]
        cursor_export_as_of = cursor_data["export_as_of"]
        cursor_executed_at = cursor_data["executed_at"]
        cursor_row_id = cursor_data["id"]
        effective_owner = cursor_data["effective_owner"]
        effective_ns = cursor_data["effective_ns"]

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        rows = await repo.fetch_memory_export(
            conn,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            category=category,
            limit=limit,
            offset=offset,
        )
        records = [_memory_to_record(dict(r), mpf_version=emit_version) for r in rows]
        # Cross-tenant ID leak guard for v0.2 exports: Morpheus synthesis
        # builds source_memories from cluster members across namespaces,
        # then assigns the summary to the majority-owner. A non-root
        # export scoped to that owner could otherwise expose other
        # owners' memory IDs through wasInfluencedBy. Filter to only
        # IDs that appear in the export's own records[] — out-of-scope
        # source IDs would be unresolvable for the importer anyway.
        if emit_version.startswith("0.2"):
            in_scope_ids = {r.id for r in records}
            for rec in records:
                if rec.provenance and "wasInfluencedBy" in rec.provenance:
                    filtered = [
                        infl
                        for infl in rec.provenance["wasInfluencedBy"]
                        if not (
                            infl.get("type") == "memory"
                            and infl.get("id") not in in_scope_ids
                        )
                    ]
                    if filtered:
                        rec.provenance["wasInfluencedBy"] = filtered
                    else:
                        rec.provenance.pop("wasInfluencedBy", None)

        kg_triples_out: Optional[List[Dict[str, Any]]] = None
        memory_versions_out: Optional[List[Dict[str, Any]]] = None
        compression_manifest_out: Optional[List[Dict[str, Any]]] = None
        deletion_log_out: Optional[List[Dict[str, Any]]] = None
        deletion_log_next_cursor: Optional[str] = None

        if include_sidecars:
            memory_ids = [r["id"] for r in rows]
            kg_rows = await repo.fetch_kg_triples_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                include_unattached=include_unattached_kg,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(kg_rows, "kg_triples")
            kg_triples_out = [
                _kg_triple_to_entry(dict(r), mpf_version=emit_version)
                for r in kg_rows
            ]

            mv_rows = await repo.fetch_memory_versions_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                effective_ns=effective_ns,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(mv_rows, "memory_versions")
            memory_versions_out = _topo_sort_versions(
                [
                    _memory_version_to_entry(dict(r), mpf_version=emit_version)
                    for r in mv_rows
                ]
            )

            cv_rows = await repo.fetch_compressed_variants_for_export(
                conn,
                memory_ids=memory_ids,
                effective_owner=effective_owner,
                hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
            )
            _enforce_sidecar_cap(cv_rows, "compression_manifest")
            compression_manifest_out = [
                _compression_variant_to_entry(dict(r), mpf_version=emit_version)
                for r in cv_rows
            ]

            # deletion_log: v0.2-only. The v0.1 spec has no deletion_log
            # sidecar, and the v0.1 envelope shouldn't carry it. Scope by
            # owner/namespace (not by live memory ids — the whole point
            # is tracking DELETED ones). Window + scope + snapshot anchor
            # are resolved above (before the transaction) — see cursor
            # decode block. effective_owner / effective_ns above are
            # already cursor-bound when a cursor is present.
            if emit_version.startswith("0.2"):
                # Window from cursor (page 2+) or request (page 1).
                effective_dl_from = (
                    cursor_dl_from if cursor_data is not None else deletion_log_from
                )
                effective_dl_to = (
                    cursor_dl_to if cursor_data is not None else deletion_log_to
                )
                # Snapshot anchor: cursor wins on page 2+; page 1 derives
                # from the DB (not the app clock — clock skew between app
                # and DB would otherwise let rows leak across the boundary).
                if cursor_export_as_of is not None:
                    export_as_of = cursor_export_as_of
                else:
                    db_now_row = await conn.fetchrow("SELECT now() AS now")
                    db_now = db_now_row["now"]
                    if hasattr(db_now, "isoformat"):
                        export_as_of = db_now.isoformat()
                    else:
                        export_as_of = str(db_now)
                dl_rows = await repo.fetch_deletion_log_for_export(
                    conn,
                    effective_owner=effective_owner,
                    effective_ns=effective_ns,
                    hard_limit=_EXPORT_SIDECAR_HARD_LIMIT,
                    from_executed_at=effective_dl_from,
                    to_executed_at=effective_dl_to,
                    cursor_executed_at=cursor_executed_at,
                    cursor_id=cursor_row_id,
                    export_as_of=export_as_of,
                )
                # Page-aware overflow handling: if we got cap+1 rows, the
                # cap-th row is the cursor for the NEXT page. Slice it
                # off, emit a next_cursor, and DON'T 413 — keyset
                # pagination is the operator's exit from the cap, no
                # narrowing required. This deviates from the other
                # sidecars, which still 413 since they don't have
                # equivalent cursor support yet.
                if len(dl_rows) > _EXPORT_SIDECAR_HARD_LIMIT:
                    last_in_page = dl_rows[_EXPORT_SIDECAR_HARD_LIMIT - 1]
                    last_executed_at = last_in_page.get("executed_at")
                    last_id = last_in_page.get("id")
                    if last_executed_at is None or last_id is None:
                        # Defensive fallback: if the row somehow lacks
                        # executed_at or id, can't build a stable cursor;
                        # raise 413 with the standard message rather than
                        # silently emit a partial page.
                        _enforce_sidecar_cap(dl_rows, "deletion_log")
                    if hasattr(last_executed_at, "isoformat"):
                        last_executed_at = last_executed_at.isoformat()
                    deletion_log_next_cursor = _encode_deletion_log_cursor(
                        str(last_executed_at),
                        str(last_id),
                        export_as_of=export_as_of,
                        deletion_log_from=effective_dl_from,
                        deletion_log_to=effective_dl_to,
                        effective_owner=effective_owner,
                        effective_ns=effective_ns,
                    )
                    dl_rows = dl_rows[:_EXPORT_SIDECAR_HARD_LIMIT]
                # Filter out empty entries (the serializer returns {} for
                # v0.1 emission to be safe — but we're inside the v0.2
                # branch so all should populate). Drop any that lack the
                # required deleted_at field after the executed_at→requested_at
                # fallback.
                deletion_log_out = [
                    entry
                    for entry in (
                        _deletion_log_to_entry(dict(r), mpf_version=emit_version)
                        for r in dl_rows
                    )
                    if entry and entry.get("deleted_at")
                ]

    return MPFEnvelope(
        mpf_version=emit_version,
        source_system=SOURCE_SYSTEM,
        source_version=SOURCE_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        record_count=len(records),
        records=records,
        kg_triples=kg_triples_out,
        memory_versions=memory_versions_out,
        compression_manifest=compression_manifest_out,
        deletion_log=deletion_log_out,
        deletion_log_next_cursor=deletion_log_next_cursor,
    )

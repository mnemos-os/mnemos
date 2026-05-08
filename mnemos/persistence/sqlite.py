"""SQLite persistence backend for the MNEMOS persistence interface.

Requires SQLite 3.35.0 or newer for UPDATE ... RETURNING support.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised when the sqlite extra is installed.
    import aiosqlite
except ImportError:  # pragma: no cover - local CI can run without optional extra.
    aiosqlite = None

from mnemos.core.auth_context import UserContext
from mnemos.core.config import hot_rs_enabled
from mnemos.persistence.base import (
    BranchRepository,
    CompressionRepository,
    CompressionStatsRow,
    ConsultationAuditRepository,
    DuplicateMemoryError,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    MemoryStatsRow,
    PersistenceBackend,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import MEMORY_COLS as _MEMORY_COLS, Row
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core import webhook_constants

logger = logging.getLogger(__name__)

MIN_SQLITE_VERSION = (3, 35, 0)


SQLITE_MIGRATION_FILES = [
    "migrations.sql",
    "migrations_v1_multiuser.sql",
    "migrations_v2_versioning.sql",
    "migrations_v2_sessions.sql",
    "migrations_model_registry.sql",
    "migrations_v3_dag.sql",
    "migrations_v3_graeae_unified.sql",
    "migrations_v3_webhooks.sql",
    "migrations_v3_oauth.sql",
    "migrations_v3_federation.sql",
    "migrations_v3_ownership.sql",
    "migrations_v3_1_compression.sql",
    "migrations_v3_1_versioning_fix.sql",
    "migrations_v3_1_2_kg_tenancy.sql",
    "migrations_v3_1_2_audit_log_columns.sql",
    "migrations_v3_2_user_namespace.sql",
    "migrations_v3_2_entities_namespace.sql",
    "migrations_v3_2_2_version_snapshot_new_values.sql",
    "migrations_v3_3_morpheus.sql",
    "migrations_v3_3_morpheus_namespace.sql",
    "migrations_v3_3_recall_tracking.sql",
    "migrations_charon_trigger_guard.sql",
    "migrations_v3_4_federation_compat.sql",
    "migrations_v3_5_trigger_same_memory_parent.sql",
    "migrations_v3_5_rls_group_select_unix_bits.sql",
    "migrations_v3_5_webhook_retry_terminal_state.sql",
    "migrations_v3_5_webhook_attempt_lease.sql",
    "migrations_v3_5_webhook_writer_revision.sql",
    "migrations_v3_5_webhook_status_updated_at.sql",
    "migrations_v3_5_webhook_superseded_marker.sql",
    "migrations_v3_5_webhook_attempt_unique.sql",
    "migrations_v3_5_webhook_succeeded_unique.sql",
    "migrations_v3_5_webhook_succeeded_terminal_trigger.sql",
    "migrations_v3_5_entities_namespace_unique.sql",
    "migrations_v3_5_state_journal_namespace.sql",
    "migrations_v3_5_session_compression_ratio_drop.sql",
    "migrations_v3_5_session_compression_legacy_drop.sql",
    "migrations_v3_5_sessions_consultations_namespace.sql",
    "migrations_v4_2_compression_candidates_reject_reason.sql",
    "migrations_v4_2_morpheus_consolidate_sqlite.sql",
    "migrations_v4_2_morpheus_extract_sqlite.sql",
    "migrations_v4_2_persephone.sql",
    "migrations_v4_2_pantheon_routing_audit_sqlite.sql",
    "migrations_v5_0_consolidated_at_sqlite.sql",
    "migrations_v5_0_morpheus_extract_run_memories_sqlite.sql",
    "migrations_v5_0_2_artemis_dedup_sqlite.sql",
    "migrations_v5_0_3_timestamp_tz_upgrade_sqlite.sql",
    "migrations_v5_1_0_deletion_log_sqlite.sql",
    "migrations_v5_2_0_nats_outbox_idempotency_sqlite.sql",
    "migrations_v5_3_4_mcp_audit_log_sqlite.sql",
]


def _dict_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json_text(value: Any, *, default: Any = None) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else default)


def _json_array_text(value: Sequence[Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value))


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _in_clause(column: str, values: Sequence[Any], params: list[Any]) -> str:
    if not values:
        return "0"
    params.extend(values)
    return f"{column} IN ({_placeholders(values)})"


def _read_visibility_clause(
    user: UserContext,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    p = f"{table_alias}." if table_alias else ""
    params.append(user.user_id)
    group_ids = list(user.group_ids)
    if group_ids:
        group_clause = f"{p}group_id IN ({_placeholders(group_ids)})"
        params.extend(group_ids)
    else:
        group_clause = "0"
    return (
        "("
        f"{p}owner_id = ?"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        ")"
    )


def _version_visibility_clause(
    user: UserContext,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    p = f"{table_alias}." if table_alias else ""
    params.append(user.user_id)
    return f"({p}owner_id = ? OR ({p}permission_mode % 10) >= 4)"


def _sqlite_memory_cols(table_alias: str = "") -> str:
    """Return ``_MEMORY_COLS``-equivalent SELECT list for SQLite.

    The SQLite ``memories`` table lacks the Postgres-only
    ``compressed_content`` column, so emit ``NULL AS compressed_content``
    in its place. Timestamp columns are normalized to ISO-shaped TEXT
    so handler serialization sees the same wire shape as Postgres
    ``datetime.isoformat()`` output. Other ``_MEMORY_COLS`` columns are
    present on both backends and pass through with the optional
    ``table_alias.`` prefix so the result is safe to JOIN.
    """
    p = f"{table_alias}." if table_alias else ""
    out: list[str] = []
    for raw in _MEMORY_COLS.split(","):
        col = raw.strip()
        if col == "compressed_content":
            out.append("NULL AS compressed_content")
        elif col in {"created", "updated"}:
            out.append(f"replace(datetime({p}{col}), ' ', 'T') AS {col}")
        else:
            out.append(f"{p}{col}")
    return ", ".join(out)


def _render_sqlite_visibility(
    visibility: VisibilityFilter,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    """SQLite analog of ``mnemos.persistence.postgres._render_postgres_visibility``.

    Appends parameters to ``params`` (qmark style — SQLite has no ``$N``)
    and returns the WHERE fragment. Returns an empty string for
    ``ROOT_BYPASS`` with no namespace pin so callers can omit the WHERE
    entirely.

    Mirrors the existing ``_read_visibility_clause`` shape (the
    v1_multiuser RLS read predicate, expanded inline because SQLite has
    no RLS), but takes a backend-neutral ``VisibilityFilter`` so the
    repository surface stays dialect-agnostic.
    """
    p = f"{table_alias}." if table_alias else ""

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return ""
        params.append(visibility.namespace)
        return f"{p}namespace = ?"

    if visibility.namespace is None:
        return "1=0"

    if visibility.scope == VisibilityScope.OWN_ONLY:
        # Mutation path: strict owner_id + namespace.
        clauses: list[str] = [f"{p}owner_id = ?", f"{p}namespace = ?"]
        params.append(visibility.user_id)
        params.append(visibility.namespace)
        return " AND ".join(clauses)

    # READABLE: full v1_multiuser predicate (own / federation / world /
    # group), namespace pin appended after.
    user_id = visibility.user_id or ""
    group_ids = list(visibility.group_ids)
    params.append(user_id)
    if group_ids:
        group_clause = f"{p}group_id IN ({_placeholders(group_ids)})"
        params.extend(group_ids)
    else:
        group_clause = "0"
    clause = (
        "("
        f"{p}owner_id = ?"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        ")"
    )
    clause = f"{clause} AND {p}namespace = ?"
    params.append(visibility.namespace)
    return clause


def _parse_embedding(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part for part in raw.strip("[]").split(",") if part]
    if isinstance(raw, Iterable):
        out: list[float] = []
        for item in raw:
            try:
                out.append(float(item))
            except (TypeError, ValueError):
                return []
        return out
    return []


# Optional Rust hot-path accelerator. Loaded lazily so the absence of
# the wheel on a given build host does NOT break the import — the
# Python implementation below stays the source of truth.
# Opt-in via env var MNEMOS_HOT_RS_ENABLED=1; default off until soak.
_HOT_RS = None
_HOT_RS_ENABLED = hot_rs_enabled()
if _HOT_RS_ENABLED:
    try:
        import mnemos_hot as _HOT_RS  # type: ignore[import-not-found]
        logger.info(
            "mnemos_hot Rust accelerator enabled (cosine UDF will use mnemos_hot %s)",
            getattr(_HOT_RS, "__version__", "?"),
        )
    except ImportError as _exc:
        # Wheel not built for this platform / venv — fall back to the
        # Python implementation. Operator can install via
        # `maturin develop` from /private/tmp/mnemos-hot-rs.
        logger.warning(
            "MNEMOS_HOT_RS_ENABLED=1 but mnemos_hot wheel is not importable: %s. "
            "Falling back to pure-Python cosine UDF.",
            _exc,
        )
        _HOT_RS = None


def _cosine_similarity(left: Any, right: Any) -> float:
    if _HOT_RS is not None:
        # Rust path: ~12× faster on 384-dim batches per
        # /private/tmp/mnemos-hot-rs/bench_vs_python.py. The Rust
        # parse_embedding mirrors the Python semantics 1:1 (None →
        # [], list → float-extract, str → JSON-array hand-parse,
        # length mismatch → 0.0, zero norm → 0.0).
        try:
            a = _HOT_RS.parse_embedding(left)
            b = _HOT_RS.parse_embedding(right)
        except Exception:
            # Defensive: an unexpected input shape (e.g., bytes) would
            # raise ValueError out of pyo3. Fall back to Python.
            a = _parse_embedding(left)
            b = _parse_embedding(right)
        return _HOT_RS.cosine(a, b)
    a = _parse_embedding(left)
    b = _parse_embedding(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _content_hash_for_sqlite(content: Any) -> str:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call(method: Any, *args: Any) -> Any:
    return await _maybe_await(method(*args))


async def _execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    normalized = tuple(_sqlite_value(value) for value in params)
    return await _maybe_await(conn.execute(sql, normalized))


async def _execute_count(conn: Any, sql: str, params: Sequence[Any] = ()) -> int:
    cursor = await _execute(conn, sql, params)
    count = int(getattr(cursor, "rowcount", 0) or 0)
    close = getattr(cursor, "close", None)
    if close is not None:
        await _maybe_await(close())
    return count


# #184: removed `_executemany` — dead. No call sites; SQLite
# multi-row writes go through individual `await conn.execute(...)`
# calls in the migration runner and test helpers. The sibling
# `_executescript` IS still used (migration script application).


async def _executescript(conn: Any, sql: str) -> Any:
    return await _maybe_await(conn.executescript(sql))


async def _fetch_all(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Row]:
    cursor = await _execute(conn, sql, params)
    try:
        rows = await _maybe_await(cursor.fetchall())
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            await _maybe_await(close())
    return list(rows)


async def _fetch_one(conn: Any, sql: str, params: Sequence[Any] = ()) -> Row | None:
    cursor = await _execute(conn, sql, params)
    try:
        row = await _maybe_await(cursor.fetchone())
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            await _maybe_await(close())
    return row


async def _fetch_val(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    row = await _fetch_one(conn, sql, params)
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


async def _commit(conn: Any) -> None:
    await _maybe_await(conn.commit())


async def _rollback(conn: Any) -> None:
    await _maybe_await(conn.rollback())


class SqliteTransaction:
    """Transaction wrapper that keeps SQLite connection details private."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._closed = False

    @property
    def conn(self) -> Any:
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    async def commit(self) -> None:
        if self._closed:
            return
        await _commit(self._conn)
        self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        await _rollback(self._conn)
        self._closed = True


def _sqlite_tx(tx: Transaction) -> SqliteTransaction:
    if not isinstance(tx, SqliteTransaction):
        raise TypeError("SQLite repositories require a SqliteTransaction")
    return tx


class _SqliteRepository:
    @staticmethod
    def _conn(tx: Transaction) -> Any:
        return _sqlite_tx(tx).conn


class SqliteMemoryRepository(_SqliteRepository, MemoryRepository):
    # Set by SqliteBackend on construction so search/upsert paths can
    # enforce the configured embedding dim end-to-end. None disables the
    # check (e.g. tests that bypass the backend).
    _expected_embedding_dim: int | None = None

    def _require_dim(self, embedding: Sequence[float], op: str) -> None:
        """Fail loudly if the embedding length doesn't match the configured dim.

        Without this guard, mnemos_cosine_similarity would return 0.0 on every
        length mismatch, silently degrading search to "rank by recency" and
        letting wrong-dim writes poison the table until the next restart-time
        guard fires. We want loud failure on every call.
        """
        expected = self._expected_embedding_dim
        if expected is None:
            return
        actual = len(embedding)
        if actual != expected:
            raise ValueError(
                f"SQLite embedding dim mismatch on {op}: got {actual}-D vector "
                f"but the configured MNEMOS_EMBEDDING_DIM is {expected}. The "
                f"embedding endpoint may have been switched to a different "
                f"model. Verify `INFERENCE_EMBED_HOST` / model selection and "
                f"either restart with the matching MNEMOS_EMBEDDING_DIM or "
                f"swap the embedding endpoint back to the model the DB was "
                f"sized for."
            )

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        conn = self._conn(tx)
        if _is_root(user):
            row = await _fetch_one(conn, "SELECT 1 FROM memory_versions WHERE memory_id = ? LIMIT 1", (memory_id,))
        else:
            params: list[Any] = [memory_id]
            vis_clause = _read_visibility_clause(user, params)
            params.append(user.namespace)
            row = await _fetch_one(
                conn,
                f"SELECT 1 FROM memories WHERE id = ? AND {vis_clause} AND namespace = ? LIMIT 1",
                params,
            )
        if not row:
            raise PermissionError(f"Memory {memory_id} not found")

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        rows = await _fetch_all(
            self._conn(tx),
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
                    mb.name = ? AND
                    mb.head_version_id = mv.id
                )
                WHERE mv.memory_id = ?
                UNION ALL
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
                WHERE cw.depth < ?
            )
            SELECT
                commit_hash, version_num, branch, category, change_type,
                snapshot_at, snapshot_by, owner_id, namespace, permission_mode
            FROM commit_walk
            ORDER BY depth ASC
            LIMIT ?
            """,
            (branch, memory_id, limit, limit),
        )
        if _is_root(user):
            return rows
        return [
            row for row in rows
            if row["namespace"] == user.namespace
            and (row["owner_id"] == user.user_id or (row["permission_mode"] % 10) >= 4)
        ]

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        conn = self._conn(tx)
        if _is_root(user):
            sql = "SELECT content, version_num FROM memory_versions WHERE memory_id = ? AND commit_hash = ?"
            return (
                await _fetch_one(conn, sql, (memory_id, commit_a)),
                await _fetch_one(conn, sql, (memory_id, commit_b)),
            )
        params_a: list[Any] = [memory_id, commit_a]
        vis_clause = _version_visibility_clause(user, params_a)
        params_a.append(user.namespace)
        sql = (
            "SELECT content, version_num FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?"
        )
        params_b: list[Any] = [memory_id, commit_b]
        vis_clause_b = _version_visibility_clause(user, params_b)
        params_b.append(user.namespace)
        sql_b = (
            "SELECT content, version_num FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause_b} AND namespace = ?"
        )
        return (await _fetch_one(conn, sql, params_a), await _fetch_one(conn, sql_b, params_b))

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        conn = self._conn(tx)
        select = (
            "SELECT commit_hash, version_num, branch, category, subcategory, "
            "content, change_type, snapshot_at, snapshot_by "
            "FROM memory_versions "
        )
        if _is_root(user):
            return await _fetch_one(conn, select + "WHERE memory_id = ? AND commit_hash = ?", (memory_id, commit_hash))
        params: list[Any] = [memory_id, commit_hash]
        vis_clause = _version_visibility_clause(user, params)
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            select + f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?",
            params,
        )

    async def fetch_memory_export(
        self,
        tx: Transaction,
        *,
        effective_owner: str | None,
        effective_ns: str | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        conditions: list[str] = []
        params: list[Any] = []
        if effective_owner:
            conditions.append("owner_id = ?")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = ?")
            params.append(effective_ns)
        if category:
            conditions.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        return await _fetch_all(
            self._conn(tx),
            # Provenance columns for MPF v0.2 emission. SQLite uses
            # `source_memory_ids` and `provenance` (JSON-text) where
            # postgres uses `source_memories` (text[]) and `provenance`
            # (text). The serializer reads either key with fallback —
            # we still alias `provenance` to `prov_kind` so it doesn't
            # collide with the v0.2 record-level `provenance` field
            # name in serializer logic.
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            "metadata, "
            "provenance AS prov_kind, morpheus_run_id, "
            "source_memory_ids, federation_source "
            f"FROM memories {where} ORDER BY created ASC LIMIT ? OFFSET ?",
            params,
        )

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        if not referenced_ids:
            return []
        params: list[Any] = []
        conditions = [_in_clause("id", list(referenced_ids), params)]
        if scope_owner is not None:
            conditions.append("owner_id = ?")
            params.append(scope_owner)
        if scope_namespace is not None:
            conditions.append("namespace = ?")
            params.append(scope_namespace)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT id, owner_id, namespace FROM memories WHERE {' AND '.join(conditions)}",
            params,
        )

    async def insert_memory(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        quality_rating: int,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        verbatim_content: str | None,
        created: Any,
        updated: Any,
    ) -> str:
        inserted = await _fetch_one(
            self._conn(tx),
            """
            INSERT INTO memories (
                id, content, category, subcategory, metadata,
                content_hash, quality_rating, verbatim_content, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                created, updated
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP)
            )
            ON CONFLICT(id) DO NOTHING
            RETURNING id
            """,
            (
                memory_id,
                content,
                category,
                subcategory,
                metadata_json,
                _content_hash_for_sqlite(content),
                quality_rating,
                verbatim_content,
                owner_id,
                namespace,
                permission_mode,
                source_model,
                source_provider,
                source_session,
                source_agent,
                created,
                updated,
            ),
        )
        if inserted is None:
            raise DuplicateMemoryError(f"memory id already exists: {memory_id}")
        return "INSERT 0 1"

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT content, category, subcategory, metadata, quality_rating, owner_id, "
            "namespace, permission_mode, source_model, source_provider, source_session, "
            "source_agent, created, updated FROM memories WHERE id = ?",
            (memory_id,),
        )

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        await _execute(self._conn(tx), "CREATE TEMP TABLE IF NOT EXISTS mnemos_tx_flags (key TEXT PRIMARY KEY)")
        await _execute(self._conn(tx), "INSERT OR IGNORE INTO mnemos_tx_flags(key) VALUES ('suppress_version_snapshot')")

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("memory_id", list(memory_ids), params)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT DISTINCT memory_id FROM memory_versions WHERE {condition}",
            params,
        )

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("m.id", list(memory_ids), params)
        return await _fetch_all(
            self._conn(tx),
            "SELECT m.id, m.content AS memory_content, mv.content AS head_content "
            "FROM memories m "
            "LEFT JOIN memory_branches b ON b.memory_id = m.id AND b.name = 'main' "
            "LEFT JOIN memory_versions mv ON mv.id = b.head_version_id "
            f"WHERE {condition}",
            params,
        )

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        conn = self._conn(tx)
        categories = ("solutions", "patterns", "decisions", "infrastructure")
        category_placeholders = _placeholders(categories)
        like_q = f"%{query}%"
        params: list[Any] = []
        if getattr(user, "role", None) == "root":
            params.extend([like_q, *categories, limit])
            rows = await _fetch_all(
                conn,
                "SELECT m.id, COALESCE(v.compressed_content, m.content) AS content "
                "FROM memories m "
                "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
                f"WHERE lower(m.content) LIKE lower(?) OR m.category IN ({category_placeholders}) "
                "ORDER BY m.updated DESC LIMIT ?",
                params,
            )
        else:
            vis_clause = _read_visibility_clause(user, params, table_alias="m")
            params.extend([user.namespace, like_q, *categories, limit])
            rows = await _fetch_all(
                conn,
                "SELECT m.id, COALESCE(v.compressed_content, m.content) AS content "
                "FROM memories m "
                "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
                f"WHERE {vis_clause} AND m.namespace = ? "
                f"AND (lower(m.content) LIKE lower(?) OR m.category IN ({category_placeholders})) "
                "ORDER BY m.updated DESC LIMIT ?",
                params,
            )
        return [{"id": row["id"], "content": row["content"]} for row in rows]

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        self._require_dim(embedding, "upsert_memory_embedding")
        embedding_json = json.dumps([float(value) for value in embedding])
        conn = self._conn(tx)
        await _execute(conn, "UPDATE memories SET embedding = ? WHERE id = ?", (embedding_json, memory_id))
        await _execute(
            conn,
            "INSERT INTO memory_embeddings(memory_id, embedding) VALUES (?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET embedding = excluded.embedding",
            (memory_id, embedding_json),
        )

    async def semantic_search(
        self,
        tx: Transaction,
        *,
        embedding: Sequence[float],
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
        boost_recency: bool = False,  # noqa: ARG002 - Postgres-only option.
        recency_weight: float = 0.15,  # noqa: ARG002 - Postgres-only option.
    ) -> list[Row]:
        self._require_dim(embedding, "semantic_search")
        embedding_json = json.dumps([float(value) for value in embedding])
        conditions: list[str] = ["me.embedding IS NOT NULL"]
        if not include_archived:
            conditions.append("m.archived_at IS NULL")
        params: list[Any] = [embedding_json]
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                conditions.append(f"m.{col} = ?")
                params.append(val)
        vis_clause = _render_sqlite_visibility(visibility, params, table_alias="m")
        if vis_clause:
            conditions.append(vis_clause)
        params.append(limit)
        # SELECT ``_MEMORY_COLS`` (with the ``m.`` alias) so the row
        # shape matches what the handler's ``row_to_memory`` consumes —
        # parity with PostgresMemoryRepository.semantic_search.
        select_cols = _sqlite_memory_cols("m")
        return await _fetch_all(
            self._conn(tx),
            f"SELECT {select_cols}, "
            "mnemos_cosine_similarity(me.embedding, ?) AS similarity "
            "FROM memory_embeddings me "
            "JOIN memories m ON m.id = me.memory_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY similarity DESC, m.updated DESC LIMIT ?",
            params,
        )

    async def fts_search(
        self,
        tx: Transaction,
        *,
        query: str,
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
    ) -> list[Row]:
        conn = self._conn(tx)
        # FTS path: $1=query (MATCH), filter+visibility params in the
        # middle, $LAST=limit. Mirrors the legacy shape but with the
        # full _MEMORY_COLS row so the handler can pass results
        # straight to row_to_memory.
        params: list[Any] = [query]
        conditions: list[str] = []
        if not include_archived:
            conditions.append("m.archived_at IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                conditions.append(f"m.{col} = ?")
                params.append(val)
        vis_clause = _render_sqlite_visibility(visibility, params, table_alias="m")
        if vis_clause:
            conditions.append(vis_clause)
        where_extra = f" AND {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        select_cols = _sqlite_memory_cols("m")
        try:
            return await _fetch_all(
                conn,
                f"SELECT {select_cols}, bm25(memories_fts) AS rank "
                "FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.id "
                f"WHERE memories_fts MATCH ?{where_extra} "
                "ORDER BY rank ASC, m.updated DESC LIMIT ?",
                params,
            )
        except sqlite3.Error:
            # ILIKE-equivalent fallback when FTS5 isn't available or
            # the query is malformed for tsquery purposes. Same
            # predicate shape, content LIKE instead of MATCH.
            like_params: list[Any] = [f"%{query}%"]
            like_conditions: list[str] = ["lower(m.content) LIKE lower(?)"]
            if not include_archived:
                like_conditions.append("m.archived_at IS NULL")
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    like_conditions.append(f"m.{col} = ?")
                    like_params.append(val)
            like_vis_clause = _render_sqlite_visibility(
                visibility, like_params, table_alias="m",
            )
            if like_vis_clause:
                like_conditions.append(like_vis_clause)
            like_params.append(limit)
            return await _fetch_all(
                conn,
                f"SELECT {select_cols} FROM memories m "
                f"WHERE {' AND '.join(like_conditions)} "
                "ORDER BY m.updated DESC LIMIT ?",
                like_params,
            )

    # --- v4.1 handler-through-backend impls -----------------------------------

    async def list_memories(
        self,
        tx: Transaction,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> tuple[list[Row], int]:
        conn = self._conn(tx)
        where_parts: list[str] = []
        if not include_archived:
            where_parts.append("archived_at IS NULL")
        params: list[Any] = []
        if category is not None:
            where_parts.append("category = ?")
            params.append(category)
        if subcategory is not None:
            where_parts.append("subcategory = ?")
            params.append(subcategory)
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            where_parts.append(vis_clause)
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        select_sql = (
            f"SELECT {_sqlite_memory_cols()} FROM memories{where_sql} "
            "ORDER BY created DESC LIMIT ? OFFSET ?"
        )
        # COUNT(*) first (without limit/offset params), then paged
        # SELECT with the same predicate params plus limit/offset.
        count_sql = f"SELECT COUNT(*) FROM memories{where_sql}"
        total = await _fetch_val(conn, count_sql, params)
        rows = await _fetch_all(conn, select_sql, [*params, limit, offset])
        return rows, int(total or 0)

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = self._conn(tx)
        archived_clause = "" if include_archived else " AND archived_at IS NULL"
        if visibility.scope == VisibilityScope.ROOT_BYPASS and visibility.namespace is None:
            return await _fetch_one(
                conn,
                f"SELECT {_sqlite_memory_cols()} FROM memories WHERE id = ?{archived_clause}",
                (memory_id,),
            )
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        sql = f"SELECT {_sqlite_memory_cols()} FROM memories WHERE id = ?{archived_clause} AND {vis_clause}"
        return await _fetch_one(conn, sql, params)

    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        if not fields:
            return None
        conn = self._conn(tx)
        keys = list(fields.keys())
        set_clauses = [f"{col} = ?" for col in keys]
        values: list[Any] = [fields[k] for k in keys]
        if "content" in fields:
            set_clauses.append("content_hash = ?")
            values.append(_content_hash_for_sqlite(fields["content"]))
        set_clauses.append("updated = CURRENT_TIMESTAMP")
        set_sql = ", ".join(set_clauses)
        # WHERE id=? + visibility predicate. Authorization folded into
        # the same UPDATE/RETURNING — same TOCTOU-safe shape as the
        # Postgres impl.
        params: list[Any] = [*values, memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                f"UPDATE memories SET {set_sql} "
                f"WHERE id = ? AND {vis_clause} "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        else:
            sql = (
                f"UPDATE memories SET {set_sql} WHERE id = ? "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        return await _fetch_one(conn, sql, params)

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conditions = [
            "owner_id = ?",
            "deleted_at IS NULL",
            "archived_at IS NULL",
            "consolidated_into IS NULL",
            "content_hash = ?",
        ]
        params: list[Any] = [owner_id, content_hash]
        if not cross_namespace:
            conditions.insert(1, "namespace = ?")
            params.insert(1, namespace)
        return await _fetch_one(
            self._conn(tx),
            "SELECT id, last_recalled_at FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY created ASC, id ASC LIMIT 1",
            params,
        )

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = self._conn(tx)
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP "
                f"WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL AND {vis_clause} "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        else:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        return await _fetch_one(conn, sql, params)

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        params: list[Any] = []
        namespace_clause = ""
        if namespace is not None:
            namespace_clause = "AND namespace = ?"
            params.append(namespace)
        rows = await _fetch_all(
            self._conn(tx),
            """
            SELECT
                owner_id,
                namespace,
                content_hash,
                COUNT(*) AS duplicate_count,
                GROUP_CONCAT(id, char(31)) AS memory_ids,
                substr(GROUP_CONCAT(id, char(31)), 1, instr(GROUP_CONCAT(id, char(31)) || char(31), char(31)) - 1)
                    AS canonical_id
            FROM (
                SELECT id, owner_id, namespace, content_hash, created
                FROM memories
                WHERE deleted_at IS NULL
                  AND archived_at IS NULL
                  AND consolidated_into IS NULL
                  AND content_hash IS NOT NULL
                  {namespace_clause}
                ORDER BY owner_id ASC, namespace ASC, content_hash ASC, created ASC, id ASC
            )
            GROUP BY owner_id, namespace, content_hash
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
            """.format(namespace_clause=namespace_clause),
            params,
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            raw_ids = values.get("memory_ids") or ""
            values["memory_ids"] = [part for part in str(raw_ids).split("\x1f") if part]
            values["duplicate_count"] = int(values.get("duplicate_count") or 0)
            normalized.append(values)
        return normalized

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        params: list[Any] = [canonical_id, *duplicate_ids, canonical_id, canonical_id]
        placeholders = _placeholders(duplicate_ids)
        return await _execute_count(
            self._conn(tx),
            f"""
            UPDATE memories
            SET consolidated_into = ?,
                consolidated_at = CURRENT_TIMESTAMP,
                deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP),
                updated = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
              AND id <> ?
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = ?
                    AND deleted_at IS NULL
                    AND archived_at IS NULL
                    AND consolidated_into IS NULL
              )
            """,
            params,
        )

    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        requested_by: str | None = None,
        requested_at: Any = None,
        request_kind: str = "admin_purge",
        reason: str | None = None,
        source: Sequence[str] | None = None,
    ) -> Row | None:
        conn = self._conn(tx)
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                "DELETE FROM memories "
                f"WHERE id = ? AND {vis_clause} "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
        else:
            sql = (
                "DELETE FROM memories WHERE id = ? "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
        return await _fetch_one(conn, sql, params)

    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        conn = self._conn(tx)
        total = await _fetch_val(conn, "SELECT COUNT(*) FROM memories")
        native = await _fetch_val(
            conn, "SELECT COUNT(*) FROM memories WHERE federation_source IS NULL",
        )
        federated = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL",
        )
        peer_rows = await _fetch_all(
            conn,
            "SELECT federation_source, COUNT(*) AS cnt FROM memories "
            "WHERE federation_source IS NOT NULL "
            "GROUP BY federation_source ORDER BY cnt DESC",
        )
        cat_rows = await _fetch_all(
            conn,
            "SELECT category, COUNT(*) AS cnt FROM memories GROUP BY category",
        )
        sub_rows = await _fetch_all(
            conn,
            "SELECT category, subcategory, COUNT(*) AS cnt FROM memories "
            "WHERE subcategory IS NOT NULL "
            "GROUP BY category, subcategory ORDER BY cnt DESC",
        )
        avg_quality = await _fetch_val(
            conn,
            "SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL",
        )
        memories_by_subcategory: dict[str, dict[str, int]] = {}
        for r in sub_rows:
            memories_by_subcategory.setdefault(r["category"], {})[r["subcategory"]] = r["cnt"]
        return MemoryStatsRow(
            total_memories=int(total or 0),
            native_memories=int(native or 0),
            federated_memories=int(federated or 0),
            memories_by_peer={r["federation_source"]: r["cnt"] for r in peer_rows},
            memories_by_category={r["category"]: r["cnt"] for r in cat_rows},
            memories_by_subcategory=memories_by_subcategory,
            avg_quality_rating=float(avg_quality) if avg_quality is not None else None,
        )


class SqliteKGRepository(_SqliteRepository, KGRepository):
    async def fetch_kg_triples_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        include_unattached: bool,
        hard_limit: int,
    ) -> list[Row]:
        conditions: list[str] = []
        params: list[Any] = []
        if memory_ids:
            memory_condition = _in_clause("memory_id", list(memory_ids), params)
            if include_unattached:
                conditions.append(f"(memory_id IS NULL OR {memory_condition})")
            else:
                conditions.append(memory_condition)
        elif include_unattached:
            conditions.append("memory_id IS NULL")
        else:
            return []
        if effective_owner:
            conditions.append("owner_id = ?")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = ?")
            params.append(effective_ns)
        params.append(hard_limit + 1)
        return await _fetch_all(
            self._conn(tx),
            "SELECT id, subject, predicate, object, subject_type, object_type, "
            "valid_from, valid_until, memory_id, confidence, created, owner_id, namespace "
            f"FROM kg_triples WHERE {' AND '.join(conditions)} LIMIT ?",
            params,
        )

    async def insert_kg_triple(
        self,
        tx: Transaction,
        *,
        triple_id: str,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str | None,
        object_type: str | None,
        valid_from: Any,
        valid_until: Any,
        memory_id: str | None,
        confidence: float | None,
        created: Any,
        owner_id: str,
        namespace: str | None,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO kg_triples (
                id, subject, predicate, object,
                subject_type, object_type,
                valid_from, valid_until,
                memory_id, confidence, created,
                owner_id, namespace
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), ?,
                ?, COALESCE(?, 1.0),
                COALESCE(?, CURRENT_TIMESTAMP),
                ?, COALESCE(?, 'default')
            )
            """,
            (
                triple_id,
                subject,
                predicate,
                obj,
                subject_type,
                object_type,
                valid_from,
                valid_until,
                memory_id,
                confidence,
                created,
                owner_id,
                namespace,
            ),
        )
        return "INSERT 0 1"

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT subject, predicate, object, subject_type, object_type, memory_id, "
            "confidence, owner_id, namespace, valid_from, valid_until, created "
            "FROM kg_triples WHERE id = ?",
            (triple_id,),
        )

    async def search_triples(
        self,
        tx: Transaction,
        query: str,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
        limit: int = 20,
    ) -> list[Row]:
        params: list[Any] = [f"%{query}%", f"%{query}%", f"%{query}%"]
        conditions = [
            "(lower(subject) LIKE lower(?) OR lower(predicate) LIKE lower(?) OR lower(object) LIKE lower(?))"
        ]
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = ?")
            params.append(namespace)
        params.append(limit)
        return await _fetch_all(
            self._conn(tx),
            "SELECT * FROM kg_triples "
            f"WHERE {' AND '.join(conditions)} ORDER BY valid_from ASC, created ASC LIMIT ?",
            params,
        )


async def _fetch_sidecar(
    conn: Any,
    *,
    table: str,
    columns: str,
    memory_id_column: str,
    memory_ids: Sequence[str],
    effective_owner: str | None,
    effective_ns: str | None,
    bound_to_memories: bool,
    hard_limit: int,
    order_by: str | None = None,
) -> list[Row]:
    conditions: list[str] = []
    params: list[Any] = []
    if bound_to_memories:
        if not memory_ids:
            return []
        conditions.append(_in_clause(memory_id_column, list(memory_ids), params))
    if effective_owner:
        conditions.append("owner_id = ?")
        params.append(effective_owner)
    if effective_ns:
        conditions.append("namespace = ?")
        params.append(effective_ns)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = f"ORDER BY {order_by}" if order_by else ""
    params.append(hard_limit + 1)
    return await _fetch_all(conn, f"SELECT {columns} FROM {table} {where} {order} LIMIT ?", params)


class SqliteVersionRepository(_SqliteRepository, VersionRepository):
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await _fetch_sidecar(
            self._conn(tx),
            table="memory_versions",
            columns=(
                "id, memory_id, version_num, content, category, "
                "subcategory, metadata, verbatim_content, owner_id, "
                "namespace, permission_mode, source_model, source_provider, "
                "source_session, source_agent, snapshot_at, snapshot_by, "
                "change_type, commit_hash, parent_version_id, branch, merge_parents"
            ),
            memory_id_column="memory_id",
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            bound_to_memories=True,
            hard_limit=hard_limit,
            order_by="memory_id ASC, branch ASC, version_num ASC",
        )

    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        if not version_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("id", list(version_ids), params)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT id, memory_id, owner_id, namespace FROM memory_versions WHERE {condition}",
            params,
        )

    async def insert_memory_version(
        self,
        tx: Transaction,
        *,
        version_id: str,
        memory_id: str,
        version_num: int,
        content: str,
        category: str | None,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str | None,
        owner_id: str,
        namespace: str | None,
        permission_mode: int | None,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        snapshot_at: Any,
        snapshot_by: str | None,
        change_type: str | None,
        commit_hash: str | None,
        parent_version_id: str | None,
        branch: str | None,
        merge_parents: Any,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO memory_versions (
                id, memory_id, version_num, content,
                category, subcategory, metadata, verbatim_content,
                owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                snapshot_at, snapshot_by, change_type,
                commit_hash, parent_version_id, branch, merge_parents
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, COALESCE(?, 600),
                ?, ?, ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), ?, COALESCE(?, 'create'),
                ?, ?, COALESCE(?, 'main'), ?
            )
            """,
            (
                version_id,
                memory_id,
                version_num,
                content,
                category,
                subcategory,
                metadata_json,
                verbatim_content,
                owner_id,
                namespace,
                permission_mode,
                source_model,
                source_provider,
                source_session,
                source_agent,
                snapshot_at,
                snapshot_by,
                change_type,
                commit_hash,
                parent_version_id,
                branch,
                _json_text(merge_parents, default=[]),
            ),
        )
        return "INSERT 0 1"

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT memory_id, owner_id, namespace, version_num, content, commit_hash, "
            "parent_version_id, branch, merge_parents, category, subcategory, metadata, "
            "verbatim_content, permission_mode, source_model, source_provider, source_session, "
            "source_agent, snapshot_at, snapshot_by, change_type "
            "FROM memory_versions WHERE id = ?",
            (version_id,),
        )


class SqliteBranchRepository(_SqliteRepository, BranchRepository):
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]:
        conn = self._conn(tx)
        if _is_root(user):
            live = await _fetch_one(conn, "SELECT 1 FROM memories WHERE id = ?", (memory_id,))
        else:
            live = await _fetch_one(
                conn,
                "SELECT 1 FROM memories WHERE id = ? AND owner_id = ? AND namespace = ?",
                (memory_id, user.user_id, user.namespace),
            )
        if not live:
            return {"success": False, "error": f"Memory {memory_id} not found"}

        if from_commit:
            start = await self._fetch_branch_start_by_commit(conn, memory_id, from_commit, user)
            if not start:
                return {"success": False, "error": "Commit not found"}
        else:
            start = await self._fetch_main_branch_start(conn, memory_id, user)
            if not start:
                return {"success": False, "error": "main branch not found"}

        await _execute(
            conn,
            "INSERT OR IGNORE INTO memory_branches (memory_id, name, head_version_id, created_by) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, name, start["id"], user.user_id),
        )
        existing = await self._fetch_existing_branch(conn, memory_id, name, user)
        if existing is None:
            return {
                "success": False,
                "error": (
                    "branch exists but its head is not visible or points at a foreign memory version; "
                    "reconciliation required"
                ),
            }
        if existing["head_version_id"] == start["id"]:
            return {
                "success": True,
                "memory_id": memory_id,
                "branch": name,
                "commit_hash": existing["commit_hash"],
                "created_by": user.user_id,
                "idempotent": existing["head_version_id"] != start["id"],
            }
        return {
            "success": False,
            "error": f"branch '{name}' already exists at a different head; refusing to silently move it",
        }

    async def _fetch_branch_start_by_commit(
        self,
        conn: Any,
        memory_id: str,
        from_commit: str,
        user: UserContext,
    ) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT id, commit_hash FROM memory_versions WHERE memory_id = ? AND commit_hash = ?",
                (memory_id, from_commit),
            )
        params: list[Any] = [memory_id, from_commit]
        vis_clause = _version_visibility_clause(user, params)
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT id, commit_hash FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?",
            params,
        )

    async def _fetch_main_branch_start(self, conn: Any, memory_id: str, user: UserContext) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT mv.id, mv.commit_hash FROM memory_versions mv "
                "INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id "
                "WHERE mv.memory_id = ? AND mb.name = 'main'",
                (memory_id,),
            )
        params: list[Any] = [memory_id]
        vis_clause = _version_visibility_clause(user, params, table_alias="mv")
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT mv.id, mv.commit_hash FROM memory_versions mv "
            "INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id "
            f"WHERE mv.memory_id = ? AND mb.name = 'main' AND {vis_clause} AND mv.namespace = ?",
            params,
        )

    async def _fetch_existing_branch(
        self,
        conn: Any,
        memory_id: str,
        name: str,
        user: UserContext,
    ) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT mb.head_version_id, mv.commit_hash FROM memory_branches mb "
                "INNER JOIN memory_versions mv ON mv.id = mb.head_version_id AND mv.memory_id = mb.memory_id "
                "WHERE mb.memory_id = ? AND mb.name = ?",
                (memory_id, name),
            )
        params: list[Any] = [memory_id, name]
        vis_clause = _version_visibility_clause(user, params, table_alias="mv")
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT mb.head_version_id, mv.commit_hash FROM memory_branches mb "
            "INNER JOIN memory_versions mv ON mv.id = mb.head_version_id AND mv.memory_id = mb.memory_id "
            f"AND {vis_clause} AND mv.namespace = ? "
            "WHERE mb.memory_id = ? AND mb.name = ?",
            params[2:] + params[:2],
        )

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return
        params: list[Any] = []
        condition = _in_clause("memory_id", list(memory_ids), params)
        await _execute(self._conn(tx), f"DELETE FROM memory_branches WHERE {condition}", params)

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        conditions = [_in_clause("memory_id", list(memory_ids), params)]
        if authorized_version_uuids is not None:
            conditions.append(_in_clause("id", list(authorized_version_uuids), params))
        return await _fetch_all(
            self._conn(tx),
            "SELECT memory_id, branch, id AS head_version_id "
            "FROM ("
            "  SELECT memory_id, branch, id, version_num, "
            "         ROW_NUMBER() OVER (PARTITION BY memory_id, branch ORDER BY version_num DESC) AS rn "
            "  FROM memory_versions "
            f"  WHERE {' AND '.join(conditions)}"
            ") ranked WHERE rn = 1",
            params,
        )

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(memory_id, name) DO UPDATE SET head_version_id = excluded.head_version_id",
            (memory_id, branch, head_version_id),
        )


class SqliteCompressionRepository(_SqliteRepository, CompressionRepository):
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await _fetch_sidecar(
            self._conn(tx),
            table="memory_compressed_variants",
            columns=(
                "memory_id, owner_id, winner_candidate_id, engine_id, engine_version, "
                "compressed_content, compressed_tokens, compression_ratio, quality_score, "
                "composite_score, scoring_profile, judge_model, selected_at"
            ),
            memory_id_column="memory_id",
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=None,
            bound_to_memories=True,
            hard_limit=hard_limit,
        )

    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        exists = await _fetch_val(
            self._conn(tx),
            "SELECT 1 FROM memory_compression_candidates WHERE id = ? AND memory_id = ? AND owner_id = ?",
            (candidate_id, memory_id, owner_id),
        )
        return bool(exists)

    async def insert_compressed_variant(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        owner_id: str,
        winner_candidate_id: str | None,
        engine_id: str,
        engine_version: str | None,
        compressed_content: str | None,
        compressed_tokens: int | None,
        compression_ratio: float | None,
        quality_score: float | None,
        composite_score: float | None,
        scoring_profile: str | None,
        judge_model: str | None,
        selected_at: Any,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO memory_compressed_variants (
                memory_id, owner_id, winner_candidate_id,
                engine_id, engine_version, compressed_content,
                compressed_tokens, compression_ratio,
                quality_score, composite_score,
                scoring_profile, judge_model, selected_at
            )
            VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                COALESCE(?, 'balanced'), ?,
                COALESCE(?, CURRENT_TIMESTAMP)
            )
            """,
            (
                memory_id,
                owner_id,
                winner_candidate_id,
                engine_id,
                engine_version,
                compressed_content,
                compressed_tokens,
                compression_ratio,
                quality_score,
                composite_score,
                scoring_profile,
                judge_model,
                selected_at,
            ),
        )
        return "INSERT 0 1"

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT owner_id, winner_candidate_id, engine_id, engine_version, compressed_content, "
            "compressed_tokens, compression_ratio, quality_score, composite_score, scoring_profile, "
            "judge_model, selected_at FROM memory_compressed_variants WHERE memory_id = ?",
            (memory_id,),
        )

    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        conn = self._conn(tx)
        total = await _fetch_val(
            conn, "SELECT COUNT(*) FROM memory_compressed_variants",
        )
        avg_ratio = await _fetch_val(
            conn, "SELECT AVG(compression_ratio) FROM memory_compressed_variants",
        )
        unreviewed = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memory_compressed_variants "
            "WHERE quality_score IS NULL",
        )
        return CompressionStatsRow(
            total_compressions=int(total or 0),
            average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
            unreviewed_compressions=int(unreviewed or 0),
        )


class SqliteWebhookRepository(_SqliteRepository, WebhookRepository):
    async def insert_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str | None = None,
        url: str,
        events: Sequence[str],
        secret: str | None = None,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> str:
        subscription_id = subscription_id or str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            "INSERT INTO webhook_subscriptions (id, url, events, secret, owner_id, namespace) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (subscription_id, url, json.dumps(list(events)), secret, owner_id, namespace),
        )
        return subscription_id

    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        conn = self._conn(tx)
        conditions = ["revoked = 0"]
        params: list[Any] = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = ?")
            params.append(namespace)
        subscriptions = await _fetch_all(
            conn,
            f"SELECT id, events, url, owner_id, namespace FROM webhook_subscriptions WHERE {' AND '.join(conditions)}",
            params,
        )
        body = json.dumps(
            {"event": event_type, "timestamp": _now_iso(), "data": payload},
            separators=(",", ":"),
            sort_keys=True,
        )
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_ids: list[str] = []
        for sub in subscriptions:
            if event_type not in _json_list(sub["events"]):
                continue
            delivery_id = str(uuid.uuid4())
            await _execute(
                conn,
                "INSERT INTO webhook_deliveries "
                "(id, subscription_id, event_type, payload, payload_hash, status, writer_revision) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    delivery_id,
                    sub["id"],
                    event_type,
                    body,
                    body_hash,
                    webhook_constants.NEW_CODE_WRITER_REVISION,
                ),
            )
            from mnemos.nats.webhook_events import publish_delivery_queued
            await publish_delivery_queued(
                delivery_id=delivery_id,
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
            delivery_ids.append(delivery_id)
        return delivery_ids

    async def fetch_deliveries(self, tx: Transaction, subscription_id: str | None = None) -> list[Row]:
        if subscription_id is None:
            return await _fetch_all(self._conn(tx), "SELECT * FROM webhook_deliveries ORDER BY created_at ASC")
        return await _fetch_all(
            self._conn(tx),
            "SELECT * FROM webhook_deliveries WHERE subscription_id = ? ORDER BY created_at ASC",
            (subscription_id,),
        )


class SqliteConsultationAuditRepository(_SqliteRepository, ConsultationAuditRepository):
    async def fetch_recommended_model(
        self,
        tx: Transaction,
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
        rows = await _fetch_all(
            self._conn(tx),
            "SELECT provider, model_id, display_name, input_cost_per_mtok, output_cost_per_mtok, "
            "capabilities, graeae_weight, context_window "
            "FROM model_registry WHERE available = 1 AND deprecated = 0",
        )

        def _has_priced(row: Row) -> bool:
            return (
                row["input_cost_per_mtok"] is not None
                and row["output_cost_per_mtok"] is not None
            )

        def _avg_cost_or_none(row: Row) -> float | None:
            if not _has_priced(row):
                return None
            return (
                float(row["input_cost_per_mtok"]) + float(row["output_cost_per_mtok"])
            ) / 2.0

        # Budget tier EXCLUDES rows with NULL costs — an unknown
        # cost cannot legally satisfy a "<= budget" constraint;
        # treating NULL as 0 would let partially-synced rows rank
        # ahead of priced ones. Mirrors the Postgres invariant in
        # mnemos/db/mcp_repo.py and friends.
        eligible = [
            row for row in rows
            if float(row["graeae_weight"] or 0) >= quality_floor
            and _has_priced(row)
            and (_avg_cost_or_none(row) or 0.0) <= cost_budget
            and all(cap in _json_list(row["capabilities"]) for cap in required_caps)
        ]
        chosen_rows = sorted(eligible, key=lambda r: _avg_cost_or_none(r) or 0.0)
        if not chosen_rows:
            # Degraded fallback: no priced model met the budget.
            # Allow NULL-cost rows here but sort priced ones first
            # via "(unknown=infinity)" key — matches PG's NULLS LAST.
            chosen_rows = sorted(
                rows,
                key=lambda r: (
                    _avg_cost_or_none(r) if _avg_cost_or_none(r) is not None else float("inf")
                ),
            )
        if not chosen_rows:
            return None, required_caps
        model = chosen_rows[0]
        return {
            "provider": model["provider"],
            "model_id": model["model_id"],
            "display_name": model["display_name"],
            # cost_per_mtok is None when either cost column is NULL.
            # Surface unknown honestly rather than fabricate 0.0
            # which would silently lie about pricing semantics.
            "cost_per_mtok": _avg_cost_or_none(model),
            "quality_score": float(model["graeae_weight"] or 0),
            "context_window": model["context_window"],
        }, required_caps

    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        model, _required = await self.fetch_recommended_model(tx, task_type, cost_budget, quality_floor)
        return model

    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0",
            (model,),
        )
        if row is not None:
            return row["provider"]
        if "/" not in model:
            return None
        head, tail = model.split("/", 1)
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE provider = ? AND model_id = ? "
            "AND available = 1 AND deprecated = 0",
            (head, tail),
        )
        return row["provider"] if row is not None else None

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            "SELECT provider, model_id, display_name FROM model_registry "
            "WHERE available = 1 AND deprecated = 0 "
            "ORDER BY graeae_weight IS NULL, graeae_weight DESC, model_id ASC",
        )

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0 LIMIT 1",
            (model_id,),
        )
        return row["provider"] if row is not None else None


class SqliteFederationRepository(_SqliteRepository, FederationRepository):
    _ALLOWED_PEER_COLS = {
        "name",
        "base_url",
        "auth_token",
        "namespace_filter",
        "category_filter",
        "enabled",
        "sync_interval_secs",
        "compat_mode",
    }

    @staticmethod
    def _peer_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["enabled"] = bool(out.get("enabled"))
        out["namespace_filter"] = _json_list(out.get("namespace_filter")) or None
        out["category_filter"] = _json_list(out.get("category_filter")) or None
        out["created"] = out.get("created") or out.get("created_at")
        out["updated"] = out.get("updated") or out.get("updated_at")
        out["last_sync_cursor"] = out.get("last_sync_cursor") or out.get("cursor_updated")
        return out

    def _conn(self, tx: Transaction) -> Any:
        return super()._conn(tx)

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: str | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        params: list[Any] = []
        where = ""
        if updated_after is not None and id_after is not None:
            where = "WHERE updated > ? OR (updated = ? AND id > ?)"
            params.extend([updated_after, updated_after, id_after])
        params.append(limit)
        return await _fetch_all(
            self._conn(tx),
            "SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated "
            f"FROM memories {where} ORDER BY updated ASC, id ASC LIMIT ?",
            params,
        )

    async def create_peer(
        self,
        tx: Transaction,
        *,
        name: str,
        base_url: str,
        auth_token: str,
        namespace_filter: Sequence[str] | None,
        category_filter: Sequence[str] | None,
        enabled: bool,
        sync_interval_secs: int,
        compat_mode: str,
    ) -> Row:
        peer_id = str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            """
            INSERT INTO federation_peers
              (id, name, base_url, auth_token, api_key, namespace_filter,
               category_filter, enabled, sync_interval_secs, compat_mode,
               created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                peer_id,
                name,
                base_url,
                auth_token,
                auth_token,
                _json_array_text(namespace_filter),
                _json_array_text(category_filter),
                int(enabled),
                sync_interval_secs,
                compat_mode,
            ),
        )
        row = await self.get_peer(tx, peer_id)
        assert row is not None
        return row

    async def list_peers(self, tx: Transaction) -> list[Row]:
        rows = await _fetch_all(self._conn(tx), "SELECT * FROM federation_peers ORDER BY name")
        return [self._peer_row(row) for row in rows]  # type: ignore[list-item]

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return self._peer_row(await _fetch_one(
            self._conn(tx),
            "SELECT * FROM federation_peers WHERE id = ?",
            (peer_id,),
        ))

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        assignments: list[str] = []
        params: list[Any] = []
        for col, value in updates.items():
            assignments.append(f"{col} = ?")
            if col in {"namespace_filter", "category_filter"}:
                params.append(_json_array_text(value))
            elif col == "enabled":
                params.append(int(bool(value)))
            else:
                params.append(value)
        assignments.append("updated = CURRENT_TIMESTAMP")
        params.append(peer_id)
        await _execute(
            self._conn(tx),
            f"UPDATE federation_peers SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        return await self.get_peer(tx, peer_id)

    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        await _execute(
            self._conn(tx),
            "INSERT INTO federation_peers (id, base_url, name, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url, "
            "name = excluded.name, enabled = excluded.enabled",
            (peer_id, base_url, name, int(enabled)),
        )

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        return await _execute_count(
            self._conn(tx),
            "DELETE FROM federation_peers WHERE id = ?",
            (peer_id,),
        ) > 0

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            SELECT id, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (peer_id, limit),
        )

    async def feed_query(
        self,
        tx: Transaction,
        *,
        since_updated: Any | None,
        since_id: str | None,
        namespaces: Sequence[str],
        categories: Sequence[str],
        limit: int,
        prefer_compressed: bool,
    ) -> list[Row]:
        if prefer_compressed:
            raise NotImplementedError(
                "SQLite federation feed does not support prefer_compressed; "
                "use the Postgres server profile for compressed federation feeds."
            )
        memory_query_parts = [
            "m.federation_source IS NULL",
            "(m.permission_mode % 10) >= 4",
            "m.archived_at IS NULL",
            "m.consolidated_into IS NULL",
        ]
        tombstone_query_parts = [
            "m.federation_source IS NULL",
            "m.consolidated_into IS NOT NULL",
            "m.consolidated_at IS NOT NULL",
        ]
        memory_params: list[Any] = []
        tombstone_params: list[Any] = []
        if since_updated is not None:
            memory_query_parts.append("(m.updated > ? OR (m.updated = ? AND m.id > ?))")
            memory_params.extend([since_updated, since_updated, since_id])
            tombstone_query_parts.append("(m.consolidated_at > ? OR (m.consolidated_at = ? AND m.id > ?))")
            tombstone_params.extend([since_updated, since_updated, since_id])
        if namespaces:
            placeholders = _placeholders(namespaces)
            memory_query_parts.append(f"m.namespace IN ({placeholders})")
            tombstone_query_parts.append(f"m.namespace IN ({placeholders})")
            memory_params.extend(namespaces)
            tombstone_params.extend(namespaces)
        if categories:
            placeholders = _placeholders(categories)
            memory_query_parts.append(f"m.category IN ({placeholders})")
            tombstone_query_parts.append(f"m.category IN ({placeholders})")
            memory_params.extend(categories)
            tombstone_params.extend(categories)

        memory_where_clause = " AND ".join(memory_query_parts)
        tombstone_where_clause = " AND ".join(tombstone_query_parts)
        return await _fetch_all(
            self._conn(tx),
            f"""
            SELECT *
            FROM (
                SELECT NULL AS type,
                       m.id,
                       m.content,
                       m.category,
                       m.subcategory,
                       m.metadata,
                       m.quality_rating,
                       m.verbatim_content,
                       m.owner_id,
                       m.namespace,
                       m.permission_mode,
                       m.source_model,
                       m.source_provider,
                       m.source_session,
                       m.source_agent,
                       m.created,
                       m.updated,
                       m.archived_at,
                       NULL AS consolidated_into,
                       NULL AS consolidated_at,
                       NULL AS compressed_content
                FROM memories m
                WHERE {memory_where_clause}

                UNION ALL

                SELECT 'consolidation' AS type,
                       m.id,
                       NULL AS content,
                       NULL AS category,
                       NULL AS subcategory,
                       NULL AS metadata,
                       NULL AS quality_rating,
                       NULL AS verbatim_content,
                       NULL AS owner_id,
                       m.namespace,
                       NULL AS permission_mode,
                       NULL AS source_model,
                       NULL AS source_provider,
                       NULL AS source_session,
                       NULL AS source_agent,
                       m.created,
                       m.consolidated_at AS updated,
                       NULL AS archived_at,
                       m.consolidated_into,
                       m.consolidated_at,
                       NULL AS compressed_content
                FROM memories m
                WHERE {tombstone_where_clause}
            ) feed
            ORDER BY updated ASC, id ASC
            LIMIT ?
            """,
            [*memory_params, *tombstone_params, limit],
        )

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        query_parts = [
            "m.federation_source IS NULL",
            "(m.permission_mode % 10) >= 4",
            "m.archived_at IS NULL",
            "m.consolidated_into IS NULL",
            "m.id = ?",
        ]
        params: list[Any] = [memory_id]
        if namespaces:
            query_parts.append(f"m.namespace IN ({_placeholders(namespaces)})")
            params.extend(namespaces)
        if categories:
            query_parts.append(f"m.category IN ({_placeholders(categories)})")
            params.extend(categories)
        return await _fetch_one(
            self._conn(tx),
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated, archived_at
            FROM memories m
            WHERE {' AND '.join(query_parts)}
            """,
            params,
        )

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return self._peer_row(await _fetch_one(
            self._conn(tx),
            """
            SELECT id, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor,
                   compat_mode
            FROM federation_peers WHERE id = ?
            """,
            (peer_id,),
        ))

    async def update_peer_schema_check(self, tx: Transaction, peer_id: str, peer_version: str | None) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET peer_mnemos_version = ?, last_schema_check_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (peer_version, peer_id),
        )

    async def record_schema_abort(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        peer_version: str | None,
        cursor_before: Any,
        error: str,
        is_transient: bool,
    ) -> None:
        await self.update_peer_schema_check(tx, peer_id, peer_version)
        log_id = await self.create_sync_log(tx, peer_id, cursor_before)
        await self.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=0,
            memories_new=0,
            memories_updated=0,
            error=error,
            cursor_after=cursor_before,
        )
        if is_transient:
            await _execute(
                self._conn(tx),
                """
                UPDATE federation_peers
                SET last_sync_at = datetime(
                        CURRENT_TIMESTAMP,
                        printf('-%d seconds', sync_interval_secs),
                        '+60 seconds'
                    ),
                    last_error = ?,
                    last_error_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, peer_id),
            )
        else:
            await self.record_sync_error(tx, peer_id, error)

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        log_id = str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            """
            INSERT INTO federation_sync_log
              (id, peer_id, direction, status, started_at, cursor_before)
            VALUES (?, ?, 'pull', 'started', CURRENT_TIMESTAMP, ?)
            """,
            (log_id, peer_id, cursor_before),
        )
        return log_id

    async def finish_sync_log(
        self,
        tx: Transaction,
        *,
        log_id: Any,
        memories_pulled: int,
        memories_new: int,
        memories_updated: int,
        error: str | None,
        cursor_after: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_sync_log
            SET finished_at = CURRENT_TIMESTAMP,
                memories_pulled = ?,
                memories_new = ?,
                memories_updated = ?,
                records_seen = ?,
                records_written = ?,
                status = ?,
                error = ?,
                cursor_after = ?
            WHERE id = ?
            """,
            (
                memories_pulled,
                memories_new,
                memories_updated,
                memories_pulled,
                memories_new + memories_updated,
                "error" if error else "ok",
                error,
                cursor_after,
                str(log_id),
            ),
        )

    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET last_sync_at = CURRENT_TIMESTAMP,
                last_error = ?,
                last_error_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, peer_id),
        )

    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET last_sync_at = CURRENT_TIMESTAMP,
                last_sync_cursor = ?,
                cursor_updated = ?,
                last_error = NULL,
                last_error_at = NULL,
                total_pulled = total_pulled + ?
            WHERE id = ?
            """,
            (cursor, cursor, total_pulled, peer_id),
        )

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            SELECT id, name, sync_interval_secs, last_sync_at
            FROM federation_peers
            WHERE enabled = 1
              AND (
                last_sync_at IS NULL
                OR datetime(last_sync_at, printf('+%d seconds', sync_interval_secs)) <= CURRENT_TIMESTAMP
              )
            ORDER BY COALESCE(
                datetime(last_sync_at, printf('+%d seconds', sync_interval_secs)),
                '1970-01-01T00:00:00'
            )
            LIMIT ?
            """,
            (limit,),
        )

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT federation_remote_updated FROM memories WHERE id = ?",
            (local_id,),
        )

    async def insert_federated_memory(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        peer_name: str,
        remote_updated: Any,
    ) -> bool:
        try:
            await _execute(
                self._conn(tx),
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'federation', ?, 644,
                        ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    local_id,
                    content,
                    category,
                    subcategory,
                    metadata_json,
                    verbatim_content,
                    quality_rating,
                    namespace,
                    source_model,
                    source_provider,
                    source_session,
                    source_agent,
                    peer_name,
                    remote_updated,
                    remote_updated,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    async def update_federated_memory_if_newer(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        remote_updated: Any,
    ) -> bool:
        return await _execute_count(
            self._conn(tx),
            """
            UPDATE memories SET
              content = ?,
              category = ?,
              subcategory = ?,
              metadata = ?,
              verbatim_content = ?,
              quality_rating = ?,
              namespace = ?,
              federation_remote_updated = ?,
              updated = ?
            WHERE id = ?
              AND (
                  federation_remote_updated IS NULL
                  OR federation_remote_updated < ?
              )
            """,
            (
                content,
                category,
                subcategory,
                metadata_json,
                verbatim_content,
                quality_rating,
                namespace,
                remote_updated,
                remote_updated,
                local_id,
                remote_updated,
            ),
        ) > 0

    async def apply_consolidation_tombstone(
        self,
        tx: Transaction,
        *,
        local_id: str,
        local_canonical_id: str,
        consolidated_at: Any,
        remote_id: str,
        canonical_remote_id: str,
        peer_name: str,
    ) -> bool:
        return await _execute_count(
            self._conn(tx),
            """
            UPDATE memories
            SET consolidated_into = ?,
                consolidated_at = COALESCE(?, CURRENT_TIMESTAMP),
                permission_mode = 400,
                metadata = json_set(
                    COALESCE(NULLIF(metadata, ''), '{}'),
                    '$.federation_consolidation',
                    json_object(
                        'remote_id', ?,
                        'remote_consolidated_into', ?,
                        'peer', ?
                    )
                )
            WHERE id = ?
              AND (consolidated_into IS NULL OR consolidated_into <> ?)
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = ?
              )
            """,
            (
                local_canonical_id,
                consolidated_at,
                remote_id,
                canonical_remote_id,
                peer_name,
                local_id,
                local_canonical_id,
                local_canonical_id,
            ),
        ) > 0

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        local_id = f"fed:{peer_name}:{memory_id}"
        return await _execute_count(
            self._conn(tx),
            """
            DELETE FROM memories
            WHERE id = ?
              AND federation_source = ?
            """,
            (local_id, peer_name),
        )


class SqliteStateRepository(_SqliteRepository, StateRepository):
    async def get(self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default") -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT key, value, updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = ? AND namespace = ? AND key = ? AND deleted_at IS NULL",
            (owner_id, namespace, key),
        )

    async def set(
        self,
        tx: Transaction,
        key: str,
        value: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        expires_at: Any | None = None,
    ) -> Row | None:
        _ = expires_at
        return await _fetch_one(
            self._conn(tx),
            "INSERT INTO state (owner_id, namespace, key, value, updated) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(owner_id, namespace, key) DO UPDATE SET "
            "value = excluded.value, updated = CURRENT_TIMESTAMP, version = state.version + 1 "
            "WHERE state.deleted_at IS NULL "
            "RETURNING key, value, updated, version, owner_id, namespace",
            (owner_id, namespace, key, value),
        )

    async def delete(self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default") -> bool:
        return await _execute_count(
            self._conn(tx),
            "DELETE FROM state WHERE owner_id = ? AND namespace = ? AND key = ? AND deleted_at IS NULL",
            (owner_id, namespace, key),
        ) > 0

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        params: list[Any] = [owner_id, namespace]
        sql = (
            "SELECT key, updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = ? AND namespace = ? AND deleted_at IS NULL ORDER BY key"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return await _fetch_all(self._conn(tx), sql, params)

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        return await _execute_count(
            self._conn(tx),
            "DELETE FROM state WHERE owner_id = ? AND namespace = ? AND deleted_at IS NULL",
            (owner_id, namespace),
        )


class SqliteBackend(PersistenceBackend):
    """SQLite persistence facade backed by one serialized connection."""

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    uses_sqlite_vec = True
    uses_fts5 = True

    def __init__(self, db_path: Path | str, settings: Any):
        self._db_path = Path(db_path)
        self._settings = settings
        self._conn: Any | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._vec_loaded = False
        self._memories = SqliteMemoryRepository()
        self._kg_triples = SqliteKGRepository()
        self._memory_versions = SqliteVersionRepository()
        self._memory_branches = SqliteBranchRepository()
        self._compression = SqliteCompressionRepository()
        self._webhooks = SqliteWebhookRepository()
        self._consultations_audit = SqliteConsultationAuditRepository()
        self._federation = SqliteFederationRepository()
        self._state_kv = SqliteStateRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def vec_loaded(self) -> bool:
        return self._vec_loaded

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self._db_path != Path(":memory:"):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if aiosqlite is not None:
            conn = await aiosqlite.connect(str(self._db_path))
        else:
            conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = _dict_factory
        await self._check_sqlite_version(conn)
        self._conn = conn
        await _execute(conn, "PRAGMA journal_mode=WAL")
        await _execute(conn, "PRAGMA foreign_keys=ON")
        await self._register_functions(conn)
        await self._load_sqlite_vec(conn)
        await self._apply_migrations(conn)
        await self._ensure_repository_columns(conn)
        await self._create_vec_virtual_table(conn)
        # Wire the configured dim into the memory repository so the runtime
        # search + upsert paths can enforce the same invariant the startup
        # checks established. Without this, a misconfigured embedding
        # endpoint after open() could poison the table or silently degrade
        # search until the next restart.
        self._memories._expected_embedding_dim = self._resolve_embedding_dim()
        await _commit(conn)

    async def _check_sqlite_version(self, conn: Any) -> None:
        raw_version = await _fetch_val(conn, "SELECT sqlite_version()")
        version = tuple(int(part) for part in str(raw_version).split(".")[:3])
        if version < MIN_SQLITE_VERSION:
            await _call(conn.close)
            required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
            raise RuntimeError(
                f"SQLite {required}+ is required for UPDATE ... RETURNING support; "
                f"found {raw_version}"
            )

    async def _register_functions(self, conn: Any) -> None:
        await _call(conn.create_function, "mnemos_cosine_similarity", 2, _cosine_similarity)
        await _call(conn.create_function, "mnemos_content_sha256", 1, _content_hash_for_sqlite)

    async def _load_sqlite_vec(self, conn: Any) -> None:
        try:
            await _call(conn.enable_load_extension, True)
            try:
                await _call(conn.load_extension, "vec0")
            finally:
                await _call(conn.enable_load_extension, False)
            self._vec_loaded = True
            return
        except Exception as exc:
            logger.debug("sqlite-vec load_extension('vec0') unavailable: %s", exc)

        try:  # pragma: no cover - depends on optional sqlite-vec wheel.
            import sqlite_vec

            raw_conn = getattr(conn, "_conn", conn)
            sqlite_vec.load(raw_conn)
            self._vec_loaded = True
        except Exception as exc:  # pragma: no cover - optional path.
            logger.debug("sqlite-vec Python loader unavailable; using cosine UDF fallback: %s", exc)

    async def _apply_migrations(self, conn: Any) -> None:
        migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations_sqlite"
        for migration_name in SQLITE_MIGRATION_FILES:
            migration_path = migrations_dir / migration_name
            if not migration_path.exists():
                continue
            try:
                await _executescript(conn, migration_path.read_text())
            except sqlite3.OperationalError as exc:
                # SQLite ``ALTER TABLE ADD COLUMN`` cannot be wrapped
                # in IF NOT EXISTS and re-running it on a column that
                # already exists raises ``duplicate column name``.
                # Treat that specific error as a no-op so upgrade
                # migrations stay idempotent — every other operational
                # error still propagates.
                if "duplicate column name" in str(exc).lower():
                    logger.debug(
                        "sqlite migration %s: column already present, skipping (%s)",
                        migration_name,
                        exc,
                    )
                    continue
                raise

    async def _ensure_repository_columns(self, conn: Any) -> None:
        await self._ensure_columns(
            conn,
            "state",
            {
                "updated_by": "updated_by TEXT",
                "version": "version INTEGER NOT NULL DEFAULT 1",
                "deleted_at": "deleted_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "memories",
            {
                "federation_remote_updated": "federation_remote_updated TEXT",
                "archived_at": "archived_at TEXT",
                "consolidated_into": "consolidated_into TEXT",
                "consolidated_at": "consolidated_at TEXT",
                "deleted_at": "deleted_at TEXT",
                "content_hash": "content_hash TEXT",
            },
        )
        await _execute(
            conn,
            "UPDATE memories SET content_hash = mnemos_content_sha256(content) WHERE content_hash IS NULL",
        )
        await self._ensure_columns(
            conn,
            "federation_peers",
            {
                "auth_token": "auth_token TEXT",
                "namespace_filter": "namespace_filter TEXT",
                "category_filter": "category_filter TEXT",
                "sync_interval_secs": "sync_interval_secs INTEGER NOT NULL DEFAULT 300",
                "last_sync_cursor": "last_sync_cursor TEXT",
                "last_error": "last_error TEXT",
                "last_error_at": "last_error_at TEXT",
                "total_pulled": "total_pulled INTEGER NOT NULL DEFAULT 0",
                "compat_mode": "compat_mode TEXT NOT NULL DEFAULT 'strict'",
                "peer_mnemos_version": "peer_mnemos_version TEXT",
                "last_schema_check_at": "last_schema_check_at TEXT",
                "created": "created TEXT",
                "updated": "updated TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "federation_sync_log",
            {
                "started_at": "started_at TEXT",
                "finished_at": "finished_at TEXT",
                "memories_pulled": "memories_pulled INTEGER NOT NULL DEFAULT 0",
                "memories_new": "memories_new INTEGER NOT NULL DEFAULT 0",
                "memories_updated": "memories_updated INTEGER NOT NULL DEFAULT 0",
                "cursor_before": "cursor_before TEXT",
                "cursor_after": "cursor_after TEXT",
            },
        )

    async def _ensure_columns(self, conn: Any, table: str, definitions: dict[str, str]) -> None:
        rows = await _fetch_all(conn, f"PRAGMA table_info({table})")
        existing = {row["name"] for row in rows}
        for column, definition in definitions.items():
            if column not in existing:
                await _execute(conn, f"ALTER TABLE {table} ADD COLUMN {definition}")

    async def _create_vec_virtual_table(self, conn: Any) -> None:
        dim = self._resolve_embedding_dim()
        # Guard 1: if the vec0 virtual table already exists at a different dim,
        # the CREATE ... IF NOT EXISTS DDL would be a silent no-op and the
        # service would run searches against the wrong dim. Fatal — operator
        # must explicitly migrate.
        existing_vec_dim = await self._existing_vec_table_dim(conn)
        if existing_vec_dim is not None and existing_vec_dim != dim:
            raise RuntimeError(
                f"SQLite vec0 dimension mismatch: memory_embedding_vec exists "
                f"at dim={existing_vec_dim} but MNEMOS_EMBEDDING_DIM resolves "
                f"to {dim}. The vec0 virtual table cannot be re-sized in place. "
                f"To migrate: stop this service, run "
                f"`sqlite3 {self._db_path} 'DROP TABLE memory_embedding_vec; "
                f"DELETE FROM memory_embeddings;'`, then restart the service to "
                f"recreate the table at the new dim and re-embed all memories. "
                f"Refusing to start to prevent silent search degradation."
            )
        # Guard 2: scan ALL fallback memory_embeddings rows. Stale-dim rows
        # would silently score 0.0 in cosine similarity against new-dim
        # queries — search degrades to "rank by recency" with no warning.
        # We can't trust a single-row sample because a DB poisoned before
        # the runtime dim guard landed (c9007dd) can have mixed-dim rows.
        fb_histogram = await self._scan_fallback_embedding_dims(conn)
        bad_dims = {d: c for d, c in fb_histogram.items() if d != dim}
        if bad_dims:
            shape = ", ".join(f"dim={d} x{c}" for d, c in sorted(bad_dims.items()))
            raise RuntimeError(
                f"SQLite fallback embedding dimension mismatch: "
                f"memory_embeddings has {sum(bad_dims.values())} rows at "
                f"non-configured dims ({shape}); MNEMOS_EMBEDDING_DIM "
                f"resolves to {dim}. Searching new-dim queries against "
                f"stale-dim rows produces meaningless cosine scores. To "
                f"migrate: stop this service, run "
                f"`sqlite3 {self._db_path} 'DELETE FROM memory_embeddings;'`, "
                f"then restart and re-embed all memories at the new dim. "
                f"Refusing to start to prevent silent search degradation."
            )
        if not self._vec_loaded:
            return
        try:
            await _execute(
                conn,
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_embedding_vec USING vec0(embedding float[{dim}])",
            )
        except Exception as exc:
            self._vec_loaded = False
            logger.debug("sqlite-vec virtual table creation failed; using fallback memory_embeddings table: %s", exc)

    async def _existing_vec_table_dim(self, conn: Any) -> Optional[int]:
        """Return the embedded float[N] dim of memory_embedding_vec if it exists.

        Returns None if the table doesn't exist (fresh install) or if the DDL
        can't be parsed. Parses the DDL string from sqlite_master.sql; format is
        ``CREATE VIRTUAL TABLE ... USING vec0(embedding float[<N>])`` where N
        is a positive integer.
        """
        row = await _fetch_one(
            conn,
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='memory_embedding_vec'",
        )
        if not row:
            return None
        ddl = row.get("sql") if isinstance(row, dict) else row[0]
        if not ddl:
            return None
        match = re.search(r"float\[(\d+)\]", ddl, re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def _scan_fallback_embedding_dims(self, conn: Any) -> dict[int, int]:
        """Return a histogram of {dim: row_count} across all memory_embeddings.

        A DB that was running BEFORE the runtime dim guard landed could have
        accumulated mixed-dim rows from a misconfigured embedding endpoint
        (e.g. the model was switched mid-flight). Sampling a single row could
        miss this — if the sample happened to match the configured dim,
        startup would succeed while stale-dim rows lurked in the table and
        silently scored 0.0 in cosine similarity.

        This scans every row. The query uses sqlite's json_array_length
        which is O(1) per row given the stored format. For the PYTHIA fleet
        (~9k memories) this is millisecond-scale at boot. Returns empty
        dict if the table is absent or has no rows.
        """
        row_meta = await _fetch_one(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='memory_embeddings'",
        )
        if not row_meta:
            return {}
        rows = await _fetch_all(
            conn,
            "SELECT json_array_length(embedding) AS dim, COUNT(*) AS cnt "
            "FROM memory_embeddings WHERE embedding IS NOT NULL "
            "GROUP BY json_array_length(embedding)",
        )
        histogram: dict[int, int] = {}
        for r in rows:
            dim = r.get("dim") if isinstance(r, dict) else r[0]
            cnt = r.get("cnt") if isinstance(r, dict) else r[1]
            if dim is None:
                continue
            try:
                histogram[int(dim)] = int(cnt)
            except (TypeError, ValueError):
                continue
        return histogram

    def _resolve_embedding_dim(self) -> int:
        # Settings can be None in tests + lite-CLI paths. Fall back to 768
        # (nomic-embed-text default) when no override is available.
        try:
            dim = self._settings.database.embedding_dim
        except AttributeError:
            return 768
        # sqlite-vec's SQLITE_VEC_VEC0_MAX_DIMENSIONS upstream caps at 8192;
        # values above silently fail the CREATE VIRTUAL TABLE and drop us to
        # the slower JSON/UDF path. Reject those before they bite.
        if not isinstance(dim, int) or dim < 1 or dim > 8192:
            logger.warning(
                "MNEMOS_EMBEDDING_DIM=%r out of supported range [1, 8192] "
                "(sqlite-vec SQLITE_VEC_VEC0_MAX_DIMENSIONS); falling back to "
                "768. Set the env var to your model's actual dim.",
                dim,
            )
            return 768
        return dim

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        if self._closed:
            raise RuntimeError("SQLite backend is closed")
        async with self._lock:
            await self.open()
            assert self._conn is not None
            await _execute(self._conn, "BEGIN IMMEDIATE")
            tx = SqliteTransaction(self._conn)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    @property
    def memories(self) -> MemoryRepository:
        return self._memories

    @property
    def kg_triples(self) -> KGRepository:
        return self._kg_triples

    @property
    def memory_versions(self) -> VersionRepository:
        return self._memory_versions

    @property
    def memory_branches(self) -> BranchRepository:
        return self._memory_branches

    @property
    def compression(self) -> CompressionRepository:
        return self._compression

    @property
    def webhooks(self) -> WebhookRepository:
        return self._webhooks

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit

    @property
    def federation(self) -> FederationRepository:
        return self._federation

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv

    async def close(self) -> None:
        if self._closed:
            return
        if self._conn is not None:
            await _call(self._conn.close)
        self._conn = None
        self._closed = True

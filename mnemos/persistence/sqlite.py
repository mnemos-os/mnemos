"""SQLite persistence backend for the MNEMOS persistence interface."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised when the sqlite extra is installed.
    import aiosqlite
except ImportError:  # pragma: no cover - local CI can run without optional extra.
    aiosqlite = None

from mnemos.core.auth_context import UserContext
from mnemos.persistence.base import (
    BranchRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    PersistenceBackend,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import Row

logger = logging.getLogger(__name__)


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


def _cosine_similarity(left: Any, right: Any) -> float:
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call(method: Any, *args: Any) -> Any:
    return await _maybe_await(method(*args))


async def _execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    normalized = tuple(_sqlite_value(value) for value in params)
    return await _maybe_await(conn.execute(sql, normalized))


async def _executemany(conn: Any, sql: str, rows: Sequence[Sequence[Any]]) -> Any:
    normalized = [tuple(_sqlite_value(value) for value in row) for row in rows]
    return await _maybe_await(conn.executemany(sql, normalized))


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
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            f"metadata FROM memories {where} ORDER BY created ASC LIMIT ? OFFSET ?",
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
        created: Any,
        updated: Any,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO memories (
                id, content, category, subcategory, metadata,
                quality_rating, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                created, updated
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP)
            )
            """,
            (
                memory_id,
                content,
                category,
                subcategory,
                metadata_json,
                quality_rating,
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
        embedding: Sequence[float],
        *,
        limit: int = 5,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[Row]:
        embedding_json = json.dumps([float(value) for value in embedding])
        conditions = ["me.embedding IS NOT NULL"]
        params: list[Any] = [embedding_json]
        if owner_id is not None:
            conditions.append("m.owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("m.namespace = ?")
            params.append(namespace)
        params.append(limit)
        return await _fetch_all(
            self._conn(tx),
            "SELECT m.id, m.content, m.category, "
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
        query: str,
        *,
        limit: int = 5,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = self._conn(tx)
        conditions: list[str] = []
        params: list[Any] = [query]
        if owner_id is not None:
            conditions.append("m.owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("m.namespace = ?")
            params.append(namespace)
        where_extra = f" AND {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        try:
            return await _fetch_all(
                conn,
                "SELECT m.id, m.content, m.category, bm25(memories_fts) AS rank "
                "FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.id "
                f"WHERE memories_fts MATCH ?{where_extra} "
                "ORDER BY rank ASC, m.updated DESC LIMIT ?",
                params,
            )
        except sqlite3.Error:
            like_params: list[Any] = [f"%{query}%"]
            like_conditions = ["lower(m.content) LIKE lower(?)"]
            if owner_id is not None:
                like_conditions.append("m.owner_id = ?")
                like_params.append(owner_id)
            if namespace is not None:
                like_conditions.append("m.namespace = ?")
                like_params.append(namespace)
            like_params.append(limit)
            return await _fetch_all(
                conn,
                "SELECT m.id, m.content, m.category FROM memories m "
                f"WHERE {' AND '.join(like_conditions)} ORDER BY m.updated DESC LIMIT ?",
                like_params,
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
            f"SELECT id, events FROM webhook_subscriptions WHERE {' AND '.join(conditions)}",
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
                (delivery_id, sub["id"], event_type, body, body_hash, 2),
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

        def _avg_cost(row: Row) -> float:
            return (float(row["input_cost_per_mtok"] or 0) + float(row["output_cost_per_mtok"] or 0)) / 2.0

        eligible = [
            row for row in rows
            if float(row["graeae_weight"] or 0) >= quality_floor
            and _avg_cost(row) <= cost_budget
            and all(cap in _json_list(row["capabilities"]) for cap in required_caps)
        ]
        chosen_rows = sorted(eligible, key=_avg_cost)
        if not chosen_rows:
            chosen_rows = sorted(rows, key=_avg_cost)
        if not chosen_rows:
            return None, required_caps
        model = chosen_rows[0]
        return {
            "provider": model["provider"],
            "model_id": model["model_id"],
            "display_name": model["display_name"],
            "cost_per_mtok": _avg_cost(model),
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


class SqliteStateRepository(_SqliteRepository, StateRepository):
    async def get(self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default") -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT key, value, owner_id, namespace FROM state WHERE owner_id = ? AND namespace = ? AND key = ?",
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
    ) -> None:
        await _execute(
            self._conn(tx),
            "INSERT INTO state (owner_id, namespace, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id, namespace, key) DO UPDATE SET value = excluded.value, updated = CURRENT_TIMESTAMP",
            (owner_id, namespace, key, value),
        )

    async def delete(self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default") -> None:
        await _execute(
            self._conn(tx),
            "DELETE FROM state WHERE owner_id = ? AND namespace = ? AND key = ?",
            (owner_id, namespace, key),
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
        self._conn = conn
        await _execute(conn, "PRAGMA journal_mode=WAL")
        await _execute(conn, "PRAGMA foreign_keys=ON")
        await self._register_functions(conn)
        await self._load_sqlite_vec(conn)
        await self._apply_migrations(conn)
        await self._create_vec_virtual_table(conn)
        await _commit(conn)

    async def _register_functions(self, conn: Any) -> None:
        await _call(conn.create_function, "mnemos_cosine_similarity", 2, _cosine_similarity)

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
            await _executescript(conn, migration_path.read_text())

    async def _create_vec_virtual_table(self, conn: Any) -> None:
        if not self._vec_loaded:
            return
        try:
            await _execute(
                conn,
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_embedding_vec USING vec0(embedding float[768])",
            )
        except Exception as exc:
            self._vec_loaded = False
            logger.debug("sqlite-vec virtual table creation failed; using fallback memory_embeddings table: %s", exc)

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

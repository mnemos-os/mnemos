"""PERSEPHONE cold-set archival and restore operations."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import zstandard as zstd

logger = logging.getLogger(__name__)

ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_CONTENT_PREFIX = "ARCHIVED:"
DEFAULT_ARCHIVED_BY = "system:persephone"

_ARCHIVE_SELECT_SQL = """
SELECT id, content, category, subcategory, metadata, quality_rating,
       verbatim_content, owner_id, group_id, namespace, permission_mode,
       source_model, source_provider, source_session, source_agent,
       source_memories, provenance, morpheus_run_id, consolidated_into,
       triples_extracted_at, recall_count, last_recalled_at,
       created, updated, archived_at
  FROM memories
 WHERE id = $1
   AND deleted_at IS NULL
 FOR UPDATE
"""

_ELIGIBLE_SQL = """
SELECT id
  FROM memories
 WHERE deleted_at IS NULL
   AND archived_at IS NULL
   AND consolidated_into IS NULL
   AND (last_recalled_at IS NULL OR last_recalled_at < NOW() - ($2::int * INTERVAL '1 day'))
   AND created < NOW() - ($2::int * INTERVAL '1 day')
   AND namespace = $1
 ORDER BY created ASC
 LIMIT $3
 FOR UPDATE SKIP LOCKED
"""


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _coerce_json_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_coerce_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_coerce_json_value(item) for item in value]
    return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _archive_payload(row: Any) -> dict[str, Any]:
    metadata = _row_get(row, "metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {"_raw": metadata}
    elif metadata is None:
        metadata = {}

    fields = {
        "id": _row_get(row, "id"),
        "content": _row_get(row, "content"),
        "category": _row_get(row, "category"),
        "subcategory": _row_get(row, "subcategory"),
        "metadata": metadata,
        "quality_rating": _row_get(row, "quality_rating"),
        "verbatim_content": _row_get(row, "verbatim_content"),
        "owner_id": _row_get(row, "owner_id"),
        "group_id": _row_get(row, "group_id"),
        "namespace": _row_get(row, "namespace"),
        "permission_mode": _row_get(row, "permission_mode"),
        "source_model": _row_get(row, "source_model"),
        "source_provider": _row_get(row, "source_provider"),
        "source_session": _row_get(row, "source_session"),
        "source_agent": _row_get(row, "source_agent"),
        "source_memories": _row_get(row, "source_memories"),
        "provenance": _row_get(row, "provenance"),
        "morpheus_run_id": _row_get(row, "morpheus_run_id"),
        "consolidated_into": _row_get(row, "consolidated_into"),
        "triples_extracted_at": _row_get(row, "triples_extracted_at"),
        "recall_count": _row_get(row, "recall_count"),
        "last_recalled_at": _row_get(row, "last_recalled_at"),
        "created": _row_get(row, "created"),
        "updated": _row_get(row, "updated"),
    }
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "memory": _coerce_json_value(fields),
    }


def _compress_payload(payload: dict[str, Any]) -> tuple[bytes, int]:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    compressed = zstd.ZstdCompressor().compress(raw)
    return compressed, len(raw)


def _decompress_payload(compressed: bytes) -> dict[str, Any]:
    raw = zstd.ZstdDecompressor().decompress(bytes(compressed))
    payload = json.loads(raw.decode("utf-8"))
    if int(payload.get("schema_version") or 0) != ARCHIVE_SCHEMA_VERSION:
        raise ValueError("unsupported memory archive schema_version")
    memory = payload.get("memory")
    if not isinstance(memory, dict) or "content" not in memory:
        raise ValueError("invalid memory archive payload")
    return payload


async def is_archived(conn: Any, memory_id: str) -> bool:
    """Return True when the live memory row is an archive stub."""
    archived_at = await conn.fetchval(
        "SELECT archived_at FROM memories WHERE id = $1 AND deleted_at IS NULL",
        memory_id,
    )
    return archived_at is not None


async def archive_memory(conn: Any, memory_id: str, archived_by: str | None = None) -> None:
    """Move one live memory into compressed archival storage.

    The live row remains in ``memories`` as ``content='ARCHIVED:<id>'`` with
    ``archived_at`` set. The content update intentionally fires the existing
    version/federation trigger path.
    """
    archived_by = archived_by or DEFAULT_ARCHIVED_BY
    async with conn.transaction():
        row = await conn.fetchrow(_ARCHIVE_SELECT_SQL, memory_id)
        if row is None:
            raise ValueError(f"memory {memory_id!r} not found")
        if _row_get(row, "archived_at") is not None:
            return

        payload = _archive_payload(row)
        compressed, original_size = _compress_payload(payload)

        await conn.execute(
            """
            INSERT INTO memory_archive (
                id, archived_by, compressed_content, compression_algo,
                original_size_bytes, compressed_size_bytes, schema_version
            ) VALUES ($1, $2, $3, 'zstd', $4, $5, $6)
            """,
            memory_id,
            archived_by,
            compressed,
            original_size,
            len(compressed),
            ARCHIVE_SCHEMA_VERSION,
        )
        await conn.execute(
            "SELECT set_config('mnemos.current_user_id', $1, true)",
            archived_by,
        )
        result = await conn.execute(
            """
            UPDATE memories
               SET content = $2,
                   verbatim_content = NULL,
                   archived_at = NOW(),
                   updated = NOW()
             WHERE id = $1
               AND archived_at IS NULL
               AND deleted_at IS NULL
            """,
            memory_id,
            f"{ARCHIVE_CONTENT_PREFIX}{memory_id}",
        )
        if result == "UPDATE 0":
            raise RuntimeError(f"memory {memory_id!r} was not archived")


async def restore_memory(conn: Any, memory_id: str, restored_by: str | None = None) -> None:
    """Restore an archived memory's live content from ``memory_archive``."""
    restored_by = restored_by or "system:persephone-restore"
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT m.id, m.archived_at, a.compressed_content, a.compression_algo
              FROM memories m
              JOIN memory_archive a ON a.id = m.id
             WHERE m.id = $1
               AND m.deleted_at IS NULL
             FOR UPDATE OF m
            """,
            memory_id,
        )
        if row is None or _row_get(row, "archived_at") is None:
            raise ValueError(f"memory {memory_id!r} is not archived")
        if _row_get(row, "compression_algo") != "zstd":
            raise ValueError("unsupported memory archive compression_algo")

        payload = _decompress_payload(_row_get(row, "compressed_content"))
        memory = payload["memory"]
        metadata = memory.get("metadata") or {}

        await conn.execute(
            "SELECT set_config('mnemos.current_user_id', $1, true)",
            restored_by,
        )
        result = await conn.execute(
            """
            UPDATE memories
               SET content = $2,
                   verbatim_content = $3,
                   metadata = $4::jsonb,
                   archived_at = NULL,
                   updated = NOW()
             WHERE id = $1
               AND archived_at IS NOT NULL
               AND deleted_at IS NULL
            """,
            memory_id,
            memory["content"],
            memory.get("verbatim_content"),
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        if result == "UPDATE 0":
            raise RuntimeError(f"memory {memory_id!r} was not restored")
        await conn.execute("DELETE FROM memory_archive WHERE id = $1", memory_id)


async def sweep_for_archival(
    pool: Any,
    namespace: str,
    archive_after_days: int,
    batch_size: int,
) -> int:
    """Archive up to ``batch_size`` cold memories in one namespace."""
    if archive_after_days < 1:
        raise ValueError("archive_after_days must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    archived = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                _ELIGIBLE_SQL,
                namespace,
                int(archive_after_days),
                int(batch_size),
            )
            for row in rows:
                await archive_memory(conn, row["id"], DEFAULT_ARCHIVED_BY)
                archived += 1
    if archived:
        logger.info(
            "persephone archived %d memory row(s) namespace=%s after_days=%d",
            archived,
            namespace,
            archive_after_days,
        )
    return archived

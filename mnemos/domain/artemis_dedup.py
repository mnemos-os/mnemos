"""ARTEMIS duplicate-content detection helpers."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Literal

from mnemos.core.config import get_settings


DedupAction = Literal["create", "reject", "merge", "warn"]
DedupMode = Literal["reject", "merge", "warn", "off"]

_VALID_MODES: set[str] = {"reject", "merge", "warn", "off"}


@dataclass(frozen=True)
class DedupDecision:
    action: DedupAction
    content_hash: str
    existing_id: str | None = None
    hint: str | None = None


def normalize_content_for_hash(content: str) -> str:
    """Normalize newlines before content-addressing a memory body."""
    return (content or "").replace("\r\n", "\n").replace("\r", "\n")


def content_sha256(content: str) -> str:
    return hashlib.sha256(normalize_content_for_hash(content).encode("utf-8")).hexdigest()


def artemis_dedup_mode() -> DedupMode:
    raw = (get_settings().artemis.dedup_mode or "reject").strip().lower()
    if raw not in _VALID_MODES:
        return "reject"
    return raw  # type: ignore[return-value]


def artemis_dedup_cross_namespace() -> bool:
    return bool(get_settings().artemis.dedup_cross_namespace)


async def evaluate_memory_create_dedup(
    memory_repo,
    tx,
    *,
    owner_id: str,
    namespace: str,
    content: str,
    logger: logging.Logger | None = None,
) -> DedupDecision:
    """Return the write-time duplicate-content decision for a create.

    The route owns HTTP status shaping; this helper owns ARTEMIS'
    content-addressing and mode semantics.
    """
    digest = content_sha256(content)
    mode = artemis_dedup_mode()
    if mode == "off":
        return DedupDecision(action="create", content_hash=digest)

    existing = await memory_repo.find_active_duplicate_by_content_hash(
        tx,
        owner_id=owner_id,
        namespace=namespace,
        content_hash=digest,
        cross_namespace=artemis_dedup_cross_namespace(),
    )
    if existing is None:
        return DedupDecision(action="create", content_hash=digest)

    existing_id = existing["id"]
    hint = "Memory with identical content already exists; consider update instead of create"
    if mode == "warn":
        if logger is not None:
            logger.warning(
                "duplicate_content_created",
                extra={
                    "existing_id": existing_id,
                    "owner_id": owner_id,
                    "namespace": namespace,
                    "content_hash": digest,
                },
            )
        return DedupDecision(
            action="warn",
            content_hash=digest,
            existing_id=existing_id,
            hint=hint,
        )
    if mode == "merge":
        return DedupDecision(
            action="merge",
            content_hash=digest,
            existing_id=existing_id,
            hint=hint,
        )
    return DedupDecision(
        action="reject",
        content_hash=digest,
        existing_id=existing_id,
        hint=hint,
    )


def duplicate_content_error_body(existing_id: str) -> dict[str, str]:
    return {
        "error": "duplicate_content",
        "existing_id": existing_id,
        "hint": "Memory with identical content already exists; consider update instead of create",
    }


def _memory_ids_from_row(row: Any) -> list[str]:
    raw = row["memory_ids"]
    if isinstance(raw, str):
        return [part for part in raw.split("\x1f") if part]
    return [str(value) for value in (raw or [])]


def duplicate_group_payload(row: Any) -> dict[str, Any]:
    memory_ids = _memory_ids_from_row(row)
    try:
        canonical_id = row["canonical_id"]
    except (KeyError, TypeError):
        canonical_id = None
    return {
        "owner_id": row["owner_id"],
        "namespace": row["namespace"],
        "content_hash": row["content_hash"],
        "duplicate_count": int(row["duplicate_count"]),
        "canonical_id": canonical_id or (memory_ids[0] if memory_ids else None),
        "memory_ids": memory_ids,
        "duplicate_ids": memory_ids[1:],
    }


async def sweep_duplicate_content(
    backend,
    *,
    namespace: str | None = None,
    auto_merge: bool = False,
) -> dict[str, Any]:
    """Find duplicate active memories and optionally consolidate them."""
    async with backend.transactional() as tx:
        rows = await backend.memories.find_duplicate_content_groups(
            tx,
            namespace=namespace,
        )
        groups = [duplicate_group_payload(row) for row in rows]
        merged = 0
        if auto_merge:
            for group in groups:
                duplicate_ids = group["duplicate_ids"]
                if not duplicate_ids:
                    continue
                merged += await backend.memories.consolidate_duplicate_memories(
                    tx,
                    canonical_id=group["canonical_id"],
                    duplicate_ids=duplicate_ids,
                )
        return {
            "namespace": namespace,
            "groups": groups,
            "group_count": len(groups),
            "duplicate_count": sum(len(group["duplicate_ids"]) for group in groups),
            "auto_merge": auto_merge,
            "merged_count": merged,
        }

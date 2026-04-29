"""Portability ID compatibility shims."""

from __future__ import annotations

from typing import Any, Dict, Optional

from mnemos.core.ids import caller_scoped_id, caller_scoped_uuid


def _derive_caller_scoped_uuid(
    envelope_id: str,
    *,
    caller_owner: str,
    caller_namespace: str,
    extra: str = "",
) -> str:
    return caller_scoped_uuid(
        caller_owner=caller_owner,
        caller_namespace=caller_namespace,
        envelope_id=envelope_id,
        extra=extra,
    )


def _derive_caller_scoped_id(
    envelope_id: str,
    *,
    caller_owner: str,
    caller_namespace: str,
    content: str,
) -> str:
    return caller_scoped_id(
        caller_owner=caller_owner,
        caller_namespace=caller_namespace,
        envelope_id=envelope_id,
        content=content,
    )


def _row_owner_ns(
    entry: Dict[str, Any],
    *,
    caller_user_id: str,
    caller_namespace: str,
    preserve_owner: bool,
    has_namespace_column: bool = True,
) -> tuple[str, Optional[str]]:
    if preserve_owner:
        owner = entry.get("owner_id") or caller_user_id
        ns = (entry.get("namespace") or caller_namespace) if has_namespace_column else None
    else:
        owner = caller_user_id
        ns = caller_namespace if has_namespace_column else None
    return owner, ns

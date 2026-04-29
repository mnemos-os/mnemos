"""Caller-scoped deterministic ID derivation."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, Optional


def _derive_caller_scoped_uuid(
    envelope_id: str,
    *,
    caller_owner: str,
    caller_namespace: str,
    extra: str = "",
) -> str:
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    name = "\x00".join([caller_owner, caller_namespace, envelope_id, extra])
    return str(uuid.uuid5(namespace, name))


def _derive_caller_scoped_id(
    envelope_id: str,
    *,
    caller_owner: str,
    caller_namespace: str,
    content: str,
) -> str:
    h = hashlib.sha256(
        b"\x00".join(
            [
                caller_owner.encode("utf-8"),
                caller_namespace.encode("utf-8"),
                envelope_id.encode("utf-8"),
                content.encode("utf-8"),
            ]
        )
    ).hexdigest()[:32]
    return f"mnemos_{h}"


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

"""Canonical ID parsing and derivation helpers."""

from __future__ import annotations

import hashlib
import time
import uuid

from fastapi import HTTPException

_CALLER_SCOPED_NAMESPACE_UUID = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class IDNamespace:
    """Documented prefixes for deterministic IDs by subsystem."""

    MEMORY = "mem"
    KG_TRIPLE = "kgt"
    DREAM = "drm"
    VERSION = "ver"
    COMPRESSION_MANIFEST = "cpm"


def _parse_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def parse_uuid_or_400(value: str, what: str = "resource") -> str:
    """Parse a UUID string or raise a 400 validation error."""
    try:
        return _parse_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid {what} id format")


def parse_uuid_or_404(value: str, what: str = "resource") -> str:
    """Parse a UUID string or raise a 404 not-found guard."""
    try:
        return _parse_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{what} not found")


def caller_scoped_uuid(
    *,
    caller_owner: str,
    caller_namespace: str,
    envelope_id: str,
    extra: str = "",
) -> str:
    """Derive a deterministic UUID scoped to the caller and envelope."""
    name = "\x00".join([caller_owner, caller_namespace, envelope_id, extra])
    return str(uuid.uuid5(_CALLER_SCOPED_NAMESPACE_UUID, name))


def caller_scoped_id(
    *,
    caller_owner: str,
    caller_namespace: str,
    envelope_id: str,
    content: str,
) -> str:
    """Derive a deterministic MNEMOS memory-style ID scoped to caller content."""
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


def new_memory_id() -> str:
    """Return the standard timestamp-plus-hex memory ID."""
    return f"mem_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

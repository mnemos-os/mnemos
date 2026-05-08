"""NATS v0.3 event helpers for persistence-owned data changes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Mapping

from mnemos.core.config import get_settings
from mnemos.nats import client as nats_client
from mnemos.nats import publisher as nats_publisher

logger = logging.getLogger("mnemos.persistence.nats_events")

WEBHOOKS_OUTBOX_SUBJECT_PREFIX = "mnemos.webhooks.outbox"
FEDERATION_MEMORY_SUBJECT_PREFIX = "mnemos.federation.memory"
FEDERATION_MEMORY_SCHEMA_VERSION = "1"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def _publish_event(subject: str, payload: Mapping[str, Any], *, msg_id: str) -> None:
    timeout = float(get_settings().nats.publish_timeout_seconds)
    try:
        await asyncio.wait_for(
            nats_publisher.publish_event(subject, payload, msg_id=msg_id),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("NATS publish timed out after %.3fs for %s", timeout, subject)


def webhooks_nats_enabled() -> bool:
    return _truthy_env("MNEMOS_NATS_WEBHOOKS_ENABLED")


def federation_nats_enabled() -> bool:
    return _truthy_env("MNEMOS_NATS_FEDERATION_ENABLED")


def safe_subject_segment(value: Any, *, default: str = "default") -> str:
    raw = str(value if value is not None else default)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
    return safe or default


def _safe_event_type(event_type: str) -> str:
    parts = [safe_subject_segment(part, default="event") for part in event_type.split(".") if part]
    return ".".join(parts) or "event"


def webhook_outbox_subject(*, tenant: str | None, event_type: str) -> str:
    return f"{WEBHOOKS_OUTBOX_SUBJECT_PREFIX}.{safe_subject_segment(tenant)}.{_safe_event_type(event_type)}"


async def publish_webhook_outbox_insert(
    *,
    delivery_id: str,
    subscription_id: Any,
    event_type: str,
    url: str,
    payload_hash: str,
    namespace: str | None,
    owner_id: str | None,
) -> None:
    """Best-effort v0.3 webhook outbox insert fanout.

    ``webhook_deliveries`` remains the durable source of truth. This
    NATS event only wakes push consumers sooner than the polling worker.
    """
    if not webhooks_nats_enabled():
        return
    subject = webhook_outbox_subject(tenant=owner_id, event_type=event_type)
    payload = {
        "event_id": str(delivery_id),
        "delivery_id": str(delivery_id),
        "subscription_id": str(subscription_id),
        "event_type": event_type,
        "url": url,
        "payload_hash": payload_hash,
        "namespace": namespace,
        "tenant": owner_id,
        "owner_id": owner_id,
        "outbox_table": "webhook_deliveries",
        "source_node": nats_client.get_node_name(),
    }
    await _publish_event(
        subject,
        payload,
        msg_id=f"webhooks.outbox.{delivery_id}",
    )


def federation_memory_subject(namespace: str | None) -> str:
    return f"{FEDERATION_MEMORY_SUBJECT_PREFIX}.{safe_subject_segment(namespace)}"


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _wire_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def is_federation_memory_publishable(row: Mapping[str, Any] | Any) -> bool:
    if _row_value(row, "federation_source") is not None:
        return False
    if _row_value(row, "deleted_at") is not None:
        return False
    if _row_value(row, "archived_at") is not None:
        return False
    if _row_value(row, "consolidated_into") is not None:
        return False
    try:
        permission_mode = int(_row_value(row, "permission_mode", 0))
    except (TypeError, ValueError):
        return False
    return permission_mode % 10 >= 4


def federation_memory_upsert_event(row: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    if not is_federation_memory_publishable(row):
        return None
    memory_id = _row_value(row, "id")
    if not memory_id:
        return None
    updated = _wire_datetime(_row_value(row, "updated"))
    event_id = f"federation.memory.upsert.{memory_id}.{updated or 'unknown'}"
    content = _row_value(row, "content") or ""
    return {
        "schema_version": FEDERATION_MEMORY_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": "memory.upsert",
        "memory_id": str(memory_id),
        "id": str(memory_id),
        "content": content,
        "verbatim_content": _row_value(row, "verbatim_content") or content,
        "category": _row_value(row, "category") or "federation",
        "subcategory": _row_value(row, "subcategory"),
        "metadata": _json_object(_row_value(row, "metadata")),
        "quality_rating": _row_value(row, "quality_rating") or 75,
        "owner_id": _row_value(row, "owner_id"),
        "namespace": _row_value(row, "namespace") or "default",
        "permission_mode": _row_value(row, "permission_mode"),
        "source_model": _row_value(row, "source_model"),
        "source_provider": _row_value(row, "source_provider"),
        "source_session": _row_value(row, "source_session"),
        "source_agent": _row_value(row, "source_agent"),
        "created": _wire_datetime(_row_value(row, "created")),
        "updated": updated,
        "source_node": nats_client.get_node_name(),
    }


async def publish_federation_memory_upsert_event(event: Mapping[str, Any]) -> None:
    if not federation_nats_enabled():
        return
    subject = federation_memory_subject(str(event.get("namespace") or "default"))
    await _publish_event(
        subject,
        event,
        msg_id=str(event.get("event_id") or event.get("memory_id") or ""),
    )


# #185: removed `publish_federation_memory_upsert(row)` row-form
# overload — defined but never called. Live callers all use the
# `_event(event)` variant directly (postgres.py + integration
# tests). Callers wanting row-form can call
# `federation_memory_upsert_event(row)` then
# `publish_federation_memory_upsert_event(event)` inline.

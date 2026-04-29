"""Transactional webhook outbox inserts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import asyncpg

from . import types as webhook_types


async def _matching_subscriptions(
    conn: asyncpg.Connection,
    event_type: str,
    owner_id: Optional[str],
    namespace: Optional[str],
) -> Iterable[asyncpg.Record]:
    """Find non-revoked subscriptions that include this event_type.

    If owner_id/namespace are provided, filter to subscriptions with matching
    ownership. Otherwise, return all non-revoked matches (useful for
    system-level events not bound to a caller).
    """
    query = """
        SELECT id, url, events, secret, owner_id, namespace
        FROM webhook_subscriptions
        WHERE NOT revoked AND $1 = ANY(events)
    """
    args: list = [event_type]
    if owner_id is not None:
        query += " AND owner_id = $2"
        args.append(owner_id)
        if namespace is not None:
            query += " AND namespace = $3"
            args.append(namespace)
    return await conn.fetch(query, *args)


async def _dispatch_on_conn(
    conn: asyncpg.Connection,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> None:
    """Insert delivery intents using an already-selected connection."""
    subs = await _matching_subscriptions(conn, event_type, owner_id, namespace)
    if not subs:
        return

    body = json.dumps({
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }, separators=(",", ":"), sort_keys=True)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    for sub in subs:
        delivery_id = await conn.fetchval(
            """
            INSERT INTO webhook_deliveries
              (subscription_id, event_type, payload, payload_hash, status, writer_revision)
            VALUES ($1, $2, $3, $4, 'pending', $5)
            RETURNING id
            """,
            sub["id"], event_type, body, body_hash, webhook_types.NEW_CODE_WRITER_REVISION,
        )
        # Schedule the send via the lifecycle-tracked delivery registry
        # so graceful shutdown lets in-flight attempts finalize. Import lazily to avoid
        # circular imports at module load time.
        from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
        from .sender import _attempt_delivery
        _schedule_delivery_attempt(_attempt_delivery(str(delivery_id)))

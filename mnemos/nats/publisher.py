"""Fire-and-forget event publisher to NATS JetStream subjects.

API contract: never raise. NATS is best-effort additive at v4.2; the
durable webhook outbox remains the source of truth for delivery
guarantees. This publisher exists so subscribers (federation peers,
desktop CHARON-replay sync, MCP push) can react in real time.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from .client import get_jetstream

logger = logging.getLogger("mnemos.nats.publisher")


async def publish_event(
    subject: str,
    payload: Mapping[str, Any],
    *,
    msg_id: Optional[str] = None,
) -> None:
    """Publish a JSON-encoded event to a NATS subject.

    ``msg_id`` opts into JetStream's deduplication window (configured
    on the stream, default 2 minutes). Pass a stable id (e.g. the
    memory id + event type) for idempotent re-publishes during retries.
    """
    js = get_jetstream()
    if js is None:
        return

    try:
        body = json.dumps(payload, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning("publish_event: payload not json-serializable: %s", exc)
        return

    headers = {"Nats-Msg-Id": msg_id} if msg_id else None

    try:
        await js.publish(subject, body, headers=headers)
    except Exception as exc:
        logger.warning("publish_event: %s failed: %s", subject, exc)

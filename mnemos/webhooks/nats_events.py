"""NATS event helpers for webhook delivery outbox rows."""
from __future__ import annotations

import re
from typing import Any

from mnemos import nats as nats_bus
from mnemos.nats import client as nats_client


def safe_namespace(namespace: str | None) -> str:
    """Return a namespace token suitable for a NATS subject segment."""
    value = namespace or "default"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe or "default"


async def publish_delivery_queued(
    *,
    delivery_id: str,
    subscription_id: Any,
    event_type: str,
    url: str,
    payload_hash: str,
    namespace: str | None,
    owner_id: str | None,
) -> None:
    """Best-effort webhook delivery queued event.

    The Postgres outbox remains authoritative. This event only nudges
    dispatchers so they can attempt the existing row immediately.
    """
    subject = f"mnemos.webhook.delivery.queued.{safe_namespace(namespace)}"
    payload = {
        "delivery_id": str(delivery_id),
        "subscription_id": str(subscription_id),
        "event_type": event_type,
        "url": url,
        "payload_hash": payload_hash,
        "namespace": namespace,
        "owner_id": owner_id,
        "source_node": nats_client.get_node_name(),
    }
    await nats_bus.publish_event(
        subject,
        payload,
        msg_id=f"webhook.delivery.{delivery_id}.queued",
    )

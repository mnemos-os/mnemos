"""NATS event helpers for webhook delivery outbox rows.

Canonical implementation lives in :mod:`mnemos.nats.webhook_events`
so the persistence layer can publish queued-delivery nudges without
the layered-architecture contract being violated. This module
re-exports those symbols for callers in the webhook layer that
already import from ``mnemos.webhooks.nats_events``.
"""
from __future__ import annotations

from mnemos.nats.webhook_events import publish_delivery_queued, safe_namespace

__all__ = ["publish_delivery_queued", "safe_namespace"]

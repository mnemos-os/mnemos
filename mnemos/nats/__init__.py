"""NATS JetStream substrate for MNEMOS v4.2 — event bus + future MQ.

Public surface:

- ``connect_nats(settings) -> Optional[JetStreamContext]``
- ``ensure_streams(js)``: idempotent stream declarations
- ``publish_event(js, subject, payload)``: fire-and-forget publish

Failure mode: NATS is **optional**. If ``MNEMOS_NATS_URL`` is unset
or the broker is unreachable, the helper logs a warning and returns
``None``. Callers that get ``None`` MUST skip publish silently —
NATS is additive at v4.2; the existing webhooks outbox remains the
durable delivery path until v4.3 refactors that onto NATS.
"""

from .client import connect_nats, ensure_streams, get_jetstream
from .publisher import publish_event

__all__ = [
    "connect_nats",
    "ensure_streams",
    "get_jetstream",
    "publish_event",
]

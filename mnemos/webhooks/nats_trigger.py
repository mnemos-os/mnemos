"""NATS push trigger for webhook delivery outbox rows."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Mapping

import asyncpg

from mnemos.core.config import Settings, get_settings
from mnemos.nats.backoff import ReconnectBackoff

logger = logging.getLogger("mnemos.webhooks.nats_trigger")

SUBJECT = "mnemos.webhook.delivery.queued.>"
STREAM = "MNEMOS_WEBHOOK"
DURABLE = "mnemos_webhook_delivery_trigger"
QUEUE_GROUP = "mnemos_webhook_delivery_workers"


async def consumer_loop(
    pool: asyncpg.Pool,
    *,
    settings: Settings | None = None,
    retry_seconds: float = 30.0,
    connect: Callable[[Settings], Awaitable[Any | None]] | None = None,
) -> None:
    """Consume webhook queued nudges until cancelled."""
    settings = settings or get_settings()
    if not settings.nats.url:
        logger.info("webhook nats trigger disabled (MNEMOS_NATS_URL unset)")
        return
    connect = connect or _connect
    # Exponential backoff with full jitter capped at the existing
    # retry_seconds tuning so operator-set ceilings still apply.
    # Audit Finding 8 / mnemos.nats.backoff.ReconnectBackoff.
    backoff = ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)

    while True:
        nc = None
        sub = None
        try:
            connect_result = await connect(settings)
            # Backwards compat: legacy connect callables returned the
            # JetStream context directly; new path returns (nc, js).
            if isinstance(connect_result, tuple) and len(connect_result) == 2:
                nc, js = connect_result
            else:
                nc, js = None, connect_result
            if js is None:
                raise RuntimeError("NATS JetStream unavailable")
            logger.info("webhook nats trigger connected subject=%s", SUBJECT)
            sub = await _subscribe(js)
            # Reset AFTER subscribe succeeds. A broker that accepts
            # the connection but fails subscribe (stream drift,
            # consumer-group recovery) would otherwise reset every
            # iteration and burn through a tight loop.
            backoff.reset()
            # Subscription is now driven by _consume_subscription;
            # ownership transfers there. Don't double-clean below.
            sub_owned = sub
            sub = None
            try:
                await _consume_subscription(pool, sub_owned)
            finally:
                # Always drain after consume returns or raises;
                # otherwise the connection stays open across loop
                # iterations.
                await _drain_partial(nc, [sub_owned])
                nc = None
        except asyncio.CancelledError:
            logger.info("webhook nats trigger cancelled")
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            raise
        except Exception as exc:
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            delay = backoff.next_delay()
            logger.warning(
                "webhook nats trigger unavailable: %s; retrying in %.1fs",
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def _drain_partial(nc: Any, subscriptions: list[Any]) -> None:
    """Best-effort cleanup for partial connect/subscribe state.

    On a failed subscribe (broker accepted connection, stream/
    consumer rejected the subscribe), unsubscribe what landed and
    drain the underlying NATS connection so we do not leak one
    socket per retry. Errors during cleanup are swallowed — we are
    already in a failure path.
    """
    for s in subscriptions:
        try:
            await s.unsubscribe()
        except Exception:
            pass
    if nc is not None:
        try:
            await nc.drain()
        except Exception:
            try:
                await nc.close()
            except Exception:
                pass


async def handle_message(
    pool: asyncpg.Pool,
    msg: Any,
    *,
    schedule: Callable[[Awaitable[bool]], Any] | None = None,
    attempt: Callable[..., Awaitable[bool]] | None = None,
) -> None:
    """Schedule an immediate delivery attempt for one queued webhook event."""
    payload = _decode_payload(getattr(msg, "data", b""))
    if not _valid_payload(payload):
        raise PoisonMessageError(f"missing required fields payload={payload!r}")

    delivery_id = str(payload["delivery_id"])
    attempt = attempt or _attempt_delivery
    schedule = schedule or _schedule_attempt
    schedule(_attempt_once(delivery_id, pool, attempt))


async def _attempt_once(
    delivery_id: str,
    pool: asyncpg.Pool,
    attempt: Callable[..., Awaitable[bool]],
) -> bool:
    try:
        return await attempt(delivery_id, pool=pool)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("webhook nats trigger delivery attempt failed delivery_id=%s", delivery_id)
        return False


async def _consume_subscription(pool: asyncpg.Pool, sub: Any) -> None:
    """Drive the webhook trigger subscription's receive/handle/ack lifecycle.

    Three failure scopes, deliberately separated:

    1. **Receive** (``sub.next_msg``) — non-timeout failure is a
       NATS-connection issue. Escapes for ``consumer_loop`` to drain
       and reconnect with backoff.
    2. **Handle** (``handle_message``) — ANY failure is local. The
       webhook outbox in Postgres is authoritative; the
       ``repair_worker_loop`` polling fallback re-drives missed
       deliveries. A handler error (DB hiccup, downstream HTTP, etc.)
       should NOT tear down the NATS subscription. JetStream redelivers
       after ack-wait.
    3. **Ack** (``_ack``) — failure here is a NATS-connection issue
       (the broker is what we're acking to). Escape for reconnect.

    See v4.2.0a7 round-3 audit (codex finding 2026-05-01).
    """
    while True:
        msg = None

        # Scope 1: receive
        try:
            msg = await sub.next_msg(timeout=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception(
                "webhook nats trigger receive error (escaping for reconnect): %s",
                exc,
            )
            raise

        # Scope 2: handle (all failures stay local).
        try:
            await handle_message(pool, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "webhook nats trigger poison message subject=%s detail=%s",
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg)
            continue
        except Exception as exc:
            logger.exception(
                "webhook nats trigger handler error (subscription stays alive, no ack): %s",
                exc,
            )
            continue

        # Scope 3: ack
        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "webhook nats trigger ack error (escaping for reconnect): %s",
                exc,
            )
            raise


async def _ack_safely(msg: Any) -> None:
    try:
        await _ack(msg)
    except Exception as exc:  # noqa: BLE001 — best-effort poison ack
        logger.warning(
            "webhook nats trigger poison-ack failed (will be redelivered): %s",
            exc,
        )


async def _connect(settings: Settings):
    """Open a NATS connection + JetStream context.

    Returns ``(nc, js)`` so the caller can drain ``nc`` if subscribe
    fails before the consume loop starts. Without surfacing ``nc``,
    every subscribe failure leaked one TCP connection per retry.
    """
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py not installed") from exc

    connect_kwargs: dict[str, Any] = {"servers": [settings.nats.url]}
    if settings.nats.token:
        connect_kwargs["token"] = settings.nats.token
    nc = await nats.connect(**connect_kwargs)
    return nc, nc.jetstream()


def _node_durable() -> str:
    """Per-node durable name. The audit-fix queue-group attempt
    (`js.subscribe(durable=..., queue=..., config=DeliverGroup)`)
    is rejected by JetStream/nats-py 2.6 even when the consumer
    config declares a matching deliver_group. The proper path
    likely needs `js.add_consumer + js.pull_subscribe` (or
    bind_subscribe) instead of subscribe(); deferred.

    Per-node durables mean each node receives every nudge and
    races for the Postgres SKIP LOCKED claim — wasteful but
    correct. Audit Finding 5 stays open with this caveat.
    """
    from mnemos.nats.client import get_node_name
    safe = get_node_name().replace(".", "_").replace("-", "_") or "node"
    return f"{DURABLE}_{safe}"


async def _subscribe(js: Any):
    durable = _node_durable()
    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            durable_name=durable,
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.EXPLICIT,
        )
    except ImportError:
        config = None

    return await js.subscribe(
        SUBJECT,
        durable=durable,
        stream=STREAM,
        config=config,
    )


class PoisonMessageError(ValueError):
    """A malformed message that cannot succeed on redelivery."""


def _decode_payload(data: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    try:
        if isinstance(data, str):
            raw = data
        else:
            raw = bytes(data).decode("utf-8")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoisonMessageError("webhook nats trigger payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("webhook nats trigger payload must be a JSON object")
    return payload


def _valid_payload(payload: Mapping[str, Any]) -> bool:
    required_string_fields = ("delivery_id", "event_type", "url", "payload_hash", "source_node")
    if not all(isinstance(payload.get(field), str) and payload.get(field) for field in required_string_fields):
        return False
    return "subscription_id" in payload


def _schedule_attempt(coro: Awaitable[bool]) -> Any:
    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433

    return _schedule_delivery_attempt(coro)


async def _attempt_delivery(delivery_id: str, *, pool: asyncpg.Pool) -> bool:
    from .sender import _attempt_delivery as sender_attempt

    return await sender_attempt(delivery_id, pool=pool)


async def _ack(msg: Any) -> None:
    ack = getattr(msg, "ack", None)
    if ack is None:
        return
    result = ack()
    if hasattr(result, "__await__"):
        await result


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return exc.__class__.__name__ == "TimeoutError" and exc.__class__.__module__.startswith("nats")

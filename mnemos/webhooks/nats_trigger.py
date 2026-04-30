"""NATS push trigger for webhook delivery outbox rows."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Mapping

import asyncpg

from mnemos.core.config import Settings, get_settings

logger = logging.getLogger("mnemos.webhooks.nats_trigger")

SUBJECT = "mnemos.webhook.delivery.queued.>"
STREAM = "MNEMOS_WEBHOOK"
DURABLE = "mnemos_webhook_delivery_trigger"
QUEUE = "mnemos_webhook_dispatchers"


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

    while True:
        try:
            js = await connect(settings)
            if js is None:
                raise RuntimeError("NATS JetStream unavailable")
            logger.info("webhook nats trigger connected subject=%s", SUBJECT)
            sub = await _subscribe(js)
            await _consume_subscription(pool, sub)
        except asyncio.CancelledError:
            logger.info("webhook nats trigger cancelled")
            raise
        except Exception as exc:
            logger.warning(
                "webhook nats trigger unavailable: %s; retrying in %.0fs",
                exc,
                retry_seconds,
            )
            await asyncio.sleep(retry_seconds)


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
        logger.warning(
            "webhook nats trigger skipped invalid payload subject=%s payload=%r",
            getattr(msg, "subject", ""),
            payload,
        )
        return

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
    while True:
        msg = None
        try:
            msg = await sub.next_msg(timeout=1)
            await handle_message(pool, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception("webhook nats trigger event error: %s", exc)
        finally:
            if msg is not None:
                await _ack(msg)


async def _connect(settings: Settings):
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py not installed") from exc

    connect_kwargs: dict[str, Any] = {"servers": [settings.nats.url]}
    if settings.nats.token:
        connect_kwargs["token"] = settings.nats.token
    nc = await nats.connect(**connect_kwargs)
    return nc.jetstream()


async def _subscribe(js: Any):
    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            durable_name=DURABLE,
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.EXPLICIT,
        )
    except ImportError:
        config = None

    return await js.subscribe(
        SUBJECT,
        durable=DURABLE,
        queue=QUEUE,
        stream=STREAM,
        config=config,
    )


def _decode_payload(data: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    if isinstance(data, str):
        raw = data
    else:
        raw = bytes(data).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("webhook nats trigger payload must be a JSON object")
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

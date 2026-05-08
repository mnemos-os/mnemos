"""NATS v0.3 consumer for webhook outbox dispatch nudges."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Mapping

import asyncpg

from mnemos.core.config import Settings, get_settings
from mnemos.core.extras import is_extra_installed
from mnemos.nats.backoff import ReconnectBackoff
from mnemos.nats.client import get_node_name
from mnemos.persistence.nats_events import WEBHOOKS_OUTBOX_SUBJECT_PREFIX

logger = logging.getLogger("mnemos.workers.webhooks_dispatch_nats_consumer")

SUBJECT = f"{WEBHOOKS_OUTBOX_SUBJECT_PREFIX}.>"
STREAM = "MNEMOS_WEBHOOKS_OUTBOX"
DURABLE = "mnemos_webhooks_outbox_dispatch"
QUEUE_ENV = "MNEMOS_NATS_WEBHOOKS_QUEUE_GROUP"


class PoisonMessageError(ValueError):
    """Message cannot be decoded into the webhook outbox event shape."""


def _enabled() -> bool:
    return os.environ.get("MNEMOS_NATS_WEBHOOKS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def consumer_loop(
    pool: asyncpg.Pool,
    *,
    settings: Settings | None = None,
    retry_seconds: float = 30.0,
    connect: Callable[[Settings], Awaitable[Any | None]] | None = None,
) -> None:
    """Consume v0.3 webhook outbox events until cancelled."""
    settings = settings or get_settings()
    if not _enabled():
        logger.info("webhooks outbox nats consumer disabled")
        return
    if not is_extra_installed("nats"):
        logger.info("webhooks outbox nats consumer disabled (nats extra not installed)")
        return
    if not settings.nats.url:
        logger.info("webhooks outbox nats consumer disabled (MNEMOS_NATS_URL unset)")
        return

    connect = connect or _connect
    queue_group = os.environ.get(QUEUE_ENV, "").strip() or settings.nats.webhook_queue_group
    backoff = ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)

    while True:
        nc = None
        sub = None
        try:
            connect_result = await connect(settings)
            if isinstance(connect_result, tuple) and len(connect_result) == 2:
                nc, js = connect_result
            else:
                nc, js = None, connect_result
            if js is None:
                raise RuntimeError("NATS JetStream unavailable")
            logger.info("webhooks outbox nats consumer connected subject=%s", SUBJECT)
            sub = await _subscribe(js, queue_group=queue_group)
            backoff.reset()
            sub_owned = sub
            sub = None
            try:
                await _consume_subscription(pool, sub_owned)
            finally:
                await _drain_partial(nc, [sub_owned])
                nc = None
        except asyncio.CancelledError:
            logger.info("webhooks outbox nats consumer cancelled")
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            raise
        except Exception as exc:
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            delay = backoff.next_delay()
            logger.warning(
                "webhooks outbox nats consumer unavailable: %s; retrying in %.1fs",
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def handle_message(
    pool: asyncpg.Pool,
    msg: Any,
    *,
    schedule: Callable[[Awaitable[bool]], Any] | None = None,
    attempt: Callable[..., Awaitable[bool]] | None = None,
    record_dispatch: Callable[[asyncpg.Pool, str, str], Awaitable[bool]] | None = None,
) -> None:
    """Record and schedule one webhook outbox event."""
    subject = str(getattr(msg, "subject", ""))
    payload = _decode_payload(getattr(msg, "data", b""))
    if not _valid_payload(payload):
        raise PoisonMessageError(f"missing required fields payload={payload!r}")
    if payload.get("source_node") == get_node_name():
        logger.debug("webhooks outbox skipped self-loop event subject=%s", subject)
        return

    event_id = str(payload.get("event_id") or payload["delivery_id"])
    recorder = record_dispatch or _record_dispatch_once
    if not await recorder(pool, event_id, subject):
        logger.debug("webhooks outbox duplicate event_id=%s subject=%s", event_id, subject)
        return

    delivery_id = str(payload["delivery_id"])
    attempt = attempt or _attempt_delivery
    schedule = schedule or _schedule_attempt
    schedule(_attempt_once(delivery_id, pool, attempt))


async def _consume_subscription(pool: asyncpg.Pool, sub: Any) -> None:
    while True:
        msg = None
        try:
            msg = await sub.next_msg(timeout=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception("webhooks outbox nats receive error (escaping for reconnect): %s", exc)
            raise

        try:
            await handle_message(pool, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "webhooks outbox nats poison message subject=%s detail=%s",
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg)
            continue
        except Exception as exc:
            logger.exception(
                "webhooks outbox nats handler error (subscription stays alive, no ack): %s",
                exc,
            )
            continue

        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("webhooks outbox nats ack error (escaping for reconnect): %s", exc)
            raise


async def _record_dispatch_once(pool: asyncpg.Pool, event_id: str, subject: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nats_dispatch_log (event_id, subject)
            VALUES ($1, $2)
            ON CONFLICT (event_id, subject) DO NOTHING
            RETURNING event_id
            """,
            event_id,
            subject,
        )
    return row is not None


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
        logger.exception("webhooks outbox nats delivery attempt failed delivery_id=%s", delivery_id)
        return False


def _schedule_attempt(coro: Awaitable[bool]) -> Any:
    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433

    return _schedule_delivery_attempt(coro)


async def _attempt_delivery(delivery_id: str, *, pool: asyncpg.Pool) -> bool:
    from mnemos.webhooks.sender import _attempt_delivery as sender_attempt

    return await sender_attempt(delivery_id, pool=pool)


async def _connect(settings: Settings):
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py not installed") from exc

    connect_kwargs: dict[str, Any] = {"servers": [settings.nats.url]}
    if settings.nats.token:
        connect_kwargs["token"] = settings.nats.token
    nc = await nats.connect(**connect_kwargs)
    return nc, nc.jetstream()


async def _subscribe(js: Any, *, queue_group: str = "", stream: str = STREAM):
    if queue_group:
        durable = _queue_durable_name(queue_group)
    else:
        durable = _node_durable()
    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            durable_name=durable,
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_group=durable if queue_group else None,
        )
    except ImportError:
        config = None

    kwargs: dict[str, Any] = {
        "durable": durable,
        "stream": stream,
        "config": config,
    }
    if queue_group:
        kwargs["queue"] = durable
    return await js.subscribe(SUBJECT, **kwargs)


def _node_durable() -> str:
    safe = get_node_name().replace(".", "_").replace("-", "_") or "node"
    return f"{DURABLE}_{safe}"[:128]


def _queue_durable_name(queue_group: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", queue_group).strip("_")[:48]
    digest = hashlib.sha256(queue_group.encode("utf-8")).hexdigest()[:12]
    return f"{DURABLE}_q_{safe}_{digest}"[:128]


async def _drain_partial(nc: Any, subscriptions: list[Any]) -> None:
    for sub in subscriptions:
        try:
            await sub.unsubscribe()
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


def _decode_payload(data: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    try:
        raw = data if isinstance(data, str) else bytes(data).decode("utf-8")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoisonMessageError("webhooks outbox nats payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("webhooks outbox nats payload must be a JSON object")
    return payload


def _valid_payload(payload: Mapping[str, Any]) -> bool:
    required = ("event_id", "delivery_id", "event_type", "payload_hash", "outbox_table")
    if not all(isinstance(payload.get(field), str) and payload.get(field) for field in required):
        return False
    return payload.get("outbox_table") == "webhook_deliveries"


async def _ack(msg: Any) -> None:
    ack = getattr(msg, "ack", None)
    if ack is None:
        return
    result = ack()
    if hasattr(result, "__await__"):
        await result


async def _ack_safely(msg: Any) -> None:
    try:
        await _ack(msg)
    except Exception as exc:
        logger.warning("webhooks outbox nats poison-ack failed (will be redelivered): %s", exc)


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return exc.__class__.__name__ == "TimeoutError" and exc.__class__.__module__.startswith("nats")


async def main() -> None:
    import asyncpg as _asyncpg

    from mnemos.core.config import PG_CONFIG as _PG_CONFIG
    from mnemos.core.pool import wrap_pool_with_timeout

    raw_pool = await _asyncpg.create_pool(
        min_size=1,
        max_size=3,
        command_timeout=60,
        user=_PG_CONFIG["user"],
        password=_PG_CONFIG["password"],
        database=_PG_CONFIG["database"],
        host=_PG_CONFIG["host"],
        port=_PG_CONFIG["port"],
    )
    pool = wrap_pool_with_timeout(raw_pool)
    try:
        await consumer_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

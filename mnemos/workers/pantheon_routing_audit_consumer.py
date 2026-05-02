"""Optional NATS consumer for PANTHEON routing audit events."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping

from mnemos.core.config import Settings, get_settings
from mnemos.domain.pantheon.routing_log import PANTHEON_ROUTING_SUBJECT
from mnemos.nats.backoff import ReconnectBackoff

logger = logging.getLogger("mnemos.workers.pantheon_routing_audit_consumer")

STREAM = "MNEMOS_PANTHEON"
DURABLE = "mnemos_pantheon_routing_audit"

_INSERT_SQL = """
INSERT INTO pantheon_routing_audit
       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
"""


class PoisonMessageError(ValueError):
    """Message cannot be decoded into the PANTHEON routing audit shape."""


async def consumer_loop(
    pool: Any,
    *,
    settings: Settings | None = None,
    retry_seconds: float = 30.0,
    connect: Callable[[Settings], Awaitable[Any | None]] | None = None,
) -> None:
    """Consume PANTHEON routing events until cancelled."""
    settings = settings or get_settings()
    if not settings.nats.audit_consumer_enabled:
        logger.info("PANTHEON routing audit consumer disabled")
        return
    if not settings.nats.url:
        logger.info("PANTHEON routing audit consumer disabled (MNEMOS_NATS_URL unset)")
        return

    connect = connect or _connect
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
            logger.info("PANTHEON routing audit consumer connected subject=%s", PANTHEON_ROUTING_SUBJECT)
            sub = await _subscribe(js)
            backoff.reset()
            sub_owned = sub
            sub = None
            try:
                await _consume_subscription(pool, sub_owned)
            finally:
                await _drain_partial(nc, [sub_owned])
                nc = None
        except asyncio.CancelledError:
            logger.info("PANTHEON routing audit consumer cancelled")
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            raise
        except Exception as exc:
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            delay = backoff.next_delay()
            logger.warning(
                "PANTHEON routing audit consumer unavailable: %s; retrying in %.1fs",
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def handle_message(pool: Any, msg: Any) -> None:
    """Persist one PANTHEON routing event from NATS into the audit table."""
    event = _decode_payload(getattr(msg, "data", b""))
    await insert_audit_event(pool, event)


async def insert_audit_event(pool: Any, event: Mapping[str, Any]) -> None:
    """Insert a decoded routing event into ``pantheon_routing_audit``."""
    payload_json = json.dumps(event, sort_keys=True, default=str, separators=(",", ":"))
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            _text_field(event, "request_id"),
            _text_field(event, "tenant_user_id"),
            _text_field(event, "alias_or_model"),
            _text_field(event, "resolved_to"),
            _text_field(event, "outcome"),
            _int_field(event, "latency_ms"),
            _int_field(event, "tokens_in"),
            _int_field(event, "tokens_out"),
            _decimal_field(event, "cost_usd"),
            _text_field(event, "error_class"),
            payload_json,
        )


async def _consume_subscription(pool: Any, sub: Any) -> None:
    while True:
        msg = None
        try:
            msg = await sub.next_msg(timeout=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception("PANTHEON routing audit receive error (escaping for reconnect): %s", exc)
            raise

        try:
            await handle_message(pool, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "PANTHEON routing audit poison message subject=%s detail=%s",
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg)
            continue
        except Exception as exc:
            logger.exception(
                "PANTHEON routing audit handler error (subscription stays alive, no ack): %s",
                exc,
            )
            continue

        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("PANTHEON routing audit ack error (escaping for reconnect): %s", exc)
            raise


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
        PANTHEON_ROUTING_SUBJECT,
        durable=DURABLE,
        stream=STREAM,
        config=config,
    )


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


def _decode_payload(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise PoisonMessageError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("payload must be a JSON object")
    return payload


def _field(event: Mapping[str, Any], key: str) -> Any:
    if key in event:
        return event.get(key)
    metadata = event.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None


def _text_field(event: Mapping[str, Any], key: str) -> str | None:
    value = _field(event, key)
    if value is None:
        return None
    return str(value)


def _int_field(event: Mapping[str, Any], key: str) -> int | None:
    value = _field(event, key)
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _decimal_field(event: Mapping[str, Any], key: str) -> Decimal | None:
    value = _field(event, key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_timeout(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    return isinstance(exc, asyncio.TimeoutError) or "timeout" in name


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
        logger.warning("PANTHEON routing audit poison-ack failed (will be redelivered): %s", exc)


async def main() -> None:
    import asyncpg

    from mnemos.core.config import PG_CONFIG as _PG_CONFIG
    from mnemos.core.pool import wrap_pool_with_timeout

    raw_pool = await asyncpg.create_pool(
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

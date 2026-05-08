"""NATS v0.3 consumer for federation memory upserts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping

import asyncpg

from mnemos.core.config import Settings, get_settings
from mnemos.core.extras import is_extra_installed
from mnemos.domain.federation import _store_memories
from mnemos.nats.backoff import ReconnectBackoff
from mnemos.nats.client import get_node_name
from mnemos.persistence.nats_events import FEDERATION_MEMORY_SUBJECT_PREFIX

logger = logging.getLogger("mnemos.workers.federation_memory_nats_consumer")

SUBJECT = f"{FEDERATION_MEMORY_SUBJECT_PREFIX}.>"
STREAM = "MNEMOS_FEDERATION"
DURABLE = "mnemos_federation_memory_upsert"
QUEUE_ENV = "MNEMOS_NATS_FEDERATION_QUEUE_GROUP"


@dataclass(frozen=True)
class FederationMemoryPeer:
    name: str
    nats_url: str
    nats_token: str | None = None
    subjects: tuple[str, ...] = (SUBJECT,)
    namespace_filter: tuple[str, ...] | None = None
    category_filter: tuple[str, ...] | None = None


class PoisonMessageError(ValueError):
    """Message cannot be decoded into the federation memory upsert shape."""


def _enabled() -> bool:
    return os.environ.get("MNEMOS_NATS_FEDERATION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def configured_peers(settings: Settings | None = None) -> list[FederationMemoryPeer]:
    """Return v0.3 federation NATS peers from existing federation settings."""
    settings = settings or get_settings()
    peers: list[FederationMemoryPeer] = []
    for raw in settings.federation.nats_peers:
        name = raw.name.strip()
        url = raw.nats_url.strip()
        if not name or not url:
            logger.warning("federation memory nats peer missing name or nats_url: %r", raw)
            continue
        subjects = tuple(
            subject for subject in raw.subjects
            if subject.startswith(FEDERATION_MEMORY_SUBJECT_PREFIX)
        ) or (SUBJECT,)
        peers.append(
            FederationMemoryPeer(
                name=name,
                nats_url=url,
                nats_token=raw.nats_token,
                subjects=subjects,
                namespace_filter=tuple(raw.namespace_filter) if raw.namespace_filter else None,
                category_filter=tuple(raw.category_filter) if raw.category_filter else None,
            )
        )
    return peers


async def run_configured_consumers(
    pool: asyncpg.Pool,
    *,
    settings: Settings | None = None,
    retry_seconds: float = 30.0,
) -> None:
    """Run all configured v0.3 federation NATS consumers until cancelled."""
    if not _enabled():
        logger.info("federation memory nats consumer disabled")
        return
    settings = settings or get_settings()
    peers = configured_peers(settings)
    if not peers:
        logger.info("federation memory nats consumer disabled (MNEMOS_FEDERATION_NATS_PEERS empty)")
        return
    queue_group = (
        os.environ.get(QUEUE_ENV, "").strip()
        or settings.federation.nats_queue_group
    )
    tasks = [
        asyncio.create_task(
            consumer_loop(
                pool,
                peer,
                retry_seconds=retry_seconds,
                queue_group=queue_group,
            )
        )
        for peer in peers
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def consumer_loop(
    pool: asyncpg.Pool,
    peer: FederationMemoryPeer,
    *,
    retry_seconds: float = 30.0,
    connect: Callable[[FederationMemoryPeer], Awaitable[Any | None]] | None = None,
    queue_group: str = "",
) -> None:
    """Consume memory upserts from one upstream NATS peer."""
    if not _enabled():
        logger.info("federation memory nats consumer disabled")
        return
    if not is_extra_installed("nats"):
        logger.info("federation memory nats consumer disabled (nats extra not installed)")
        return
    connect = connect or _connect_peer
    backoff = ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)

    while True:
        nc = None
        subscriptions: list[Any] = []
        try:
            connect_result = await connect(peer)
            if isinstance(connect_result, tuple) and len(connect_result) == 2:
                nc, js = connect_result
            else:
                nc, js = None, connect_result
            if js is None:
                raise RuntimeError("NATS JetStream unavailable")
            logger.info(
                "federation memory nats consumer connected peer=%s subjects=%s",
                peer.name,
                ",".join(peer.subjects),
            )
            subscriptions = [
                await _subscribe(js, peer, subject, queue_group=queue_group)
                for subject in peer.subjects
            ]
            backoff.reset()
            async with _SubscriptionGroup(pool, peer, subscriptions) as group:
                subscriptions = []
                async for _ in group:
                    pass
        except asyncio.CancelledError:
            logger.info("federation memory nats consumer cancelled peer=%s", peer.name)
            await _drain_partial(nc, subscriptions)
            raise
        except Exception as exc:
            await _drain_partial(nc, subscriptions)
            delay = backoff.next_delay()
            logger.warning(
                "federation memory nats peer=%s unavailable: %s; retrying in %.1fs",
                peer.name,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def handle_message(
    pool: asyncpg.Pool,
    peer: FederationMemoryPeer,
    msg: Any,
    *,
    record_dispatch: Callable[[asyncpg.Pool, str, str], Awaitable[bool]] | None = None,
    store_memory: Callable[[asyncpg.Pool, FederationMemoryPeer, dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """Apply one direct federation memory upsert event."""
    subject = str(getattr(msg, "subject", ""))
    payload = _decode_payload(getattr(msg, "data", b""))
    if payload.get("source_node") == get_node_name():
        logger.debug("federation memory nats skipped self-loop subject=%s", subject)
        return
    memory = _memory_from_event(payload, peer)
    if not _passes_filters(peer, memory):
        logger.debug("federation memory nats filtered peer=%s memory_id=%s", peer.name, memory["id"])
        return

    event_id = _event_id(payload, subject, memory["id"])
    if record_dispatch is not None:
        if not await record_dispatch(pool, event_id, subject):
            return
        if store_memory is None:
            await _store_memory_upsert(pool, peer, memory, event_id=event_id, subject=subject, already_recorded=True)
        else:
            await store_memory(pool, peer, memory)
        return

    if store_memory is not None:
        await store_memory(pool, peer, memory)
        return
    await _store_memory_upsert(pool, peer, memory, event_id=event_id, subject=subject)


async def _store_memory_upsert(
    pool: asyncpg.Pool,
    peer: FederationMemoryPeer,
    memory: dict[str, Any],
    *,
    event_id: str,
    subject: str,
    already_recorded: bool = False,
) -> None:
    import mnemos.core.lifecycle as lifecycle
    from mnemos.persistence.postgres import PostgresBackend, _postgres_tx

    try:
        backend = lifecycle.get_persistence_backend()
    except RuntimeError:
        backend = PostgresBackend(pool, settings=None)

    async with backend.transactional() as tx:
        if not already_recorded:
            row = await _postgres_tx(tx).conn.fetchrow(
                """
                INSERT INTO nats_dispatch_log (event_id, subject)
                VALUES ($1, $2)
                ON CONFLICT (event_id, subject) DO NOTHING
                RETURNING event_id
                """,
                event_id,
                subject,
            )
            if row is None:
                logger.debug("federation memory nats duplicate event_id=%s subject=%s", event_id, subject)
                return
        await _store_memories(backend.federation, tx, peer.name, [memory])


class _SubscriptionGroup:
    def __init__(self, pool: asyncpg.Pool, peer: FederationMemoryPeer, subscriptions: Iterable[Any]):
        self.pool = pool
        self.peer = peer
        self.subscriptions = list(subscriptions)
        self.tasks: list[asyncio.Task] = []

    async def __aenter__(self):
        self.tasks = [
            asyncio.create_task(_consume_subscription(self.pool, self.peer, sub))
            for sub in self.subscriptions
        ]
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.tasks:
            raise StopAsyncIteration
        done, _pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
            self.tasks.remove(task)
        raise RuntimeError("federation memory nats subscription ended unexpectedly")


async def _consume_subscription(pool: asyncpg.Pool, peer: FederationMemoryPeer, sub: Any) -> None:
    while True:
        msg = None
        try:
            msg = await sub.next_msg(timeout=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception(
                "federation memory nats peer=%s receive error (escaping for reconnect): %s",
                peer.name,
                exc,
            )
            raise

        try:
            await handle_message(pool, peer, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "federation memory nats peer=%s poison message subject=%s detail=%s",
                peer.name,
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg, peer_label=peer.name)
            continue
        except Exception as exc:
            logger.exception(
                "federation memory nats peer=%s handler error (subscription stays alive, no ack): %s",
                peer.name,
                exc,
            )
            continue

        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "federation memory nats peer=%s ack error (escaping for reconnect): %s",
                peer.name,
                exc,
            )
            raise


async def _connect_peer(peer: FederationMemoryPeer):
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py not installed") from exc

    connect_kwargs: dict[str, Any] = {"servers": [peer.nats_url]}
    if peer.nats_token:
        connect_kwargs["token"] = peer.nats_token
    nc = await nats.connect(**connect_kwargs)
    return nc, nc.jetstream()


async def _subscribe(
    js: Any,
    peer: FederationMemoryPeer,
    subject: str,
    *,
    queue_group: str = "",
    stream: str = STREAM,
):
    if queue_group:
        durable = _queue_durable_name(queue_group, peer.name, subject)
    else:
        durable = _durable_name(peer.name, subject)
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
    return await js.subscribe(subject, **kwargs)


def _durable_name(peer_name: str, subject: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{peer_name}_{subject}").strip("_")
    return f"{DURABLE}_{safe}"[:128]


def _queue_durable_name(queue_group: str, peer_name: str, subject: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{queue_group}_{peer_name}_{subject}").strip("_")[:52]
    digest = hashlib.sha256(f"{queue_group}|{peer_name}|{subject}".encode("utf-8")).hexdigest()[:12]
    return f"{DURABLE}_q_{readable}_{digest}"[:128]


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
        raise PoisonMessageError("federation memory nats payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("federation memory nats payload must be a JSON object")
    return payload


def _memory_from_event(payload: Mapping[str, Any], peer: FederationMemoryPeer) -> dict[str, Any]:
    memory_id = payload.get("memory_id") or payload.get("id")
    if not isinstance(memory_id, str) or not memory_id:
        raise PoisonMessageError("federation memory nats event missing memory_id")
    content = payload.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise PoisonMessageError("federation memory nats content must be a string")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {**metadata, "federation_nats_source_node": payload.get("source_node")}
    return {
        "id": memory_id,
        "content": content,
        "verbatim_content": payload.get("verbatim_content") or content,
        "category": payload.get("category") or "federation",
        "subcategory": payload.get("subcategory"),
        "namespace": payload.get("namespace") or "default",
        "quality_rating": payload.get("quality_rating") or 75,
        "metadata": metadata,
        "source_model": payload.get("source_model"),
        "source_provider": payload.get("source_provider"),
        "source_session": payload.get("source_session"),
        "source_agent": payload.get("source_agent") or "federation-nats-v0.3",
        "created": payload.get("created"),
        "updated": payload.get("updated") or payload.get("created"),
        "_peer_name": peer.name,
    }


def _passes_filters(peer: FederationMemoryPeer, memory: Mapping[str, Any]) -> bool:
    if peer.namespace_filter and memory.get("namespace") not in peer.namespace_filter:
        return False
    if peer.category_filter and memory.get("category") not in peer.category_filter:
        return False
    return True


def _event_id(payload: Mapping[str, Any], subject: str, memory_id: str) -> str:
    raw = payload.get("event_id")
    if isinstance(raw, str) and raw:
        return raw
    updated = payload.get("updated") or payload.get("created") or "unknown"
    return f"{subject}:{memory_id}:{updated}"


async def _ack(msg: Any) -> None:
    ack = getattr(msg, "ack", None)
    if ack is None:
        return
    result = ack()
    if hasattr(result, "__await__"):
        await result


async def _ack_safely(msg: Any, *, peer_label: str) -> None:
    try:
        await _ack(msg)
    except Exception as exc:
        logger.warning(
            "federation memory nats peer=%s poison-ack failed (will be redelivered): %s",
            peer_label,
            exc,
        )


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
        await run_configured_consumers(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

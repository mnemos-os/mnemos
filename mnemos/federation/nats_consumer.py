"""NATS JetStream push consumer for federation memory events.

This is an additive fast path beside the existing HTTP federation pull worker.
Consumers subscribe with ``DeliverPolicy.NEW`` so process startup does not replay
the full retained stream backlog. Operators can still use the HTTP poll path for
backfill or repair if a peer was offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping

import asyncpg

from mnemos.core.config import Settings, get_settings
from mnemos.domain.federation import _store_memories, pull_memory_by_id
from mnemos.nats.backoff import ReconnectBackoff
from mnemos.nats.client import get_node_name

logger = logging.getLogger("mnemos.federation.nats_consumer")

DEFAULT_SUBJECTS = (
    "mnemos.memory.created.>",
    "mnemos.memory.updated.>",
    "mnemos.memory.deleted.>",
)
MEMORY_STREAM = "MNEMOS_MEMORY"


@dataclass(frozen=True)
class FederationNatsPeer:
    name: str
    nats_url: str
    nats_token: str | None = None
    subjects: tuple[str, ...] = DEFAULT_SUBJECTS
    base_url: str | None = None
    auth_token: str | None = None
    namespace_filter: tuple[str, ...] | None = None
    category_filter: tuple[str, ...] | None = None


def configured_nats_peers(settings: Settings | None = None) -> list[FederationNatsPeer]:
    """Return valid NATS federation peers from runtime settings."""
    settings = settings or get_settings()
    peers: list[FederationNatsPeer] = []
    for raw in settings.federation.nats_peers:
        name = raw.name.strip()
        url = raw.nats_url.strip()
        if not name or not url:
            logger.warning("federation nats peer missing name or nats_url: %r", raw)
            continue
        subjects = tuple(_expand_subject(s.strip()) for s in raw.subjects if s and s.strip())
        expanded = tuple(dict.fromkeys(item for group in subjects for item in group))
        peers.append(
            FederationNatsPeer(
                name=name,
                nats_url=url,
                nats_token=raw.nats_token,
                subjects=expanded or DEFAULT_SUBJECTS,
                base_url=raw.base_url,
                auth_token=raw.auth_token,
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
    """Run all configured peer consumers until cancelled."""
    settings = settings or get_settings()
    peers = configured_nats_peers(settings)
    if not peers:
        logger.info("federation nats consumer disabled (MNEMOS_FEDERATION_NATS_PEERS empty)")
        return

    queue_group = (settings.federation.nats_queue_group or "").strip()
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
    peer: FederationNatsPeer,
    *,
    retry_seconds: float = 30.0,
    connect: Callable[[FederationNatsPeer], Awaitable[Any]] | None = None,
    queue_group: str = "",
) -> None:
    """Connect to one upstream peer and consume memory events forever.

    Reconnect backoff is exponential with full jitter (cap at the
    legacy ``retry_seconds`` ceiling so existing tuning is honoured).
    See ``mnemos.nats.backoff.ReconnectBackoff`` — Audit Finding 8.

    ``queue_group`` (Audit Finding 5): when non-empty, all mnemos
    replicas subscribing with the same queue-group name share a single
    durable JetStream consumer per (peer, subject) and JetStream
    load-balances messages across them. Empty string preserves the
    historical single-replica shape.
    """
    connect = connect or _connect_peer
    backoff = ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)

    while True:
        nc = None
        subscriptions: list[Any] = []
        try:
            connect_result = await connect(peer)
            # Backwards compat: legacy connect callables returned the
            # JetStream context directly; new path returns (nc, js).
            if isinstance(connect_result, tuple) and len(connect_result) == 2:
                nc, js = connect_result
            else:
                nc, js = None, connect_result
            logger.info(
                "federation nats consumer connected peer=%s url=%s subjects=%s queue_group=%s",
                peer.name,
                peer.nats_url,
                ",".join(peer.subjects),
                queue_group or "(single-replica)",
            )
            subscriptions = [
                await _subscribe(js, peer, subject, queue_group=queue_group)
                for subject in peer.subjects
            ]
            # Reset AFTER all subscriptions succeed. A broker that
            # accepts the connection but fails JetStream subscribe
            # (stream drift, consumer-group recovery) would otherwise
            # reset the window every iteration and burn through a
            # tight reconnect loop instead of backing off.
            backoff.reset()
            async with _SubscriptionGroup(pool, peer, subscriptions) as group:
                # Subscriptions are now owned by the group; the
                # finally-block below should NOT double-drain them.
                subscriptions = []
                async for _ in group:
                    pass
        except asyncio.CancelledError:
            logger.info("federation nats consumer cancelled peer=%s", peer.name)
            await _drain_partial(nc, subscriptions)
            raise
        except Exception as exc:
            await _drain_partial(nc, subscriptions)
            delay = backoff.next_delay()
            logger.warning(
                "federation nats consumer peer=%s unavailable: %s; retrying in %.1fs",
                peer.name,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def _drain_partial(nc: Any, subscriptions: list[Any]) -> None:
    """Best-effort cleanup for partial connect/subscribe state.

    On a failed subscribe (e.g. second subject's subscribe errors
    after the first succeeded), unsubscribe what landed and drain
    the underlying NATS connection so we don't accumulate
    abandoned TCP sockets per retry. Errors during cleanup are
    swallowed — we are already in a failure path.
    """
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


async def handle_message(
    pool: asyncpg.Pool,
    peer: FederationNatsPeer,
    msg: Any,
    *,
    store: Callable[[Any, str, list[dict[str, Any]]], Awaitable[tuple[int, int]]] | None = None,
    delete: Callable[[asyncpg.Pool, str, str], Awaitable[int]] | None = None,
    fetch: Callable[[FederationNatsPeer, str], Awaitable[list[dict[str, Any]]]] | None = None,
) -> None:
    """Apply a single NATS memory event to local federation storage."""
    delete = delete or delete_federated_memory
    subject = getattr(msg, "subject", "")
    payload = _decode_payload(getattr(msg, "data", b""))
    source_node = payload.get("source_node")
    if source_node == get_node_name():
        logger.debug(
            "skipped self-loop event peer=%s subject=%s source_node=%s",
            peer.name,
            subject,
            source_node,
        )
        return
    memory_id = _memory_id(payload)
    logger.debug(
        "federation nats received peer=%s subject=%s memory_id=%s",
        peer.name,
        subject,
        memory_id,
    )

    if not memory_id:
        raise PoisonMessageError("federation nats event missing memory_id")

    if _is_deleted_subject(subject):
        await delete(pool, peer.name, memory_id)
        return

    fetcher = fetch or _fetch_authorized_memories
    memories = await fetcher(peer, memory_id)
    if not memories:
        logger.info(
            "federation nats peer=%s memory_id=%s not returned by authorized feed",
            peer.name,
            memory_id,
        )
        return
    if store is not None:
        async with pool.acquire() as conn:
            await store(conn, peer.name, memories)
        return

    import mnemos.core.lifecycle as lifecycle

    backend = lifecycle.get_persistence_backend()
    async with backend.transactional() as tx:
        await _store_memories(backend.federation, tx, peer.name, memories)


async def _fetch_authorized_memories(
    peer: FederationNatsPeer,
    memory_id: str,
) -> list[dict[str, Any]]:
    if not peer.base_url or not peer.auth_token:
        logger.warning(
            "federation nats peer=%s missing base_url/auth_token; nudge ignored until HTTP poll",
            peer.name,
        )
        return []
    return await pull_memory_by_id(
        peer.base_url,
        peer.auth_token,
        memory_id,
        list(peer.namespace_filter) if peer.namespace_filter else None,
        list(peer.category_filter) if peer.category_filter else None,
    )


async def delete_federated_memory(pool: asyncpg.Pool, peer_name: str, memory_id: str) -> int:
    """Hard-delete a federated row matching the poll-path local id shape."""
    import mnemos.core.lifecycle as lifecycle

    try:
        backend = lifecycle.get_persistence_backend()
    except RuntimeError:
        from mnemos.persistence.postgres import PostgresBackend

        backend = PostgresBackend(pool, settings=None)

    async with backend.transactional() as tx:
        deleted = await backend.federation.delete_federated_memory(tx, peer_name, memory_id)
    return int(deleted)


async def _connect_peer(peer: FederationNatsPeer):
    """Open a NATS connection + JetStream context for one peer.

    Returns ``(nc, js)`` so the caller can drain ``nc`` if subscribe
    fails before the consume group is active. Without surfacing
    ``nc``, every failed subscribe leaked one TCP connection per
    retry — Audit Finding from v4.2.0a6 round-2 codex.
    """
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
    peer: FederationNatsPeer,
    subject: str,
    *,
    queue_group: str = "",
):
    """Subscribe to one (peer, subject) push stream.

    Single-replica (default, ``queue_group`` empty)
        Durable: ``mnemos_federation_<peer>_<subject>``. One subscriber.

    Multi-replica (``queue_group`` non-empty)
        Durable: ``mnemos_federation_q_<group>_<peer>_<subject>``.
        ``durable``/``queue``/``deliver_group`` all the same string,
        which is what nats-py's ``js.subscribe`` requires (it treats
        the queue name AS the durable name and rejects mismatched
        values). The ``_q_<group>_`` prefix gives a distinct namespace
        from the legacy durable so legacy a7-shape replicas and
        a8-queue-mode replicas can coexist on the same broker
        without colliding on the same consumer object.

    See Audit Finding 5; the rollout caveat (legacy + queue-mode
    durables receive duplicate copies of every event during the
    transition window — federation persistence handles this via
    ON CONFLICT idempotency) is documented in NATS_OPERATIONS.md.
    """
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

    subscribe_kwargs: dict[str, Any] = dict(
        durable=durable,
        stream=MEMORY_STREAM,
        config=config,
    )
    if queue_group:
        # nats-py: queue MUST equal durable. Forcing equality here
        # also ensures the deliver_group on the consumer matches the
        # subscriber's queue, so binds across replicas line up.
        subscribe_kwargs["queue"] = durable

    return await js.subscribe(subject, **subscribe_kwargs)


class _SubscriptionGroup:
    def __init__(self, pool: asyncpg.Pool, peer: FederationNatsPeer, subscriptions: Iterable[Any]):
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
        raise RuntimeError("federation nats subscription ended unexpectedly")


async def _consume_subscription(pool: asyncpg.Pool, peer: FederationNatsPeer, sub: Any) -> None:
    """Drive a single subscription's receive/handle/ack lifecycle.

    Three failure scopes, deliberately separated:

    1. **Receive** (``sub.next_msg``) — a non-timeout failure here is a
       NATS-connection issue (broker shutdown, durable deletion, etc.).
       Escapes for ``consumer_loop`` to drain + reconnect with backoff.
    2. **Handle** (``handle_message``) — ANY failure is local. Could be
       an HTTP backfill 401, a peer RuntimeError, an asyncpg.PostgresError
       from the local pool, an asyncpg.InterfaceError from a closed
       connection — none of these mean the NATS subscription is broken,
       so a per-peer reconnect would just pause unrelated subjects behind
       backoff. Don't ack; JetStream redelivers after ack-wait.
    3. **Ack** (``_ack``) — a failure here is also a NATS-connection
       issue (the broker is what we're acking to). Escape for reconnect.

    See v4.2.0a7 round-3 audit (codex finding 2026-05-01).
    """
    received = 0
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
                "federation nats peer=%s receive error (escaping for reconnect): %s",
                peer.name,
                exc,
            )
            raise

        # Scope 2: handle (all failures stay local — don't ack,
        # JetStream redelivers after ack-wait).
        try:
            await handle_message(pool, peer, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "federation nats peer=%s poison message subject=%s detail=%s",
                peer.name,
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg, peer_label=peer.name)
            continue
        except Exception as exc:
            logger.exception(
                "federation nats peer=%s handler error (subscription stays alive, no ack): %s",
                peer.name,
                exc,
            )
            continue

        # Scope 3: ack (failure here is a NATS issue → reconnect).
        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "federation nats peer=%s ack error (escaping for reconnect): %s",
                peer.name,
                exc,
            )
            raise

        received += 1
        if received % 100 == 0:
            logger.info(
                "federation nats peer=%s subject=%s received=%d events",
                peer.name,
                getattr(msg, "subject", "?"),
                received,
            )


async def _ack_safely(msg: Any, *, peer_label: str) -> None:
    try:
        await _ack(msg)
    except Exception as exc:  # noqa: BLE001 — best-effort poison ack
        logger.warning(
            "federation nats peer=%s poison-ack failed (will be redelivered): %s",
            peer_label,
            exc,
        )


async def _ack(msg: Any) -> None:
    ack = getattr(msg, "ack", None)
    if ack is None:
        return
    result = ack()
    if hasattr(result, "__await__"):
        await result


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
        raise PoisonMessageError("federation nats payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("federation nats payload must be a JSON object")
    return payload


def _memory_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("memory_id") or payload.get("id")
    return value if isinstance(value, str) and value else None


def _memory_from_event(payload: Mapping[str, Any], peer_name: str, memory_id: str) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {**metadata, "fed_origin": peer_name}
    return {
        "id": memory_id,
        "content": payload.get("content") or "",
        "verbatim_content": payload.get("verbatim_content") or payload.get("content") or "",
        "category": payload.get("category") or "federation",
        "subcategory": payload.get("subcategory"),
        "namespace": payload.get("namespace") or "default",
        "quality_rating": payload.get("quality_rating") or 75,
        "metadata": metadata,
        "source_model": payload.get("source_model"),
        "source_provider": payload.get("source_provider"),
        "source_session": payload.get("source_session"),
        "source_agent": "federation-nats",
        "created": payload.get("created"),
        "updated": payload.get("updated") or payload.get("created"),
    }


def _durable_name(peer_name: str, subject: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{peer_name}_{subject}").strip("_")
    return f"mnemos_federation_{safe}"[:128]


def _queue_durable_name(queue_group: str, peer_name: str, subject: str) -> str:
    """Durable name for queue-group mode.

    Distinct namespace from :func:`_durable_name` so legacy single-
    replica durables and queue-mode durables can coexist on the same
    broker during a partial-fleet rollout.

    JetStream durable names are bounded at 128 chars; long
    ``queue_group`` + ``peer_name`` + ``subject`` combinations could
    push past that in a naive concat. The readable parts get capped,
    and a 12-char SHA-256 hash of the FULL untruncated triple is
    appended — two distinct triples can never share a durable, even
    if their readable prefixes are identical.
    """
    import hashlib

    group_safe = re.sub(r"[^A-Za-z0-9_-]+", "_", queue_group).strip("_")[:32]
    rest = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{peer_name}_{subject}").strip("_")[:48]
    digest = hashlib.sha256(
        f"{queue_group}|{peer_name}|{subject}".encode("utf-8")
    ).hexdigest()[:12]
    name = f"mnemos_federation_q_{group_safe}_{rest}_{digest}"
    if len(name) > 128:
        # Worst case: somebody passes pathological input. Always
        # preserve the leading namespace and the trailing hash; drop
        # readable middle to fit. The hash is what makes the durable
        # unique — readability is a debugging convenience.
        prefix = "mnemos_federation_q_"
        suffix = f"_{digest}"
        room = 128 - len(prefix) - len(suffix)
        middle = (group_safe + "_" + rest)[:room]
        name = f"{prefix}{middle}{suffix}"
    return name


def _expand_subject(subject: str) -> tuple[str, ...]:
    if subject == "mnemos.memory.>":
        return DEFAULT_SUBJECTS
    return (subject,)


def _is_deleted_subject(subject: str) -> bool:
    return subject.startswith("mnemos.memory.deleted.") or subject == "mnemos.memory.deleted"


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return exc.__class__.__name__ == "TimeoutError" and exc.__class__.__module__.startswith("nats")

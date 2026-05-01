"""API-owned lifespan integrations for domain, webhook, and worker packages."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from mnemos.api.dependencies import configure_auth
from mnemos.core import lifecycle

logger = logging.getLogger(__name__)
_registered = False


async def _reload_provider_manifest(pool: Any) -> None:
    from mnemos.domain.graeae.engine import get_graeae_engine

    await get_graeae_engine().reload_from_registry(pool)


async def _run_distillation_worker(_pool: Any) -> None:
    """Supervise the distillation worker loop with bounded restart backoff."""
    try:
        from mnemos.workers.distillation import MemoryDistillationWorker
    except ImportError as e:
        logger.warning(f"Distillation worker not available: {e}")
        lifecycle._worker_status["distillation_worker"] = "unavailable"
        return

    backoff = 1.0
    while True:
        worker = MemoryDistillationWorker()
        try:
            lifecycle._worker_status["distillation_worker"] = "starting"
            await worker.start()
            lifecycle._worker_status["distillation_worker"] = "idle"
            return
        except asyncio.CancelledError:
            logger.info("Distillation worker cancelled (shutdown)")
            lifecycle._worker_status["distillation_worker"] = "idle"
            raise
        except Exception as e:
            lifecycle._worker_status["distillation_worker"] = "error"
            logger.exception(f"Distillation worker crashed: {e} - restarting in {backoff:.0f}s")
        finally:
            try:
                if getattr(worker, "db_pool", None):
                    await worker.db_pool.close()
            except Exception:
                pass
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            lifecycle._worker_status["distillation_worker"] = "idle"
            raise
        backoff = min(backoff * 2, 300.0)


def _webhook_repair_worker(pool: Any):
    from mnemos.webhooks import repair_worker_loop

    return repair_worker_loop(pool)


def _webhook_delivery_worker(pool: Any):
    from mnemos.webhooks import delivery_worker_loop

    return delivery_worker_loop(pool)


def _federation_sync_worker(pool: Any):
    from mnemos.domain.federation import federation_worker_loop

    return federation_worker_loop(pool)


async def _federation_nats_post_db_hook(pool: Any, settings: Any) -> None:
    """Launch one federation NATS consumer per configured peer.

    Optional and additive: HTTP federation polling remains active for
    backfill and safety regardless of NATS availability.

    Audit Finding 9 (handoff queue #3): warn at boot if peers are
    configured but ``MNEMOS_NODE_NAME`` is unset. Without an
    explicit name the source-node tag falls back to
    ``socket.gethostname()``, which is fine for a single host but
    can collide on identical container hostnames across a federation
    and cause loop-back filtering to mis-fire. Operators with peers
    really want a stable, unique node name.
    """
    from mnemos.federation.nats_consumer import (
        configured_nats_peers,
        consumer_loop,
    )

    peers = list(configured_nats_peers(settings))
    if peers and not settings.nats.node_name.strip():
        logger.warning(
            "[NATS] %d federation peer(s) configured but MNEMOS_NODE_NAME is unset; "
            "falling back to hostname for source_node tagging. Set MNEMOS_NODE_NAME "
            "to a stable, deployment-unique value to avoid loop-back filter misses "
            "if peer hostnames collide.",
            len(peers),
        )

    queue_group = (settings.federation.nats_queue_group or "").strip()
    if queue_group:
        logger.info(
            "[NATS] federation queue group enabled: queue_group=%s "
            "(JetStream load-balances messages across replicas in this group)",
            queue_group,
        )

    for peer in peers:
        logger.info("Launching federation nats consumer for peer %s", peer.name)
        lifecycle.schedule_worker(consumer_loop(pool, peer, queue_group=queue_group))


async def _webhook_nats_post_db_hook(pool: Any, settings: Any) -> None:
    """Launch the webhook NATS push trigger.

    Optional and additive: the polling recovery worker remains the
    durable fallback path regardless of NATS availability.
    """
    from mnemos.webhooks.nats_trigger import consumer_loop as webhook_nats_trigger_loop

    logger.info("Launching webhook nats trigger consumer")
    lifecycle.schedule_worker(webhook_nats_trigger_loop(pool, settings=settings))


def register_lifespan_hooks() -> None:
    """Register high-level integrations once per process."""
    global _registered
    if _registered:
        return
    lifecycle.register_auth_configurer(configure_auth)
    lifecycle.register_provider_manifest_reloader(_reload_provider_manifest)
    lifecycle.register_lifespan_worker(
        "distillation_worker",
        _run_distillation_worker,
        honor_worker_enabled=True,
    )
    lifecycle.register_lifespan_worker("webhook retry repair worker", _webhook_repair_worker)
    lifecycle.register_lifespan_worker("webhook delivery recovery worker", _webhook_delivery_worker)
    lifecycle.register_lifespan_worker("federation sync worker", _federation_sync_worker)
    lifecycle.register_post_db_startup_hook(
        "federation nats consumers", _federation_nats_post_db_hook
    )
    lifecycle.register_post_db_startup_hook(
        "webhook nats trigger", _webhook_nats_post_db_hook
    )
    _registered = True

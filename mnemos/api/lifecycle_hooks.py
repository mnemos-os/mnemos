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
    _registered = True

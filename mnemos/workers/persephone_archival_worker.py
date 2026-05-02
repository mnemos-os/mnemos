"""Optional PERSEPHONE archival worker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mnemos.core.config import get_settings
from mnemos.domain.persephone.runner import sweep_for_archival

logger = logging.getLogger(__name__)


async def persephone_archival_worker_loop(pool: Any) -> None:
    """Run periodic PERSEPHONE archival sweeps when explicitly enabled."""
    settings = get_settings().persephone
    if not settings.enabled:
        logger.info("PERSEPHONE archival worker disabled")
        return

    logger.info(
        "PERSEPHONE archival worker enabled namespace=%s archive_after_days=%d batch_size=%d interval=%.1fs",
        settings.namespace,
        settings.archive_after_days,
        settings.batch_size,
        settings.check_interval_seconds,
    )
    while True:
        try:
            archived = await sweep_for_archival(
                pool,
                namespace=settings.namespace,
                archive_after_days=settings.archive_after_days,
                batch_size=settings.batch_size,
            )
            if archived:
                logger.info("PERSEPHONE archival sweep archived %d row(s)", archived)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PERSEPHONE archival sweep failed")
        await asyncio.sleep(settings.check_interval_seconds)


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
        await persephone_archival_worker_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

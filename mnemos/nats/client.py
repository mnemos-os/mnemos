"""NATS JetStream connection management for MNEMOS.

Single process-global JetStream context. Lifecycle bootstrap calls
``connect_nats`` on startup; routes that publish events look up the
context via ``get_jetstream``.

Stream declarations live here (alongside the connection) because the
shape of subjects + retention is part of the bus contract: bumping
either is a coordinated change with consumers.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("mnemos.nats")

_jetstream = None  # type: ignore[assignment]


def get_jetstream():
    """Return the live JetStream context, or None if NATS is disabled."""
    return _jetstream


async def connect_nats(url: Optional[str], token: Optional[str]):
    """Open a NATS connection + JetStream context. Returns None on failure."""
    global _jetstream
    if not url:
        logger.info("NATS disabled (MNEMOS_NATS_URL unset)")
        return None
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("nats-py not installed; NATS publishing disabled")
        return None

    try:
        connect_kwargs = {"servers": [url]}
        if token:
            connect_kwargs["token"] = token
        nc = await nats.connect(**connect_kwargs)
        js = nc.jetstream()
    except Exception as exc:
        logger.warning("NATS connect to %s failed: %s — publishing disabled", url, exc)
        return None

    await ensure_streams(js)
    _jetstream = js
    logger.info("NATS connected to %s, JetStream context ready", url)
    return js


async def ensure_streams(js) -> None:
    """Idempotently declare the streams we publish to.

    Subjects:
      mnemos.memory.created.<namespace>
      mnemos.memory.updated.<namespace>
      mnemos.memory.deleted.<namespace>

    Retention: file-backed, 30 days, max 10 GB. Per-message TTL not
    set; consumers can replay within the retention window.
    """
    try:
        from nats.js.api import StreamConfig, RetentionPolicy, StorageType  # type: ignore
    except ImportError:
        return

    # nats-py StreamConfig expects max_age + duplicate_window in
    # seconds (it multiplies by 1e9 internally to nanoseconds).
    streams = [
        StreamConfig(
            name="MNEMOS_MEMORY",
            subjects=["mnemos.memory.>"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=30 * 24 * 60 * 60,  # 30 days in seconds
            max_bytes=10 * 1024**3,
            duplicate_window=2 * 60,  # 2 minute dedup window
        ),
    ]
    for cfg in streams:
        try:
            await js.add_stream(config=cfg)
            logger.info("NATS stream %s declared", cfg.name)
        except Exception as exc:
            # add_stream is idempotent for matching configs; mismatched
            # configs raise. Log and continue — operator must intervene.
            msg = str(exc)
            if "already in use" in msg or "stream name already" in msg.lower():
                logger.debug("NATS stream %s already exists", cfg.name)
            else:
                logger.warning("NATS stream %s declaration error: %s", cfg.name, exc)

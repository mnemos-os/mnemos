"""NATS JetStream connection management for MNEMOS.

Single process-global JetStream context. Lifecycle bootstrap calls
``connect_nats`` on startup; routes that publish events look up the
context via ``get_jetstream``.

All publish payloads carry ``source_node`` metadata. Loop prevention is the
consumer's responsibility: consumers must skip events whose ``source_node``
matches the local node name.

Stream declarations live here (alongside the connection) because the
shape of subjects + retention is part of the bus contract: bumping
either is a coordinated change with consumers.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

from mnemos.core.config import get_settings

logger = logging.getLogger("mnemos.nats")

_jetstream = None  # type: ignore[assignment]
_publishing_enabled = False


def get_jetstream():
    """Return the live JetStream context, or None if NATS is disabled."""
    return _jetstream


def publishing_enabled() -> bool:
    """Return whether startup verified all required streams for publishing."""
    return _jetstream is not None and _publishing_enabled


def get_node_name() -> str:
    """Return the resolved local NATS node name."""
    settings = get_settings()
    node_name = settings.nats.node_name.strip()
    if not node_name:
        node_name = socket.gethostname()
        settings.nats.node_name = node_name
    return node_name


async def connect_nats(url: Optional[str], token: Optional[str]):
    """Open a NATS connection + JetStream context. Returns None on failure."""
    global _jetstream, _publishing_enabled
    _jetstream = None
    _publishing_enabled = False
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

    streams_ready = await ensure_streams(js)
    if not streams_ready:
        logger.error(
            "NATS connected to %s but required streams were not verified; publishing disabled",
            url,
        )
        return None
    _jetstream = js
    _publishing_enabled = True
    logger.info("NATS connected to %s, JetStream context ready", url)
    return js


async def ensure_streams(js) -> bool:
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
        logger.error("nats-py JetStream API unavailable; NATS publishing disabled")
        return False

    # nats-py StreamConfig expects max_age + duplicate_window in
    # seconds (it multiplies by 1e9 internally to nanoseconds).
    def _stream_config(name: str, subjects: list[str], max_bytes: int = 10 * 1024**3):
        return StreamConfig(
            name=name,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=30 * 24 * 60 * 60,  # 30 days in seconds
            max_bytes=max_bytes,
            duplicate_window=2 * 60,  # 2 minute dedup window
        )

    streams = [
        _stream_config("MNEMOS_MEMORY", ["mnemos.memory.>"]),
        _stream_config("MNEMOS_CONSULTATION", ["mnemos.consultation.>"]),
        _stream_config("MNEMOS_WEBHOOK", ["mnemos.webhook.>"]),
    ]
    # Lazy-import the structured error class so the absence of the
    # nats-py JetStream API at import-time doesn't crash this module.
    try:
        from nats.js.errors import BadRequestError  # type: ignore
    except ImportError:
        BadRequestError = type("BadRequestError", (Exception,), {})  # type: ignore[misc, assignment]

    for cfg in streams:
        try:
            await js.add_stream(config=cfg)
            logger.info("NATS stream %s declared", cfg.name)
            continue
        except Exception as caught:
            exc: Exception = caught

        msg = str(exc)
        if "10047" in msg or "insufficient storage resources" in msg.lower():
            # Fresh declare: broker can't fit the canonical 10 GiB.
            # Fall back to a 1 GiB stream so a small dev/test broker
            # can still bootstrap.
            try:
                await js.add_stream(
                    config=_stream_config(cfg.name, list(cfg.subjects), 1024**3)
                )
                logger.info("NATS stream %s declared with 1GB max_bytes", cfg.name)
                continue
            except Exception as fallback_exc:
                logger.error(
                    "NATS stream %s declaration failed (canonical + fallback both rejected): %s",
                    cfg.name,
                    fallback_exc,
                )
                return False

        # add_stream raised. nats-py 2.14 silently accepts a TRUE
        # matching-config redeclare (no raise); BadRequestError /
        # "already in use" only appears when the existing config
        # diverges. So we know: existing stream is NOT cfg.
        #
        # Distinguish the two acceptable diverging cases from real
        # drift by re-trying the SAME stream with the fallback
        # config. If that one is also accepted silently, the
        # existing stream IS the 1 GiB fallback we ourselves
        # create; publishing is safe. If it ALSO raises, neither
        # canonical nor fallback matches → real drift, fail closed.
        #
        # Crucially, this approach uses the broker's full config
        # comparator (which knows about every field — max_msg_size,
        # replicas, discard, no_ack, ...) instead of our partial
        # field-by-field drift detector. Codex rounds 6/7/8.
        looks_like_existing = (
            isinstance(exc, BadRequestError)
            or "already in use" in msg
            or "stream name already" in msg.lower()
        )
        if not looks_like_existing:
            logger.error("NATS stream %s declaration error: %s", cfg.name, exc)
            return False

        try:
            await js.add_stream(
                config=_stream_config(cfg.name, list(cfg.subjects), 1024**3)
            )
            # Fallback config matched silently → existing stream IS
            # the 1 GiB fallback. Publishing is safe.
            logger.info(
                "NATS stream %s running on 1 GiB fallback path (configured "
                "max_bytes=%s could not be applied to the running stream). "
                "Publishing stays enabled. Operator can grow the stream "
                "out-of-band with `nats stream update %s` when storage is "
                "available.",
                cfg.name,
                cfg.max_bytes,
                cfg.name,
            )
            continue
        except Exception:
            # Neither canonical nor fallback config matches the
            # running stream → real drift. Run the drift detector
            # for an operator-facing diagnostic log of the SHAPE of
            # drift we can see, then fail closed regardless of what
            # it returns.
            drift = await _stream_config_drift(js, cfg)
            logger.error(
                "NATS stream %s config drift detected (running stream "
                "matches neither the canonical nor the 1 GiB fallback "
                "config). Visible drift fields: %s. Operator must "
                "`nats stream update %s` or delete+recreate to apply "
                "the new config; field list above is partial — broker "
                "may also differ on un-compared fields.",
                cfg.name,
                drift or "(none in compared dimensions; see broker)",
                cfg.name,
            )
            return False
    return True


def _normalize_enum_value(v):
    """Normalize a nats-py enum-or-string field for cross-version
    comparison. Newer nats-py returns enum objects; older code paths
    sometimes pass plain strings. Compare on uppercase string form."""
    if v is None:
        return None
    val = getattr(v, "value", v)  # enum.value on enums; str otherwise
    return str(val).upper()


async def _stream_config_drift(js, desired) -> dict[str, tuple]:
    """Compare desired StreamConfig vs the running stream's actual config.

    Returns a dict of ``{field: (running_value, desired_value)}`` for
    every operator-facing retention dimension that has drifted. Empty
    dict means configs match — safe to treat the redeclare as
    idempotent.

    Surface compared:
      * subjects
      * max_age
      * max_bytes
      * duplicate_window
      * retention (LIMITS / WORK_QUEUE / INTEREST)
      * storage   (FILE / MEMORY)

    Internal nats-py defaults that vary across versions (replicas,
    discard policy, etc.) are NOT part of the drift surface.
    """
    try:
        info = await js.stream_info(desired.name)
    except Exception as exc:
        # stream_info itself failed — we can't tell drift from
        # "broker is unavailable now"; surface as drift so the caller
        # logs and bails rather than silently continuing.
        return {"_stream_info_error": (str(exc), None)}

    current = info.config
    drift: dict[str, tuple] = {}

    # Numeric retention fields — float-tolerant compare for the
    # seconds-encoded ones (max_age / duplicate_window read back as
    # float from nats-py 2.14's StreamInfo).
    for f in ("max_age", "max_bytes", "duplicate_window"):
        cur = getattr(current, f, None)
        des = getattr(desired, f, None)
        if cur is None or des is None:
            continue
        if isinstance(cur, float) or isinstance(des, float):
            if abs(float(cur) - float(des)) > 1e-3:
                drift[f] = (cur, des)
        else:
            if cur != des:
                drift[f] = (cur, des)

    # Subjects — list compare.
    if list(getattr(current, "subjects", []) or []) != list(getattr(desired, "subjects", []) or []):
        drift["subjects"] = (current.subjects, desired.subjects)

    # Retention + storage policies — enum-or-string normalized.
    for f in ("retention", "storage"):
        cur = _normalize_enum_value(getattr(current, f, None))
        des = _normalize_enum_value(getattr(desired, f, None))
        if cur is None or des is None:
            continue
        if cur != des:
            drift[f] = (cur, des)

    return drift

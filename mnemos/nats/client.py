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
        exc: Exception | None = None
        try:
            await js.add_stream(config=cfg)
            logger.info("NATS stream %s declared", cfg.name)
            continue
        except Exception as caught:
            exc = caught

        assert exc is not None  # the only path here is through the except above
        msg = str(exc)
        if "10047" in msg or "insufficient storage resources" in msg.lower():
            try:
                await js.add_stream(
                    config=_stream_config(cfg.name, list(cfg.subjects), 1024**3)
                )
                logger.info("NATS stream %s declared with 1GB max_bytes", cfg.name)
                continue
            except Exception as fallback_exc:
                exc = fallback_exc
                msg = str(fallback_exc)

        # add_stream is idempotent for matching configs and raises
        # BadRequestError for mismatched configs. Both paths can
        # surface text like "already in use" — so disambiguate via
        # the structured exception class first, falling back to
        # message text for older nats-py releases. We then call
        # stream_info to compare the running config against `cfg`
        # and log field-level drift.
        # Audit Finding 11 / codex rounds 5 + 6 (2026-05-01).
        looks_like_existing = (
            isinstance(exc, BadRequestError)
            or "already in use" in msg
            or "stream name already" in msg.lower()
        )
        if looks_like_existing:
            drift = await _stream_config_drift(js, cfg)
            if not drift:
                logger.debug("NATS stream %s already exists (config matches)", cfg.name)
                continue
            # Special-case: if the ONLY drift is max_bytes AND the
            # running stream is smaller than what we asked for, the
            # broker took the documented insufficient-storage
            # fallback on a previous boot. Don't disable publishing
            # — operator can grow the stream out-of-band when they
            # have the space. Log info so it's visible.
            if (
                set(drift.keys()) == {"max_bytes"}
                and isinstance(drift["max_bytes"][0], (int, float))
                and isinstance(drift["max_bytes"][1], (int, float))
                and drift["max_bytes"][0] < drift["max_bytes"][1]
            ):
                logger.info(
                    "NATS stream %s running on smaller max_bytes than requested "
                    "(running=%s, requested=%s) — likely the documented "
                    "insufficient-storage fallback path. Publishing stays enabled.",
                    cfg.name,
                    drift["max_bytes"][0],
                    drift["max_bytes"][1],
                )
                continue
            logger.error(
                "NATS stream %s config drift detected; broker keeps OLD "
                "config. Drift: %s. Operator must `nats stream update %s` "
                "or delete+recreate to apply the new config.",
                cfg.name,
                drift,
                cfg.name,
            )
            return False
        else:
            logger.error("NATS stream %s declaration error: %s", cfg.name, exc)
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

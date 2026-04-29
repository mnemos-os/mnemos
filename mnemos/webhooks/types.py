"""Shared webhook delivery types, constants, and send concurrency state."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

BACKOFF_SCHEDULE = [60, 300, 1800, 7200]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE)  # = 4
DNS_TIMEOUT = float(os.getenv("WEBHOOK_DNS_TIMEOUT", "10.0"))
DELIVERY_TIMEOUT = float(os.getenv("WEBHOOK_HTTP_TIMEOUT", "10.0"))
WEBHOOK_LEASE_SECONDS = int(os.getenv(
    "WEBHOOK_LEASE_SECONDS",
    str(max(90, int(DNS_TIMEOUT + DELIVERY_TIMEOUT + 30))),
))
WEBHOOK_FINALIZE_BUFFER_SECONDS = float(os.getenv("WEBHOOK_FINALIZE_BUFFER_SECONDS", "5.0"))
WEBHOOK_RESPONSE_BODY_MAX_BYTES = int(os.getenv("WEBHOOK_RESPONSE_BODY_MAX_BYTES", "2048"))
WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS = 5.0
WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS = float(
    os.getenv("WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS", "5.0")
)
WEBHOOK_MAX_CONCURRENT_SENDS = int(os.getenv("WEBHOOK_MAX_CONCURRENT_SENDS", "64"))
NEW_CODE_WRITER_REVISION = 1
NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES = 256
MIN_SEND_WINDOW_SECONDS = 1.0
RECOVERY_POLL_INTERVAL = 30.0          # seconds between recovery-worker passes
REPAIR_BURST_SECONDS = float(os.getenv("WEBHOOK_REPAIR_BURST_SECONDS", "60.0"))
REPAIR_BURST_INTERVAL = float(os.getenv("WEBHOOK_REPAIR_BURST_INTERVAL", "5.0"))
REPAIR_PERIODIC_INTERVAL = float(os.getenv("WEBHOOK_REPAIR_PERIODIC_INTERVAL", "300.0"))
TERMINAL_DELIVERY_STATUSES = frozenset((
    "succeeded",
    "abandoned",
))
LIVE_DELIVERY_STATUSES = frozenset(("pending", "retrying"))
_send_semaphore: asyncio.Semaphore | None = None


def _derive_total_send_deadline_seconds(
    lease_seconds: int,
    finalize_buffer_seconds: float,
) -> float:
    """Derive the single wall-clock DNS+HTTP budget from the attempt lease."""
    if finalize_buffer_seconds <= 0:
        raise ValueError("WEBHOOK_FINALIZE_BUFFER_SECONDS must be positive")
    deadline = float(lease_seconds) - finalize_buffer_seconds
    if deadline <= 0:
        raise ValueError(
            "WEBHOOK_LEASE_SECONDS must be greater than WEBHOOK_FINALIZE_BUFFER_SECONDS "
            "so webhook sends leave time for finalization before lease expiry"
        )
    return deadline


if WEBHOOK_RESPONSE_BODY_MAX_BYTES <= 0:
    raise ValueError("WEBHOOK_RESPONSE_BODY_MAX_BYTES must be positive")
if WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS <= 0:
    raise ValueError("WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS must be positive")

# Keep the lease as the only operator-facing ownership budget. This derived
# value validates startup configuration; actual sends use the DB-returned claim
# timestamp pair so an app pause after claim cannot spend a stale full budget.
TOTAL_SEND_DEADLINE_SECONDS = _derive_total_send_deadline_seconds(
    WEBHOOK_LEASE_SECONDS,
    WEBHOOK_FINALIZE_BUFFER_SECONDS,
)


def _get_send_semaphore() -> asyncio.Semaphore:
    global _send_semaphore
    if _send_semaphore is None:
        _send_semaphore = asyncio.Semaphore(max(1, WEBHOOK_MAX_CONCURRENT_SENDS))
    return _send_semaphore


@dataclass(frozen=True)
class _DeliveryResult:
    succeeded: bool
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class _LeaseExpiredBeforeSend(_DeliveryResult):
    """Marker result for a lease window that elapsed before any POST began."""


@dataclass(frozen=True)
class _PostHeaderDeliveryResult(_DeliveryResult):
    """Status-code result plus open response resources for post-finalize audit work."""

    delivery_id: Any = field(default=None, repr=False, compare=False)
    response: Any = field(default=None, repr=False, compare=False)
    stream_cm: Any = field(default=None, repr=False, compare=False)
    client_cm: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _ClaimedDelivery:
    delivery: asyncpg.Record
    lease_token: str
    pre_claim_monotonic: float

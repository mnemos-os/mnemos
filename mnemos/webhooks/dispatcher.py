"""Public webhook dispatch entry points."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import types as _module_types
from typing import Any, Dict, Optional

import asyncpg
import httpx

from . import _signing as webhook_signing
from . import chain as webhook_chain
from . import finalize as webhook_finalize
from . import lease as webhook_lease
from . import repair as webhook_repair
from . import sender as webhook_sender
from . import types as webhook_types
from . import workers as webhook_workers
from .outbox import _dispatch_on_conn as _outbox_dispatch_on_conn

logger = logging.getLogger(__name__)


async def dispatch(
    event_type: str | asyncpg.Connection,
    payload: Dict[str, Any] | str,
    legacy_payload: Optional[Dict[str, Any]] = None,
    *,
    conn: Optional[asyncpg.Connection] = None,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    """Fan out an event to all matching subscriptions.

    Records a `webhook_deliveries` row per subscription, then schedules each
    delivery as a background task. When `conn` is provided, the delivery rows
    are inserted on that connection and join the caller's transaction. Without
    `conn`, the dispatcher acquires its own connection, preserving the
    stand-alone behavior used by non-transactional callers.
    """
    target_conn = conn
    resolved_event_type = event_type
    resolved_payload = payload
    if legacy_payload is not None:
        if conn is not None:
            raise TypeError("dispatch received both positional and keyword conn")
        target_conn = event_type
        resolved_event_type = payload
        resolved_payload = legacy_payload
    if not isinstance(resolved_event_type, str) or not isinstance(resolved_payload, dict):
        raise TypeError("dispatch expects dispatch(event_type, payload, *, conn=conn)")

    if target_conn is not None:
        return await _dispatch_on_conn(
            target_conn,
            resolved_event_type,
            resolved_payload,
            owner_id=owner_id,
            namespace=namespace,
        )

    from mnemos.core.lifecycle import _pool as lifecycle_pool  # noqa: WPS433
    if not lifecycle_pool:
        logger.warning("webhook dispatcher: no DB pool - skipping event %s", resolved_event_type)
        return []

    async with lifecycle_pool.acquire() as acquired_conn:
        delivery_ids = await _dispatch_on_conn(
            acquired_conn,
            resolved_event_type,
            resolved_payload,
            owner_id=owner_id,
            namespace=namespace,
        )
    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
    from .sender import _attempt_delivery

    for delivery_id in delivery_ids:
        _schedule_delivery_attempt(_attempt_delivery(str(delivery_id)))
    return delivery_ids


async def _dispatch_on_conn(
    conn: asyncpg.Connection,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    """Insert delivery intents using an already-selected connection."""
    return await _outbox_dispatch_on_conn(
        conn,
        event_type,
        payload,
        owner_id=owner_id,
        namespace=namespace,
    )

# Legacy private attributes are resolved lazily so older in-repo tests and
# debugging imports can still reach the moved implementation without putting
# state-machine code back in this public dispatcher module.
_LEGACY_MODULES = {
    "asyncio": asyncio,
    "time": time,
    "asyncpg": asyncpg,
    "httpx": httpx,
}
_LEGACY_ATTR_TARGETS: dict[str, object] = {}


def _register_legacy_attrs(target: object, names: tuple[str, ...]) -> None:
    for name in names:
        _LEGACY_ATTR_TARGETS[name] = target


_register_legacy_attrs(webhook_types, (
    "BACKOFF_SCHEDULE",
    "MAX_ATTEMPTS",
    "DNS_TIMEOUT",
    "DELIVERY_TIMEOUT",
    "WEBHOOK_LEASE_SECONDS",
    "WEBHOOK_FINALIZE_BUFFER_SECONDS",
    "WEBHOOK_RESPONSE_BODY_MAX_BYTES",
    "WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS",
    "WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS",
    "WEBHOOK_MAX_CONCURRENT_SENDS",
    "NEW_CODE_WRITER_REVISION",
    "NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES",
    "MIN_SEND_WINDOW_SECONDS",
    "RECOVERY_POLL_INTERVAL",
    "REPAIR_BURST_SECONDS",
    "REPAIR_BURST_INTERVAL",
    "REPAIR_PERIODIC_INTERVAL",
    "TERMINAL_DELIVERY_STATUSES",
    "LIVE_DELIVERY_STATUSES",
    "TOTAL_SEND_DEADLINE_SECONDS",
    "_send_semaphore",
    "_DeliveryResult",
    "_LeaseExpiredBeforeSend",
    "_PostHeaderDeliveryResult",
    "_ClaimedDelivery",
    "_derive_total_send_deadline_seconds",
    "_get_send_semaphore",
))
_register_legacy_attrs(webhook_signing, ("_sign",))
_register_legacy_attrs(webhook_workers, (
    "repair_worker_loop",
    "delivery_worker_loop",
    "recovery_worker_loop",
    "_recover_due_deliveries",
    "_semaphore_available",
    "_claim_recoverable_deliveries",
    "_recoverable_delivery_ids",
))
_register_legacy_attrs(webhook_repair, (
    "_repair_superseded_retrying_deliveries_safely",
    "repair_superseded_retrying_deliveries",
))
_register_legacy_attrs(webhook_sender, (
    "_attempt_delivery",
    "_send_claimed_delivery",
    "_send_claimed_delivery_within_deadline",
    "_cleanup_unacknowledged_send_context",
    "_run_pre_header_cleanup",
    "_run_post_header_cleanup",
    "_consume_timed_out_cleanup_result",
    "_remaining_timeout_seconds",
    "_capture_response_body_for_audit",
    "_read_capped_response_body",
    "_read_capped_raw_response_body",
    "_decode_capped_response_body",
))
_register_legacy_attrs(webhook_lease, (
    "_claim_delivery",
    "_guard_preclaimed_delivery_before_send",
    "_preclaimed_delivery_is_live_and_owned",
    "_claim_remaining_send_window_seconds",
    "_as_aware_utc",
    "_release_owned_lease_for_reclaim",
    "_clear_stale_owned_lease_after_terminal_finalize",
))
_register_legacy_attrs(webhook_finalize, (
    "_finalize_delivery",
    "_finalize_delivery_row",
    "_commit_successful_delivery_row",
    "_finalize_successful_delivery_row",
    "_abandon_live_successors_before_success_commit",
    "_abandon_success_duplicate_after_unique_violation",
    "_run_post_finalize_delivery_work",
    "_persist_response_body_for_audit",
))
_register_legacy_attrs(webhook_chain, (
    "_load_delivery_for_claim",
    "_insert_successor_delivery",
    "_lock_delivery_chain",
    "_delivery_chain_lock_key",
    "_has_successor_attempt",
    "_has_live_successor_attempt",
    "_has_succeeded_chain_attempt",
    "_abandon_owned_attempt_after_live_successor",
    "_abandon_current_attempt_after_succeeded_chain_peer",
    "_abandon_owned_attempt_after_succeeded_chain_peer",
    "_find_live_unleased_successor_attempts",
    "_abandon_live_successor_attempt",
))


def __getattr__(name: str) -> Any:
    if name in _LEGACY_MODULES:
        return _LEGACY_MODULES[name]
    target = _LEGACY_ATTR_TARGETS.get(name)
    if target is not None:
        return getattr(target, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _DispatcherModule(_module_types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        target = _LEGACY_ATTR_TARGETS.get(name)
        if target is not None:
            setattr(target, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _DispatcherModule


__all__ = [
    "dispatch",
    "_dispatch_on_conn",
]

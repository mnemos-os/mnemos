"""MCP tool audit and quota helpers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from mnemos.core.auth_context import UserContext

MCP_WRITE_RATE_LIMIT_PER_MINUTE = 60
MCP_READ_RATE_LIMIT_PER_MINUTE = 600

# Per-(principal, tool) sliding-window rate-limit buckets.
# Each deque carries the timestamps of recent calls; old timestamps
# are pruned on every touch (`_mcp_touch_bucket`).
#
# #204: until this slice the dict grew monotonically — empty/idle
# buckets were never dropped, so a high-churn principal flow (CI
# matrix, rotating tokens) leaked memory. The new
# `_gc_stale_buckets` helper runs every `_GC_SWEEP_INTERVAL` touches
# and removes any bucket whose newest timestamp is past the window
# cutoff (i.e. the principal stopped calling). A hard cap at
# `_MAX_BUCKETS` triggers an extra eviction pass that drops the
# oldest-by-last-timestamp half if the sweep alone can't shrink
# below cap.
_TOOL_RATE_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_GC_SWEEP_INTERVAL = 256  # touches between sweeps
_MAX_BUCKETS = 4096        # hard cap; eviction kicks in past this
_gc_touch_counter: int = 0

_MCP_AUDIT_LOGGER = logging.getLogger("mnemos.mcp.audit")


def _gc_stale_buckets(*, cutoff: float) -> None:
    """Drop buckets whose newest timestamp is past the cutoff (i.e.
    the principal hasn't called within the rate-limit window). Empty
    buckets are also dropped — they exist when a `defaultdict` lookup
    created them but the touch didn't append (rate-limit raised
    before the append).

    O(N) over the bucket dict; runs every `_GC_SWEEP_INTERVAL`
    touches and on cap-overflow.
    """
    stale_keys: list[tuple[str, str]] = []
    for key, bucket in _TOOL_RATE_BUCKETS.items():
        # Empty bucket → stale by construction.
        if not bucket:
            stale_keys.append(key)
            continue
        # Newest timestamp is bucket[-1] (deque is FIFO with append).
        if bucket[-1] < cutoff:
            stale_keys.append(key)
    for key in stale_keys:
        _TOOL_RATE_BUCKETS.pop(key, None)


def _evict_oldest_buckets(target_size: int) -> None:
    """Hard cap fallback: when `_gc_stale_buckets` can't shrink
    below `_MAX_BUCKETS`, evict the oldest-by-last-timestamp entries
    until size <= `target_size`. Defensive — under expected load
    the periodic sweep keeps the dict well below cap."""
    if len(_TOOL_RATE_BUCKETS) <= target_size:
        return
    # Sort by newest timestamp, ascending. Empty buckets (no last
    # timestamp) are oldest by construction — sort them first.
    items = sorted(
        _TOOL_RATE_BUCKETS.items(),
        key=lambda kv: kv[1][-1] if kv[1] else float("-inf"),
    )
    drop_count = len(_TOOL_RATE_BUCKETS) - target_size
    for key, _ in items[:drop_count]:
        _TOOL_RATE_BUCKETS.pop(key, None)

# Round-3 residual #2 of #146 (#149): track in-flight audit tasks so
# transports can drain them on shutdown. Without this, a stdio
# bridge can deliver the tool result and exit before
# persist_audit_record completes — asyncio.run cancels outstanding
# tasks and the row is silently dropped. Bounded set: warning emitted
# at MAX_INFLIGHT (back-pressure signal so we don't unbounded-grow
# under audit-DB outage).
_INFLIGHT_AUDIT_TASKS: set[asyncio.Task[bool]] = set()
_MAX_INFLIGHT_AUDIT_TASKS = 1024


def _mcp_rate_limit_enabled() -> bool:
    from mnemos.core import rate_limit as core_rate_limit

    return bool(core_rate_limit.RATE_LIMIT_ENABLED)


def _mcp_default_limit_per_minute() -> int:
    from mnemos.core import rate_limit as core_rate_limit

    raw = str(core_rate_limit.RATE_LIMIT_DEFAULT or "").strip().lower()
    match = re.match(r"\A(\d+)\s*/\s*(second|minute|hour|day)s?\Z", raw)
    if not match:
        return 300
    count = int(match.group(1))
    unit = match.group(2)
    if unit == "second":
        return max(1, count * 60)
    if unit == "hour":
        return max(1, count // 60)
    if unit == "day":
        return max(1, count // (24 * 60))
    return max(1, count)


def _mcp_touch_bucket(
    *,
    key: tuple[str, str],
    limit: int,
    window_seconds: float,
) -> None:
    global _gc_touch_counter
    now = time.monotonic()
    cutoff = now - window_seconds
    bucket = _TOOL_RATE_BUCKETS[key]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise PermissionError("rate limit exceeded")
    bucket.append(now)

    # #204: periodic GC of stale (principal, tool) keys. The
    # per-touch popleft above only prunes timestamps inside the
    # current bucket; idle principals' buckets stay forever
    # without this sweep. Run amortized — every Nth touch — so
    # the hot path stays O(1).
    _gc_touch_counter += 1
    if _gc_touch_counter >= _GC_SWEEP_INTERVAL:
        _gc_touch_counter = 0
        _gc_stale_buckets(cutoff=cutoff)
        if len(_TOOL_RATE_BUCKETS) > _MAX_BUCKETS:
            _evict_oldest_buckets(target_size=_MAX_BUCKETS // 2)


async def _mcp_consult_rate_limit(
    *,
    tool_name: str,
    user_id: str | None,
    kind: str,
) -> None:
    """Consult the core rate-limit settings and touch the MCP budget bucket."""
    if not _mcp_rate_limit_enabled():
        return

    default_per_minute = _mcp_default_limit_per_minute()
    if kind == "write":
        limit = min(MCP_WRITE_RATE_LIMIT_PER_MINUTE, default_per_minute)
    elif kind == "read":
        # Compose with the global ceiling: MCP reads may be stricter
        # than the default, but they should never bypass it.
        limit = min(MCP_READ_RATE_LIMIT_PER_MINUTE, default_per_minute)
    else:
        raise ValueError("unknown MCP rate-limit bucket")

    _mcp_touch_bucket(
        key=(kind, user_id or "anonymous"),
        limit=limit,
        window_seconds=60.0,
    )


async def _mcp_enforce_write_rate_limit(
    *,
    tool_name: str,
    user: UserContext,
    limit: int,
    window_seconds: float = 60.0,
) -> None:
    """Per-tool guard for direct database write paths."""
    if not user.authenticated:
        raise PermissionError("authenticated user required for write tool")
    if not _mcp_rate_limit_enabled():
        return
    try:
        _mcp_touch_bucket(
            key=(tool_name, user.user_id),
            limit=limit,
            window_seconds=window_seconds,
        )
    except PermissionError as exc:
        raise PermissionError(f"rate limit exceeded for {tool_name}") from exc


def _mcp_parameter_shape(parameters: dict[str, Any]) -> dict[str, Any]:
    def shape(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"type": "str", "length": len(value)}
        if isinstance(value, bool):
            return {"type": "bool"}
        if isinstance(value, int):
            return {"type": "int"}
        if isinstance(value, float):
            return {"type": "float"}
        if isinstance(value, list):
            item_types = sorted({type(item).__name__ for item in value[:10]})
            return {"type": "list", "count": len(value), "item_types": item_types}
        if isinstance(value, dict):
            return {"type": "dict", "count": len(value)}
        if value is None:
            return {"type": "none"}
        return {"type": type(value).__name__}

    return {key: shape(value) for key, value in sorted(parameters.items())}


def _schedule_audit_persist(
    *,
    caller_user_id: str,
    role: str,
    tool: str,
    parameter_shape: dict[str, Any],
    outcome: str,
    error_class: str | None,
) -> None:
    """Phase-D: schedule a fire-and-forget DB write to mcp_audit_log.

    Imports lazily to avoid a circular dep with mnemos.db.* at module
    load. Silently no-ops in sync contexts (no running loop) and on
    any pool/DB failure — the logger entry above is the always-on
    surface, the table is the durable mirror when a postgres pool is
    available.

    Round-3 residual #2 of #146 (#149): the created task is tracked
    in _INFLIGHT_AUDIT_TASKS and removed via add_done_callback so a
    transport-shutdown drain can await pending writes. Without
    tracking, asyncio.run would cancel the task on loop close and
    drop the row. The set is also bounded so an audit-DB outage
    can't unbounded-grow it.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — sync test or non-async context. Skip
        # persistence; the logger record covers this case.
        return

    try:
        from mnemos.db.mcp_audit_repo import persist_audit_record
    except Exception:  # pragma: no cover — defensive import guard
        return

    # Bounded backlog: refuse new tasks when too many are in-flight
    # so an audit-DB outage doesn't unbounded-grow this set. The
    # logger entry above is still emitted, so the call isn't a
    # silent loss in this case.
    if len(_INFLIGHT_AUDIT_TASKS) >= _MAX_INFLIGHT_AUDIT_TASKS:
        _MCP_AUDIT_LOGGER.warning(
            "mcp_audit_log inflight backlog >= %d; dropping persist for "
            "tool=%s caller=%s (logger entry retained, table row dropped)",
            _MAX_INFLIGHT_AUDIT_TASKS, tool, caller_user_id,
        )
        return

    # Round-1 of #146: persist_audit_record tries the in-process
    # pool first (API process), then falls back to httpx POST to
    # /v1/internal/mcp_audit (standalone MCP bridges).
    task = loop.create_task(
        persist_audit_record(
            caller_user_id=caller_user_id,
            role=role,
            tool=tool,
            parameter_shape=parameter_shape,
            outcome=outcome,
            error_class=error_class,
        )
    )
    _INFLIGHT_AUDIT_TASKS.add(task)
    task.add_done_callback(_INFLIGHT_AUDIT_TASKS.discard)


async def drain_pending_audit_tasks(timeout: float = 5.0) -> int:
    """Wait for in-flight audit persist tasks to complete.

    Called from transport-shutdown hooks (FastAPI lifespan, stdio
    bridge teardown, MCP HTTP shutdown) to flush durable audit
    writes before the loop closes. Without this drain, a bridge
    that exits immediately after delivering a tool result loses
    audit rows when asyncio.run cancels outstanding tasks.

    Returns the number of tasks that were still pending (i.e. drained
    or timed out). Caller may emit metrics if the count is non-zero.
    """
    pending = list(_INFLIGHT_AUDIT_TASKS)
    if not pending:
        return 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Some tasks didn't finish in time. Log a warning so
        # operators can see when audit writes are slow on shutdown,
        # but don't propagate — shutdown must complete.
        still_running = sum(1 for t in pending if not t.done())
        _MCP_AUDIT_LOGGER.warning(
            "mcp_audit_log drain timed out: %d task(s) still pending "
            "after %.1fs",
            still_running, timeout,
        )
    return len(pending)


def _mcp_log_tool_audit(
    *,
    caller_id: str | None,
    role: str | None,
    tool_name: str,
    parameters: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> None:
    parameter_shape = _mcp_parameter_shape(parameters)
    caller_user_id = caller_id or "unknown"
    role_value = role or "unknown"

    payload = {
        "caller_user_id": caller_user_id,
        "role": role_value,
        "tool": tool_name,
        "parameter_shape": parameter_shape,
        "outcome": outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    _MCP_AUDIT_LOGGER.info("mcp_tool_invocation %s", payload)

    # Phase-D durable surface — fire-and-forget DB write.
    _schedule_audit_persist(
        caller_user_id=caller_user_id,
        role=role_value,
        tool=tool_name,
        parameter_shape=parameter_shape,
        outcome=outcome,
        error_class=error_class,
    )


def _mcp_log_root_bypass(
    *,
    caller_id: str | None,
    tool_name: str,
    parameters: dict[str, Any],
) -> None:
    parameter_shape = _mcp_parameter_shape(parameters)
    caller_user_id = caller_id or "unknown"
    _MCP_AUDIT_LOGGER.warning(
        "mcp_root_bypass %s",
        {
            "caller_user_id": caller_user_id,
            "role": "root",
            "tool": tool_name,
            "parameter_shape": parameter_shape,
        },
    )
    # Phase-D mirror: persist root_bypass entries with their own
    # outcome label so operators can query for elevation events.
    _schedule_audit_persist(
        caller_user_id=caller_user_id,
        role="root",
        tool=tool_name,
        parameter_shape=parameter_shape,
        outcome="root_bypass",
        error_class=None,
    )

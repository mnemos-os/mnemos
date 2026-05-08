"""Slice #204: pin GC of stale (principal, tool) rate-limit buckets.

Audit LOW finding (mem_1778221719390_8cb1ba) at
``mnemos/mcp/tools/_security.py:16``: ``_TOOL_RATE_BUCKETS`` was
a ``defaultdict(deque)`` that pruned timestamps inside each deque
on every touch but never dropped the (principal, tool) keys
themselves. A high-churn principal flow (CI matrix, rotating
tokens) would leak memory — small per-entry, monotonically
growing for the lifetime of the process.

Fix: amortized periodic sweep (every ``_GC_SWEEP_INTERVAL``
touches) of buckets whose newest timestamp is past the cutoff.
Hard cap at ``_MAX_BUCKETS`` triggers an eviction-by-last-
timestamp pass if the sweep alone can't reduce below cap.

This test pins:
1. Constants exist with sane values.
2. Buckets fully past the cutoff are dropped on the sweep.
3. Active buckets (with timestamps inside the window) survive.
4. Empty buckets (defaultdict-created but never appended-to)
   are dropped on sweep.
5. Beyond-cap dict triggers eviction of oldest-by-last-timestamp.
6. Live touches still rate-limit normally with the sweep wired in.
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from mnemos.mcp.tools import _security as sec


@pytest.fixture(autouse=True)
def _reset_buckets():
    """Clean module-global state per test."""
    sec._TOOL_RATE_BUCKETS.clear()
    sec._gc_touch_counter = 0
    yield
    sec._TOOL_RATE_BUCKETS.clear()
    sec._gc_touch_counter = 0


def test_constants_have_sane_values():
    assert sec._GC_SWEEP_INTERVAL >= 16
    assert sec._GC_SWEEP_INTERVAL <= 100_000
    assert sec._MAX_BUCKETS >= 256
    assert sec._MAX_BUCKETS <= 1_000_000


def test_gc_drops_buckets_past_cutoff():
    """A bucket whose newest timestamp is older than the window
    cutoff is dropped by `_gc_stale_buckets`."""
    now = time.monotonic()
    # Stale: last timestamp at now - 120, cutoff at now - 60.
    sec._TOOL_RATE_BUCKETS[("alice", "search")] = deque([now - 200, now - 120])
    # Active: last timestamp at now - 30, cutoff at now - 60.
    sec._TOOL_RATE_BUCKETS[("bob", "search")] = deque([now - 30])
    sec._gc_stale_buckets(cutoff=now - 60)
    assert ("alice", "search") not in sec._TOOL_RATE_BUCKETS
    assert ("bob", "search") in sec._TOOL_RATE_BUCKETS


def test_gc_drops_empty_buckets():
    """Empty buckets — created by `defaultdict` lookup when the
    touch raised before appending — are dropped on sweep."""
    sec._TOOL_RATE_BUCKETS[("orphan", "search")] = deque()
    sec._gc_stale_buckets(cutoff=0.0)
    assert ("orphan", "search") not in sec._TOOL_RATE_BUCKETS


def test_evict_oldest_when_cap_exceeded():
    """When `_evict_oldest_buckets(target_size=N)` runs against a
    dict bigger than N, the oldest-by-last-timestamp entries are
    dropped first."""
    now = time.monotonic()
    # 10 buckets with monotonically increasing last-timestamp
    for i in range(10):
        sec._TOOL_RATE_BUCKETS[(f"u{i}", "search")] = deque([now + i])
    sec._evict_oldest_buckets(target_size=4)
    assert len(sec._TOOL_RATE_BUCKETS) == 4
    # The 6 oldest must be evicted; the 4 newest (u6..u9) survive.
    surviving = {k for k in sec._TOOL_RATE_BUCKETS}
    assert surviving == {(f"u{i}", "search") for i in range(6, 10)}


def test_periodic_sweep_runs_every_n_touches(monkeypatch):
    """`_mcp_touch_bucket` triggers the sweep every
    `_GC_SWEEP_INTERVAL` touches. Force the interval down to a
    handful so the test doesn't need to call N hundreds of
    times."""
    monkeypatch.setattr(sec, "_GC_SWEEP_INTERVAL", 4)
    # Pre-seed a stale entry that should get reaped by sweep.
    now = time.monotonic()
    sec._TOOL_RATE_BUCKETS[("ghost", "search")] = deque([now - 1000])
    # 4 touches → trigger one sweep
    for i in range(4):
        sec._mcp_touch_bucket(
            key=("alice", "search"),
            limit=1000,
            window_seconds=60,
        )
    assert ("ghost", "search") not in sec._TOOL_RATE_BUCKETS, (
        "stale (ghost, search) bucket should have been swept"
    )
    assert ("alice", "search") in sec._TOOL_RATE_BUCKETS


def test_rate_limit_still_fires_with_gc_wired_in():
    """Sanity-check: the hot-path rate-limit behavior is
    unchanged by the GC additions. The 6th call inside a 60s
    window with limit=5 must raise PermissionError."""
    for _ in range(5):
        sec._mcp_touch_bucket(
            key=("alice", "search"),
            limit=5,
            window_seconds=60,
        )
    with pytest.raises(PermissionError):
        sec._mcp_touch_bucket(
            key=("alice", "search"),
            limit=5,
            window_seconds=60,
        )

"""Reconnect backoff with full jitter for NATS consumer loops.

Audit Finding 8 (handoff queue #3): the federation and webhook NATS
consumers used a fixed-delay retry. When the broker bounces, every
worker process across a fleet wakes at the same interval and
hammers the broker on reconnect (thundering herd). Exponential
backoff with full jitter prevents synchronised reconnects:

* base: starting delay (e.g. 1s) — fast first retry
* cap: maximum delay (e.g. 60s) — bound the worst-case wait
* multiplier: 2.0 doubles delay each loop until cap
* full jitter: actual sleep is uniform(0, current_delay), so two
  workers that started together drift apart

Reference: AWS Architecture Blog,
"Exponential Backoff And Jitter" (Marc Brooker).
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass


@dataclass
class ReconnectBackoff:
    """Stateful exponential-backoff-with-jitter scheduler.

    A single instance is constructed per consumer loop; ``sleep()`` is
    awaited on every reconnect attempt and ``reset()`` is called the
    moment a connection succeeds so the next failure starts at
    ``base_seconds`` again.
    """

    base_seconds: float = 1.0
    cap_seconds: float = 60.0
    multiplier: float = 2.0
    _current: float = 0.0

    def __post_init__(self) -> None:
        if self.base_seconds <= 0:
            raise ValueError("base_seconds must be > 0")
        if self.cap_seconds < self.base_seconds:
            raise ValueError("cap_seconds must be >= base_seconds")
        if self.multiplier <= 1.0:
            raise ValueError("multiplier must be > 1.0")
        self._current = self.base_seconds

    def reset(self) -> None:
        """Reset the backoff window after a successful connection."""
        self._current = self.base_seconds

    def next_delay(self) -> float:
        """Return the jittered sleep for this attempt and advance state.

        Full-jitter: actual sleep is uniform(0, current_window). Two
        workers that started together will not synchronise on the
        next retry. The window itself doubles up to ``cap_seconds``.
        """
        window = self._current
        # Advance for next call, capped.
        self._current = min(self._current * self.multiplier, self.cap_seconds)
        # Random sleep in [0, window). Use random.uniform for an
        # explicit closed-open shape; with full jitter the lower
        # bound of 0 is the whole point — workers can retry
        # immediately.
        return random.uniform(0.0, window)

    async def sleep(self) -> None:
        """Sleep for the jittered window. Cancellable."""
        await asyncio.sleep(self.next_delay())

"""Per-endpoint circuit breaker for GPU-backed compression engines.

Why this module exists (and what it is NOT):

  * It IS circuit-breaker + CPU-fallback coordination. Engines that
    declare `gpu_required` or `gpu_optional` in their
    `CompressionEngine.gpu_intent` consult a GPUGuard before making
    their HTTP call; if the guard's circuit is open (too many recent
    failures), the engine fast-fails with a structured error instead
    of piling a doomed request onto a dead endpoint.

  * It is NOT a request batcher. True HTTP-level batching (accumulate
    N concurrent compress calls, flush as a single /v1/completions
    with a list prompt) is a v3.2 optimization. Modern inference
    servers (vLLM, Ollama) already batch concurrent requests
    internally at the model layer, so the v3.1 win-over-do-nothing is
    the correctness work — fail fast when the endpoint is down, route
    gpu_required engines around outages, track recovery — not the
    throughput work.

Circuit states:

                +------------------+                +---------------+
                |                  | fail_count  >= |               |
   success -->  |      CLOSED      | ------------>  |      OPEN     |
                |                  | threshold      |               |
                +------------------+                +-------+-------+
                        ^                                   |
                        | success on probe                  | cooldown
                        |                                   |  elapsed
                        |         +----------------+        |
                        +-------- |   HALF_OPEN    | <------+
                                  |                |
                                  +----------------+
                                          |
                                          | failure on probe
                                          v
                                    back to OPEN

  * CLOSED     — healthy; requests flow through, successes reset the
                 counter, failures increment it.
  * OPEN       — circuit tripped; `is_available()` returns False
                 until the cooldown window elapses. Engines fast-fail
                 or route around.
  * HALF_OPEN  — cooldown elapsed; exactly ONE probe request is
                 admitted. While that probe is in flight, concurrent
                 `is_available()` calls return False so the possibly-
                 still-broken endpoint isn't flooded. Probe success
                 transitions back to CLOSED; probe failure re-opens
                 the circuit for another cooldown window. If a probe
                 caller crashes or is cancelled without calling
                 record_success / record_failure, the circuit stays
                 HALF_OPEN until an operator calls reset() — we
                 don't auto-admit a replacement, because a late
                 completion from the original caller would race with
                 the replacement without a probe-identity handshake
                 (that's a v3.2 candidate).

The guard is per-endpoint (keyed by URL) and lives in a process-local
registry. Multiple GPU-consuming paths sharing the same
`GPU_PROVIDER_HOST` share one guard, so one timeout informs APOLLO
(and any other GPU-consuming path) that the endpoint is unresponsive
without each having to time out independently.

For v3.2 horizontal-scaling work, the registry becomes a Redis-backed
shared-state singleton. v3.1 is single-worker per the DEPLOYMENT.md
scaling note; process-local state is correct for the constraint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class GuardConfig:
    """Tunables for a GPUGuard.

    Defaults are conservative for the single-worker v3.1 deployment:
    three consecutive failures open the circuit for 30 seconds. Operators
    who run against flaky remote endpoints can relax these; operators
    who want stricter failure isolation can tighten them.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    # Upper bound on how long a HALF_OPEN probe can be in flight before
    # the guard treats it as abandoned and admits a replacement (v3.2
    # probe-identity handshake). Default 120s is roomy above realistic
    # GPU handler timeouts (~30s). A late completion from an abandoned
    # probe will have a stale token and be ignored by record_* calls,
    # so auto-replacement is safe.
    probe_timeout_seconds: float = 120.0
    # Minimum time between state-transition log messages. Prevents log
    # floods when many concurrent tasks hit a recently-opened circuit.
    log_throttle_seconds: float = 5.0


class GPUGuard:
    """Per-endpoint circuit breaker with async coordination.

    Thread/task safety: all state mutation goes through an asyncio.Lock.
    `is_available()` is a cheap read but takes the lock briefly to
    compute the cooldown-elapsed check atomically with a possible
    state transition to HALF_OPEN.

    Intended usage from an engine's compress() method (v3.2 shape,
    probe-identity handshake):

        guard = get_guard(self._core.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            return CompressionResult(... error="gpu_guard: circuit open ...")
        try:
            result = await self._core.extract_facts(...)
        except Exception as exc:
            await guard.record_failure(exc, probe_token=probe_token)
            raise
        else:
            await guard.record_success(probe_token=probe_token)
            return result

    `probe_token` is None for CLOSED-state admissions and an opaque
    integer when the caller was admitted as a probe in HALF_OPEN.
    HALF_OPEN callers must pass the token on record_* calls; CLOSED-state
    calls use ``None`` because no probe identity is active.
    """

    def __init__(
        self,
        endpoint: str,
        config: Optional[GuardConfig] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.config = config or GuardConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._last_log_at: float = 0.0
        self._last_error: Optional[str] = None
        # Single-probe coordination. `_probe_token` is a monotonically
        # increasing generation counter that advances every time a new
        # probe is admitted. The admitted caller receives the current
        # token; subsequent record_* calls must pass the same token
        # or they're discarded as "stale probe" — this is the v3.2
        # identity handshake that makes auto-replacement of an
        # abandoned probe safe.
        self._probe_token: int = 0
        self._probe_started_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    async def is_available(self) -> Tuple[bool, Optional[int]]:
        """Admission check. Returns (admitted, probe_token).

          admitted     — True if the caller may proceed.
          probe_token  — Opaque identity for HALF_OPEN probe admissions
                         (pass back to record_success / record_failure).
                         None for CLOSED-state admissions and for
                         rejected callers.

        State-dependent admission:
          * CLOSED:    always admit, probe_token=None.
          * OPEN:      admit iff cooldown has elapsed (the admitted
                       caller becomes a probe; state transitions to
                       HALF_OPEN; probe_token advances and is
                       returned).
          * HALF_OPEN: admit iff no probe currently in flight, OR
                       the prior probe has been in flight longer than
                       `probe_timeout_seconds` — in which case the
                       token advances (invalidating the stale probe's
                       identity) and the new caller becomes the
                       replacement probe.
        """
        async with self._lock:
            now = time.monotonic()

            if self._state is CircuitState.OPEN:
                if self._opened_at is None:
                    # Shouldn't happen, but stay defensive.
                    self._state = CircuitState.HALF_OPEN
                    self._probe_token += 1
                    self._probe_started_at = now
                    return True, self._probe_token
                if now - self._opened_at >= self.config.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_token += 1
                    self._probe_started_at = now
                    self._log_throttled(
                        "circuit HALF_OPEN: probe request may now proceed "
                        "against %s (token=%d)",
                        self.endpoint,
                        self._probe_token,
                    )
                    return True, self._probe_token
                return False, None

            if self._state is CircuitState.HALF_OPEN:
                if self._probe_started_at is None:
                    # Probe slot is free (probe resolved via reset() or
                    # defensive path through an unusual transition).
                    self._probe_token += 1
                    self._probe_started_at = now
                    return True, self._probe_token

                # Probe is in flight. Admit a replacement only if the
                # prior probe has been outstanding longer than the
                # abandonment timeout — and advance the token so the
                # stale probe's record_* calls become no-ops.
                if (
                    now - self._probe_started_at
                    >= self.config.probe_timeout_seconds
                ):
                    stale_token = self._probe_token
                    self._probe_token += 1
                    self._probe_started_at = now
                    logger.warning(
                        "gpu_guard[%s]: prior probe abandoned (token=%d -> "
                        "%d), admitting replacement",
                        self.endpoint, stale_token, self._probe_token,
                    )
                    return True, self._probe_token

                # Single-probe guarantee: reject concurrent callers
                # while a probe is still within its window.
                return False, None

            # CLOSED
            return True, None

    async def record_success(self, probe_token: Optional[int] = None) -> None:
        """Note a successful request. Resets failure counter; if the
        circuit was HALF_OPEN, transitions back to CLOSED.

        HALF_OPEN probe completions must pass the token returned by
        is_available(). Late completions from abandoned probes carry a
        stale token and cannot pollute the replacement probe's resolution.
        """
        async with self._lock:
            if (
                probe_token is not None and probe_token != self._probe_token
            ) or (
                self._state is CircuitState.HALF_OPEN and probe_token is None
            ):
                logger.debug(
                    "gpu_guard[%s]: discarding stale probe success "
                    "(token=%s, current=%d)",
                    self.endpoint, probe_token, self._probe_token,
                )
                return

            if self._state is CircuitState.HALF_OPEN:
                logger.info(
                    "gpu_guard[%s]: probe succeeded, circuit CLOSED (token=%d)",
                    self.endpoint, self._probe_token,
                )
                self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._last_error = None
            self._probe_started_at = None

    async def record_failure(
        self,
        exc: Optional[BaseException] = None,
        probe_token: Optional[int] = None,
    ) -> None:
        """Note a failure. Increments the failure counter; if the counter
        crosses the threshold (or if we were HALF_OPEN), OPEN the circuit.

        HALF_OPEN probe completions must pass the token returned by
        is_available() — see record_success for details. A stale or
        missing token discards the call so a late failure from an
        abandoned probe cannot re-open a circuit that the replacement
        probe already closed.
        """
        async with self._lock:
            if (
                probe_token is not None and probe_token != self._probe_token
            ) or (
                self._state is CircuitState.HALF_OPEN and probe_token is None
            ):
                logger.debug(
                    "gpu_guard[%s]: discarding stale probe failure "
                    "(token=%s, current=%d)",
                    self.endpoint, probe_token, self._probe_token,
                )
                return

            self._last_error = (
                f"{type(exc).__name__}: {exc}" if exc is not None else "unspecified"
            )

            if self._state is CircuitState.HALF_OPEN:
                # Probe failed — immediately re-open for another cooldown window.
                self._opened_at = time.monotonic()
                self._state = CircuitState.OPEN
                self._probe_started_at = None
                logger.warning(
                    "gpu_guard[%s]: probe failed, circuit re-OPEN (%s)",
                    self.endpoint,
                    self._last_error,
                )
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                if self._state is not CircuitState.OPEN:
                    self._opened_at = time.monotonic()
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "gpu_guard[%s]: %d consecutive failures, circuit OPEN "
                        "for %.0fs (%s)",
                        self.endpoint,
                        self._consecutive_failures,
                        self.config.cooldown_seconds,
                        self._last_error,
                    )

    def _log_throttled(self, fmt: str, *args) -> None:
        """Emit a log message at most once per log_throttle_seconds."""
        now = time.monotonic()
        if now - self._last_log_at >= self.config.log_throttle_seconds:
            logger.info(fmt, *args)
            self._last_log_at = now

    def snapshot(self) -> Dict[str, object]:
        """Diagnostic snapshot for the manifest-read endpoint / admin UI.

        `probe_in_flight` is a derived boolean for operators reading
        the snapshot JSON: true iff a probe token has been issued and
        not yet resolved. `probe_token` and `probe_started_at` are the
        underlying fields — exposed for ops debugging of the identity
        handshake.
        """
        return {
            "endpoint": self.endpoint,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
            "last_error": self._last_error,
            "probe_in_flight": self._probe_started_at is not None,
            "probe_token": self._probe_token,
            "probe_started_at": self._probe_started_at,
        }

    def reset(self) -> None:
        """Force the circuit back to CLOSED. Operator escape hatch — not
        used by engine code. Synchronous; caller is responsible for
        coordinating with in-flight requests.

        Advances the probe token so any in-flight record_* call with
        the old token is discarded."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._last_error = None
        self._probe_token += 1  # invalidate any outstanding probe
        self._probe_started_at = None


# ---- process-local registry -------------------------------------------------


_GUARDS: Dict[str, GPUGuard] = {}
_REGISTRY_LOCK = asyncio.Lock()


def get_guard(endpoint: str, config: Optional[GuardConfig] = None) -> GPUGuard:
    """Return the process-local guard for `endpoint`. Constructs on first
    access with the supplied `config` (or GuardConfig defaults)."""
    endpoint_key = endpoint.rstrip("/")
    existing = _GUARDS.get(endpoint_key)
    if existing is not None:
        return existing
    created = GPUGuard(endpoint_key, config=config)
    _GUARDS[endpoint_key] = created
    return created


def all_guards() -> Dict[str, GPUGuard]:
    """Read-only view of the registry — for diagnostics / admin surfaces."""
    return dict(_GUARDS)


def reset_registry() -> None:
    """Clear the registry. Test-only helper. Do not call from engine code."""
    _GUARDS.clear()


__all__ = [
    "CircuitState",
    "GuardConfig",
    "GPUGuard",
    "get_guard",
    "all_guards",
    "reset_registry",
]

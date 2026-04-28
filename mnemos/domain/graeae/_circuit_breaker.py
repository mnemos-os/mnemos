from __future__ import annotations

"""Per-provider circuit breaker for the GRAEAE engine.

States:
  CLOSED    — normal, all requests pass through
  OPEN      — tripped after N failures; blocks requests for cooldown period
  HALF_OPEN — probe state after cooldown; one request let through to test recovery
"""
import logging
import threading
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Tracks failures for a single provider and gates requests accordingly."""

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
        success_threshold: int = 2,
    ):
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.success_threshold = success_threshold

        self.state = CircuitState.CLOSED
        self._failures = 0
        self._probe_successes = 0
        self._opened_at: datetime | None = None
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                elapsed = (datetime.now(timezone.utc) - self._opened_at).total_seconds()
                if elapsed >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self._probe_successes = 0
                    logger.info(f"[CB] {self.provider}: OPEN → HALF_OPEN")
                    return True
                return False
            # HALF_OPEN: let the probe through
            return True

    def record_success(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self._probe_successes += 1
                if self._probe_successes >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    self._failures = 0
                    logger.info(f"[CB] {self.provider}: HALF_OPEN → CLOSED")
            elif self.state == CircuitState.CLOSED:
                # Decay failure count on success so transient spikes don't persist
                self._failures = max(0, self._failures - 1)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if self._failures >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self._opened_at = datetime.now(timezone.utc)
                    logger.warning(
                        f"[CB] {self.provider}: TRIPPED after {self._failures} failures"
                    )

    def status(self) -> dict:
        with self._lock:
            return {"state": self.state.value, "failures": self._failures}


class CircuitBreakerPool:
    """Pool of circuit breakers, one per provider."""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 300):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._breakers: dict[str, CircuitBreaker] = {}

    def _get(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(
                provider, self._failure_threshold, self._cooldown_seconds
            )
        return self._breakers[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def record_success(self, provider: str) -> None:
        self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        self._get(provider).record_failure()

    def status(self) -> dict[str, dict]:
        return {p: cb.status() for p, cb in self._breakers.items()}

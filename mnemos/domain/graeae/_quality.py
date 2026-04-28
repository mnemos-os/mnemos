from __future__ import annotations

"""Per-provider quality tracking for dynamic weight adjustment.

Maintains a rolling window of the last N outcomes per provider. The dynamic
weight is base_weight × success_multiplier, where success_multiplier scales
linearly from 0.5 (0% success) to 1.0 (100% success). This means a provider
with a perfect recent track record keeps its full configured weight, while a
flaky one is deprioritised without being removed from the pool entirely
(the circuit breaker handles full removal).
"""
import threading
from collections import deque

_WINDOW = 20  # rolling window size (number of outcomes)


class ProviderQuality:
    """Rolling success-rate and latency tracker for one provider."""

    def __init__(self, base_weight: float):
        self.base_weight = base_weight
        self._outcomes: deque[bool] = deque(maxlen=_WINDOW)
        self._latencies: deque[int] = deque(maxlen=_WINDOW)
        self._lock = threading.Lock()

    def record_success(self, latency_ms: int) -> None:
        with self._lock:
            self._outcomes.append(True)
            self._latencies.append(latency_ms)

    def record_failure(self) -> None:
        with self._lock:
            self._outcomes.append(False)

    def dynamic_weight(self) -> float:
        """base_weight scaled by recent success rate: 100% → 1.0×, 0% → 0.5×."""
        with self._lock:
            if not self._outcomes:
                return self.base_weight
            rate = sum(self._outcomes) / len(self._outcomes)
            multiplier = 0.5 + 0.5 * rate
            return round(self.base_weight * multiplier, 4)

    def avg_latency_ms(self) -> int:
        with self._lock:
            return int(sum(self._latencies) / len(self._latencies)) if self._latencies else 0


class QualityTracker:
    """Pool of quality trackers, one per provider."""

    def __init__(self, provider_weights: dict[str, float]):
        self._trackers: dict[str, ProviderQuality] = {
            p: ProviderQuality(w) for p, w in provider_weights.items()
        }

    def _get(self, provider: str) -> ProviderQuality | None:
        return self._trackers.get(provider)

    def record_success(self, provider: str, latency_ms: int) -> None:
        if t := self._get(provider):
            t.record_success(latency_ms)

    def record_failure(self, provider: str) -> None:
        if t := self._get(provider):
            t.record_failure()

    def dynamic_weight(self, provider: str) -> float:
        if t := self._get(provider):
            return t.dynamic_weight()
        return 0.0

    def status(self) -> dict:
        return {
            p: {
                "dynamic_weight": t.dynamic_weight(),
                "base_weight": t.base_weight,
                "avg_latency_ms": t.avg_latency_ms(),
            }
            for p, t in self._trackers.items()
        }

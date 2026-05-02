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
from typing import Any

from mnemos.core.resilience import (
    _get_lifecycle_redis_client,
    _redis_requested,
    _warn_fallback,
)

_WINDOW = 20  # rolling window size (number of outcomes)
_REDIS_KEY_PREFIX = "mnemos:graeae:quality:"
_EWMA_ALPHA = 0.2

_REDIS_QUALITY_EWMA_LUA = """
local key = KEYS[1]
local outcome = tonumber(ARGV[1])
local latency = tonumber(ARGV[2])
local alpha = tonumber(ARGV[3])
local base_weight = tonumber(ARGV[4])

local old_success = redis.call('ZSCORE', key, 'success_ewma')
local success
if old_success then
  success = (alpha * outcome) + ((1 - alpha) * tonumber(old_success))
else
  success = outcome
end

redis.call('ZADD', key, success, 'success_ewma')
redis.call('ZADD', key, base_weight, 'base_weight')

local latency_ewma = redis.call('ZSCORE', key, 'latency_ewma')
if latency >= 0 then
  if latency_ewma then
    latency_ewma = (alpha * latency) + ((1 - alpha) * tonumber(latency_ewma))
  else
    latency_ewma = latency
  end
  redis.call('ZADD', key, latency_ewma, 'latency_ewma')
elseif latency_ewma then
  latency_ewma = tonumber(latency_ewma)
else
  latency_ewma = 0
end

return {success, latency_ewma}
"""


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


class RedisQualityTracker:
    """Redis sorted-set EWMA quality tracker shared across workers."""

    def __init__(
        self,
        redis_client: Any,
        provider_weights: dict[str, float],
        *,
        key_prefix: str = _REDIS_KEY_PREFIX,
        alpha: float = _EWMA_ALPHA,
    ):
        self.redis = redis_client
        self.provider_weights = dict(provider_weights)
        self.key_prefix = key_prefix
        self.alpha = alpha
        self._last_status: dict[str, dict[str, float | int]] = {
            provider: {
                "dynamic_weight": weight,
                "base_weight": weight,
                "avg_latency_ms": 0,
            }
            for provider, weight in self.provider_weights.items()
        }

    def _key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}"

    async def _record(self, provider: str, *, outcome: float, latency_ms: int) -> None:
        if provider not in self.provider_weights:
            return
        base_weight = float(self.provider_weights[provider])
        result = await self.redis.eval(
            _REDIS_QUALITY_EWMA_LUA,
            1,
            self._key(provider),
            float(outcome),
            int(latency_ms),
            float(self.alpha),
            base_weight,
        )
        success_ewma = float(result[0]) if isinstance(result, (list, tuple)) else float(result)
        latency_ewma = float(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else 0.0
        self._last_status[provider] = {
            "dynamic_weight": round(base_weight * (0.5 + 0.5 * success_ewma), 4),
            "base_weight": base_weight,
            "avg_latency_ms": int(latency_ewma),
        }

    async def record_success(self, provider: str, latency_ms: int) -> None:
        await self._record(provider, outcome=1.0, latency_ms=latency_ms)

    async def record_failure(self, provider: str) -> None:
        await self._record(provider, outcome=0.0, latency_ms=-1)

    async def dynamic_weight(self, provider: str) -> float:
        base_weight = float(self.provider_weights.get(provider, 0.0))
        if base_weight == 0.0:
            return 0.0
        raw = await self.redis.zscore(self._key(provider), "success_ewma")
        if raw is None:
            return base_weight
        score = float(raw)
        weight = round(base_weight * (0.5 + 0.5 * score), 4)
        current = self._last_status.setdefault(
            provider,
            {"dynamic_weight": weight, "base_weight": base_weight, "avg_latency_ms": 0},
        )
        current["dynamic_weight"] = weight
        current["base_weight"] = base_weight
        return weight

    def status(self) -> dict:
        return dict(self._last_status)


def make_quality_tracker(
    settings: Any,
    provider_weights: dict[str, float],
    *,
    redis_client: Any | None = None,
) -> QualityTracker | RedisQualityTracker:
    if _redis_requested(settings):
        client = redis_client if redis_client is not None else _get_lifecycle_redis_client()
        if client is not None:
            return RedisQualityTracker(client, provider_weights)
        _warn_fallback(settings, "Redis quality tracker requested but unavailable")
    else:
        _warn_fallback(settings, "Redis not configured")
    return QualityTracker(provider_weights)

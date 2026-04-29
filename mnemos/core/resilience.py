from __future__ import annotations

"""GRAEAE resilience primitives with in-process and Redis-backed backends."""

import asyncio
import inspect
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

_PROVIDER_RPM: dict[str, int] = {
    "perplexity": 50,
    "groq": 60,
    "claude_opus": 40,
    "xai": 30,
    "openai": 60,
    "gemini": 60,
    "nvidia": 50,
    "together": 60,
}
_DEFAULT_RPM = 50

_PROVIDER_SLOTS: dict[str, int] = {
    "perplexity": 3,
    "groq": 4,
    "claude_opus": 3,
    "xai": 3,
    "openai": 3,
    "gemini": 3,
    "nvidia": 3,
    "together": 3,
}
_DEFAULT_SLOTS = 3

_OPEN_CACHE_SECONDS = 0.25
_CONCURRENCY_LEASE_SECONDS = 300


async def maybe_await(value: Any) -> Any:
    """Await coroutine-like values while leaving synchronous fakes compatible."""
    if inspect.isawaitable(value):
        return await value
    return value


async def call_maybe_async(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await maybe_await(func(*args, **kwargs))


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class InProcessCircuitBreaker:
    """Tracks failures for a single provider within one Python process."""

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
                if self._opened_at is None:
                    return False
                elapsed = (datetime.now(timezone.utc) - self._opened_at).total_seconds()
                if elapsed >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self._probe_successes = 0
                    logger.info("[CB] %s: OPEN -> HALF_OPEN", self.provider)
                    return True
                return False
            return True

    def check_open(self) -> bool:
        return not self.is_allowed()

    def record_success(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self._probe_successes += 1
                if self._probe_successes >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    self._failures = 0
                    self._opened_at = None
                    logger.info("[CB] %s: HALF_OPEN -> CLOSED", self.provider)
            elif self.state == CircuitState.CLOSED:
                self._failures = max(0, self._failures - 1)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if self._failures >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self._opened_at = datetime.now(timezone.utc)
                    logger.warning(
                        "[CB] %s: TRIPPED after %d failures",
                        self.provider,
                        self._failures,
                    )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self.state.value, "failures": self._failures}


class InProcessCircuitBreakerPool:
    """Pool of in-process circuit breakers, one per provider."""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 300):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._breakers: dict[str, InProcessCircuitBreaker] = {}

    def _get(self, provider: str) -> InProcessCircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = InProcessCircuitBreaker(
                provider,
                self._failure_threshold,
                self._cooldown_seconds,
            )
        return self._breakers[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def record_success(self, provider: str) -> None:
        self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        self._get(provider).record_failure()

    def status(self) -> dict[str, dict[str, Any]]:
        return {provider: breaker.status() for provider, breaker in self._breakers.items()}


_REDIS_CB_FAILURE_LUA = """
local failures = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('HSET', KEYS[3], 'state', 'closed', 'failures', failures)
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[2]))
if failures >= tonumber(ARGV[1]) then
  redis.call('SET', KEYS[2], 'open', 'EX', tonumber(ARGV[2]))
  redis.call('HSET', KEYS[3], 'state', 'open', 'failures', failures, 'opened_at', ARGV[3])
  redis.call('EXPIRE', KEYS[3], tonumber(ARGV[2]))
  return {1, failures}
end
return {0, failures}
"""

_REDIS_CB_SUCCESS_LUA = """
redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])
redis.call('DEL', KEYS[3])
return 1
"""


class RedisCircuitBreaker:
    """Redis-backed circuit breaker shared across worker processes."""

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str,
        failure_threshold: int,
        cooldown_seconds: int,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._open_cache: dict[str, float] = {}
        self._last_status: dict[str, dict[str, Any]] = {}

    def _state_key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}:state"

    def _failures_key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}:failures"

    def _hash_key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}"

    def _cache_open(self, provider: str) -> None:
        self._open_cache[provider] = time.monotonic() + min(
            _OPEN_CACHE_SECONDS,
            float(self.cooldown_seconds),
        )

    def _clear_cache(self, provider: str) -> None:
        self._open_cache.pop(provider, None)

    def cached_open(self, provider: str) -> bool:
        expires_at = self._open_cache.get(provider)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._clear_cache(provider)
            return False
        return True

    async def check_open(self, provider: str) -> bool:
        if self.cached_open(provider):
            status = self._last_status.setdefault(provider, {"state": CircuitState.OPEN.value, "failures": None})
            status["state"] = CircuitState.OPEN.value
            return True
        state = await self.redis.get(self._state_key(provider))
        if state == "open":
            self._cache_open(provider)
            status = self._last_status.setdefault(provider, {"state": CircuitState.OPEN.value, "failures": None})
            status["state"] = CircuitState.OPEN.value
            return True
        self._clear_cache(provider)
        status = self._last_status.setdefault(provider, {"state": CircuitState.CLOSED.value, "failures": None})
        status["state"] = CircuitState.CLOSED.value
        return False

    async def record_failure(self, provider: str) -> None:
        opened_at = datetime.now(timezone.utc).isoformat()
        result = await self.redis.eval(
            _REDIS_CB_FAILURE_LUA,
            3,
            self._failures_key(provider),
            self._state_key(provider),
            self._hash_key(provider),
            int(self.failure_threshold),
            int(self.cooldown_seconds),
            opened_at,
        )
        opened = bool(int(result[0])) if isinstance(result, (list, tuple)) else bool(int(result))
        failures = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else None
        self._last_status[provider] = {
            "state": CircuitState.OPEN.value if opened else CircuitState.CLOSED.value,
            "failures": failures,
        }
        if opened:
            self._cache_open(provider)
            logger.warning("[CB] %s: TRIPPED in Redis", provider)
        else:
            self._clear_cache(provider)

    async def record_success(self, provider: str) -> None:
        await self.redis.eval(
            _REDIS_CB_SUCCESS_LUA,
            3,
            self._state_key(provider),
            self._failures_key(provider),
            self._hash_key(provider),
        )
        self._clear_cache(provider)
        self._last_status[provider] = {"state": CircuitState.CLOSED.value, "failures": 0}

    def status(self, provider: str) -> dict[str, Any]:
        return dict(
            self._last_status.get(
                provider,
                {
                    "state": CircuitState.OPEN.value if self.cached_open(provider) else CircuitState.CLOSED.value,
                    "failures": None,
                },
            )
        )


class RedisCircuitBreakerPool:
    """Circuit breaker pool backed by Redis state."""

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
    ):
        self._breaker = RedisCircuitBreaker(
            redis_client,
            key_prefix,
            failure_threshold,
            cooldown_seconds,
        )
        self._providers: set[str] = set()

    async def is_allowed(self, provider: str) -> bool:
        self._providers.add(provider)
        return not await self._breaker.check_open(provider)

    async def record_success(self, provider: str) -> None:
        self._providers.add(provider)
        await self._breaker.record_success(provider)

    async def record_failure(self, provider: str) -> None:
        self._providers.add(provider)
        await self._breaker.record_failure(provider)

    def status(self) -> dict[str, dict[str, Any]]:
        return {provider: self._breaker.status(provider) for provider in sorted(self._providers)}


class InProcessRateLimiter:
    """Sliding-window rate limiter for a single provider in one process."""

    def __init__(self, provider: str, rpm: int):
        self.provider = provider
        self.rpm = rpm
        self._window = 60.0
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            self._timestamps = [timestamp for timestamp in self._timestamps if timestamp > cutoff]
            if len(self._timestamps) >= self.rpm:
                logger.warning("[RL] %s: rate limit reached (%d rpm)", self.provider, self.rpm)
                return False
            self._timestamps.append(now)
            return True

    def current_rpm(self) -> int:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            return sum(1 for timestamp in self._timestamps if timestamp > cutoff)


class InProcessRateLimiterPool:
    """Pool of in-process rate limiters, one per provider."""

    def __init__(self, overrides: dict[str, int] | None = None):
        limits = {**_PROVIDER_RPM, **(overrides or {})}
        self._limiters: dict[str, InProcessRateLimiter] = {
            provider: InProcessRateLimiter(provider, rpm)
            for provider, rpm in limits.items()
        }

    def _get(self, provider: str) -> InProcessRateLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = InProcessRateLimiter(provider, _DEFAULT_RPM)
        return self._limiters[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def status(self) -> dict[str, int]:
        return {provider: limiter.current_rpm() for provider, limiter in self._limiters.items()}


_REDIS_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if count > tonumber(ARGV[1]) then
  return 0
end
return 1
"""


class RedisRateLimiter:
    """Fixed-window Redis RPM limiter using atomic INCR with TTL."""

    def __init__(self, redis_client: Any, key_prefix: str, rpm: int):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.rpm = rpm

    def _key(self, provider: str) -> str:
        window = int(time.time() // 60)
        return f"{self.key_prefix}{provider}:{window}"

    async def acquire(self, provider: str) -> bool:
        allowed = await self.redis.eval(
            _REDIS_RATE_LIMIT_LUA,
            1,
            self._key(provider),
            int(self.rpm),
            60,
        )
        if not bool(int(allowed)):
            logger.warning("[RL] %s: Redis rate limit reached (%d rpm)", provider, self.rpm)
            return False
        return True


class RedisRateLimiterPool:
    """Rate limiter pool backed by Redis counters."""

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str,
        overrides: dict[str, int] | None = None,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self._limits = {**_PROVIDER_RPM, **(overrides or {})}
        self._limiters: dict[str, RedisRateLimiter] = {}
        self._seen_counts: dict[str, int] = defaultdict(int)

    def _get(self, provider: str) -> RedisRateLimiter:
        if provider not in self._limiters:
            rpm = self._limits.get(provider, _DEFAULT_RPM)
            self._limiters[provider] = RedisRateLimiter(self.redis, self.key_prefix, rpm)
        return self._limiters[provider]

    async def is_allowed(self, provider: str) -> bool:
        self._seen_counts[provider] += 1
        return await self._get(provider).acquire(provider)

    async def acquire(self, provider: str) -> bool:
        return await self.is_allowed(provider)

    def status(self) -> dict[str, int]:
        return dict(self._seen_counts)


class InProcessProviderConcurrencyLimiter:
    """asyncio.Semaphore-backed slot limiter for one provider."""

    def __init__(self, provider: str, max_concurrent: int):
        self.provider = provider
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0

    def is_available(self) -> bool:
        return self._sem._value > 0  # type: ignore[attr-defined]

    async def acquire(self) -> bool:
        if self.is_available():
            await self._sem.acquire()
            self._in_flight += 1
            return True
        logger.info("[CONC] %s: all %d slots occupied; skipping", self.provider, self.max_concurrent)
        return False

    def release(self) -> None:
        self._sem.release()
        self._in_flight = max(0, self._in_flight - 1)

    def status(self) -> dict[str, int]:
        return {"in_flight": self._in_flight, "max": self.max_concurrent}


class InProcessConcurrencyLimiterPool:
    """Pool of in-process concurrency limiters, one per provider."""

    def __init__(self, overrides: dict[str, int] | None = None):
        slots = {**_PROVIDER_SLOTS, **(overrides or {})}
        self._limiters: dict[str, InProcessProviderConcurrencyLimiter] = {
            provider: InProcessProviderConcurrencyLimiter(provider, max_concurrent)
            for provider, max_concurrent in slots.items()
        }

    def _get(self, provider: str) -> InProcessProviderConcurrencyLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = InProcessProviderConcurrencyLimiter(provider, _DEFAULT_SLOTS)
        return self._limiters[provider]

    def is_available(self, provider: str) -> bool:
        return self._get(provider).is_available()

    async def acquire(self, provider: str) -> bool:
        return await self._get(provider).acquire()

    def release(self, provider: str) -> None:
        self._get(provider).release()

    @asynccontextmanager
    async def reserve(self, provider: str):
        acquired = await self.acquire(provider)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(provider)

    def status(self) -> dict[str, dict[str, int]]:
        return {provider: limiter.status() for provider, limiter in self._limiters.items()}


_REDIS_CONCURRENCY_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]))
local current = redis.call('ZCARD', KEYS[1])
if current >= tonumber(ARGV[3]) then
  return 0
end
redis.call('ZADD', KEYS[1], tonumber(ARGV[2]), ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil((tonumber(ARGV[2]) - tonumber(ARGV[1])) * 1000))
return 1
"""


class RedisConcurrencyLimiter:
    """Redis sorted-set slot limiter shared across worker processes."""

    def __init__(self, redis_client: Any, key_prefix: str, max_concurrent: int):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.max_concurrent = max_concurrent
        self.lease_seconds = _CONCURRENCY_LEASE_SECONDS

    def _key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}:slots"

    async def acquire(self, provider: str) -> str | None:
        now = time.time()
        expires_at = now + self.lease_seconds
        token = f"{uuid.uuid4().hex}:{provider}"
        allowed = await self.redis.eval(
            _REDIS_CONCURRENCY_ACQUIRE_LUA,
            1,
            self._key(provider),
            now,
            expires_at,
            int(self.max_concurrent),
            token,
        )
        if not bool(int(allowed)):
            logger.info(
                "[CONC] %s: all %d Redis slots occupied; skipping",
                provider,
                self.max_concurrent,
            )
            return None
        return token

    async def release(self, provider: str, token: str) -> None:
        await self.redis.zrem(self._key(provider), token)

    @asynccontextmanager
    async def reserve(self, provider: str):
        token = await self.acquire(provider)
        try:
            yield token is not None
        finally:
            if token is not None:
                await self.release(provider, token)


class RedisConcurrencyLimiterPool:
    """Concurrency limiter pool backed by Redis slot leases."""

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str,
        overrides: dict[str, int] | None = None,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self._slots = {**_PROVIDER_SLOTS, **(overrides or {})}
        self._limiters: dict[str, RedisConcurrencyLimiter] = {}
        self._tokens: dict[str, deque[str]] = defaultdict(deque)

    def _max_concurrent(self, provider: str) -> int:
        return self._slots.get(provider, _DEFAULT_SLOTS)

    def _get(self, provider: str) -> RedisConcurrencyLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = RedisConcurrencyLimiter(
                self.redis,
                self.key_prefix,
                self._max_concurrent(provider),
            )
        return self._limiters[provider]

    def is_available(self, provider: str) -> bool:
        return len(self._tokens[provider]) < self._max_concurrent(provider)

    async def acquire(self, provider: str) -> bool:
        token = await self._get(provider).acquire(provider)
        if token is None:
            return False
        self._tokens[provider].append(token)
        return True

    async def release(self, provider: str) -> None:
        if not self._tokens[provider]:
            return
        token = self._tokens[provider].pop()
        await self._get(provider).release(provider, token)

    @asynccontextmanager
    async def reserve(self, provider: str):
        acquired = await self.acquire(provider)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release(provider)

    def status(self) -> dict[str, dict[str, int]]:
        providers = set(self._tokens) | set(self._limiters)
        return {
            provider: {
                "in_flight": len(self._tokens[provider]),
                "max": self._max_concurrent(provider),
            }
            for provider in sorted(providers)
        }


def _storage_uri(settings: Any) -> str:
    return getattr(settings.rate_limit, "storage_uri", getattr(settings.rate_limit, "storage", "memory://"))


def _redis_requested(settings: Any) -> bool:
    return _storage_uri(settings).startswith(("redis://", "rediss://"))


def _fallback_warning_enabled(settings: Any) -> bool:
    resilience = getattr(settings, "resilience", None)
    return bool(getattr(resilience, "fallback_warning", True))


def _get_lifecycle_redis_client() -> Any | None:
    try:
        from mnemos.core.lifecycle import get_redis_client
    except Exception as exc:
        logger.debug("Redis lifecycle accessor unavailable: %s", exc)
        return None
    return get_redis_client()


def _warn_fallback(settings: Any, reason: str) -> None:
    if _fallback_warning_enabled(settings):
        logger.warning(
            "%s; falling back to in-process resilience primitives. "
            "Multi-worker deployments require Redis.",
            reason,
        )


def make_circuit_breaker_pool(
    settings: Any,
    *,
    failure_threshold: int = 5,
    cooldown_seconds: int = 300,
    redis_client: Any | None = None,
) -> InProcessCircuitBreakerPool | RedisCircuitBreakerPool:
    if _redis_requested(settings):
        client = redis_client if redis_client is not None else _get_lifecycle_redis_client()
        if client is not None:
            return RedisCircuitBreakerPool(
                client,
                settings.resilience.circuit_breaker_redis_prefix,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
        _warn_fallback(settings, "Redis resilience backend requested but unavailable")
    else:
        _warn_fallback(settings, "Redis not configured")
    return InProcessCircuitBreakerPool(
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )


def make_rate_limiter_pool(
    settings: Any,
    *,
    overrides: dict[str, int] | None = None,
    redis_client: Any | None = None,
) -> InProcessRateLimiterPool | RedisRateLimiterPool:
    if _redis_requested(settings):
        client = redis_client if redis_client is not None else _get_lifecycle_redis_client()
        if client is not None:
            return RedisRateLimiterPool(
                client,
                settings.resilience.rate_limiter_redis_prefix,
                overrides=overrides,
            )
        _warn_fallback(settings, "Redis resilience backend requested but unavailable")
    else:
        _warn_fallback(settings, "Redis not configured")
    return InProcessRateLimiterPool(overrides=overrides)


def make_concurrency_limiter(
    settings: Any,
    *,
    overrides: dict[str, int] | None = None,
    redis_client: Any | None = None,
) -> InProcessConcurrencyLimiterPool | RedisConcurrencyLimiterPool:
    if _redis_requested(settings):
        client = redis_client if redis_client is not None else _get_lifecycle_redis_client()
        if client is not None:
            return RedisConcurrencyLimiterPool(
                client,
                settings.resilience.concurrency_redis_prefix,
                overrides=overrides,
            )
        _warn_fallback(settings, "Redis resilience backend requested but unavailable")
    else:
        _warn_fallback(settings, "Redis not configured")
    return InProcessConcurrencyLimiterPool(overrides=overrides)


CircuitBreaker = InProcessCircuitBreaker
CircuitBreakerPool = InProcessCircuitBreakerPool
RateLimiter = InProcessRateLimiter
RateLimiterPool = InProcessRateLimiterPool
ProviderConcurrencyLimiter = InProcessProviderConcurrencyLimiter
ConcurrencyLimiterPool = InProcessConcurrencyLimiterPool

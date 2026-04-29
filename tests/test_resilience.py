from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from mnemos.core.resilience import (
    InProcessCircuitBreakerPool,
    InProcessConcurrencyLimiterPool,
    InProcessRateLimiter,
    InProcessRateLimiterPool,
    RedisCircuitBreakerPool,
    RedisConcurrencyLimiterPool,
    RedisRateLimiterPool,
    call_maybe_async,
    make_circuit_breaker_pool,
    make_concurrency_limiter,
    make_rate_limiter_pool,
)


class _FakeAsyncRedis:
    def __init__(self):
        self._strings: dict[str, tuple[str, float | None]] = {}
        self._hashes: dict[str, tuple[dict[str, str], float | None]] = {}
        self._zsets: dict[str, tuple[dict[str, float], float | None]] = {}
        self.closed = False

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= time.monotonic()

    def _cleanup_key(self, key: str) -> None:
        value = self._strings.get(key)
        if value and self._expired(value[1]):
            self._strings.pop(key, None)
        value = self._hashes.get(key)
        if value and self._expired(value[1]):
            self._hashes.pop(key, None)
        value = self._zsets.get(key)
        if value and self._expired(value[1]):
            self._zsets.pop(key, None)

    async def get(self, key: str):
        self._cleanup_key(key)
        value = self._strings.get(key)
        return value[0] if value else None

    async def zrem(self, key: str, token: str):
        self._cleanup_key(key)
        members, expires_at = self._zsets.get(key, ({}, None))
        members.pop(token, None)
        self._zsets[key] = (members, expires_at)
        return 1

    async def eval(self, script: str, numkeys: int, *keys_and_args):
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        if "opened_at" in script:
            return self._eval_circuit_failure(keys, args)
        if "redis.call('DEL', KEYS[3])" in script:
            return self._eval_circuit_success(keys)
        if "ZREMRANGEBYSCORE" in script:
            return self._eval_concurrency_acquire(keys, args)
        return self._eval_rate_limit(keys, args)

    def _expire_at(self, seconds: int | float) -> float | None:
        seconds = float(seconds)
        return time.monotonic() + seconds if seconds > 0 else time.monotonic()

    def _eval_circuit_failure(self, keys, args):
        failures_key, state_key, hash_key = keys
        threshold, cooldown, opened_at = int(args[0]), int(args[1]), str(args[2])
        self._cleanup_key(failures_key)
        current = int(self._strings.get(failures_key, ("0", None))[0]) + 1
        expires_at = self._expire_at(cooldown)
        self._strings[failures_key] = (str(current), expires_at)
        self._hashes[hash_key] = ({"state": "closed", "failures": str(current)}, expires_at)
        if current >= threshold:
            self._strings[state_key] = ("open", expires_at)
            self._hashes[hash_key] = (
                {"state": "open", "failures": str(current), "opened_at": opened_at},
                expires_at,
            )
            return [1, current]
        return [0, current]

    def _eval_circuit_success(self, keys):
        for key in keys:
            self._strings.pop(key, None)
            self._hashes.pop(key, None)
        return 1

    def _eval_rate_limit(self, keys, args):
        key = keys[0]
        limit, ttl = int(args[0]), int(args[1])
        self._cleanup_key(key)
        count = int(self._strings.get(key, ("0", None))[0]) + 1
        expires_at = self._strings.get(key, ("0", None))[1]
        if count == 1:
            expires_at = self._expire_at(ttl)
        self._strings[key] = (str(count), expires_at)
        return 1 if count <= limit else 0

    def _eval_concurrency_acquire(self, keys, args):
        key = keys[0]
        now, expires_at, max_concurrent, token = (
            float(args[0]),
            float(args[1]),
            int(args[2]),
            str(args[3]),
        )
        self._cleanup_key(key)
        members, _old_expires_at = self._zsets.get(key, ({}, None))
        members = {member: score for member, score in members.items() if score > now}
        if len(members) >= max_concurrent:
            self._zsets[key] = (members, expires_at)
            return 0
        members[token] = expires_at
        self._zsets[key] = (members, expires_at)
        return 1


def _settings(storage_uri: str = "memory://", *, fallback_warning: bool = False):
    return SimpleNamespace(
        rate_limit=SimpleNamespace(storage_uri=storage_uri),
        resilience=SimpleNamespace(
            circuit_breaker_redis_prefix="test:cb:",
            rate_limiter_redis_prefix="test:rl:",
            concurrency_redis_prefix="test:conc:",
            fallback_warning=fallback_warning,
        ),
        server=SimpleNamespace(redis_url="redis://cache:6379/0"),
        federation=SimpleNamespace(peers="", enabled=False),
    )


def test_in_process_circuit_breaker_opens_after_threshold():
    pool = InProcessCircuitBreakerPool(failure_threshold=2, cooldown_seconds=60)

    assert pool.is_allowed("openai")
    pool.record_failure("openai")
    assert pool.is_allowed("openai")
    pool.record_failure("openai")

    assert not pool.is_allowed("openai")
    assert pool.status()["openai"]["state"] == "open"


def test_in_process_circuit_breaker_allows_probe_after_cooldown():
    pool = InProcessCircuitBreakerPool(failure_threshold=1, cooldown_seconds=0.01)

    pool.record_failure("openai")
    assert not pool.is_allowed("openai")
    time.sleep(0.02)

    assert pool.is_allowed("openai")
    pool.record_success("openai")
    pool.record_success("openai")
    assert pool.status()["openai"]["state"] == "closed"


def test_in_process_rate_limiter_blocks_above_rpm():
    limiter = InProcessRateLimiter("openai", rpm=2)

    assert limiter.is_allowed()
    assert limiter.is_allowed()
    assert not limiter.is_allowed()


def test_in_process_rate_limiter_pool_uses_default_for_unknown_provider():
    pool = InProcessRateLimiterPool(overrides={"openai": 1})

    assert pool.is_allowed("new-provider")
    assert pool.status()["new-provider"] == 1


def test_in_process_concurrency_limiter_limits_concurrent_acquires():
    async def run():
        pool = InProcessConcurrencyLimiterPool(overrides={"openai": 1})
        assert await pool.acquire("openai")
        assert not await pool.acquire("openai")
        pool.release("openai")
        assert await pool.acquire("openai")
        pool.release("openai")

    asyncio.run(run())


def test_redis_circuit_breaker_opens_across_two_pools():
    async def run():
        redis = _FakeAsyncRedis()
        first = RedisCircuitBreakerPool(redis, "test:cb:", failure_threshold=2, cooldown_seconds=60)
        second = RedisCircuitBreakerPool(redis, "test:cb:", failure_threshold=2, cooldown_seconds=60)

        await first.record_failure("openai")
        assert await second.is_allowed("openai")
        await first.record_failure("openai")

        assert not await second.is_allowed("openai")

    asyncio.run(run())


def test_redis_circuit_breaker_success_clears_shared_open_state():
    async def run():
        redis = _FakeAsyncRedis()
        first = RedisCircuitBreakerPool(redis, "test:cb:", failure_threshold=1, cooldown_seconds=60)
        second = RedisCircuitBreakerPool(redis, "test:cb:", failure_threshold=1, cooldown_seconds=60)

        await first.record_failure("openai")
        assert not await second.is_allowed("openai")
        await second.record_success("openai")

        assert await second.is_allowed("openai")

    asyncio.run(run())


def test_redis_rate_limiter_rpm_is_shared_across_pools():
    async def run():
        redis = _FakeAsyncRedis()
        first = RedisRateLimiterPool(redis, "test:rl:", overrides={"openai": 2})
        second = RedisRateLimiterPool(redis, "test:rl:", overrides={"openai": 2})

        assert await first.is_allowed("openai")
        assert await second.is_allowed("openai")
        assert not await first.is_allowed("openai")

    asyncio.run(run())


def test_redis_concurrency_limiter_slots_are_shared_across_pools():
    async def run():
        redis = _FakeAsyncRedis()
        first = RedisConcurrencyLimiterPool(redis, "test:conc:", overrides={"openai": 1})
        second = RedisConcurrencyLimiterPool(redis, "test:conc:", overrides={"openai": 1})

        assert await first.acquire("openai")
        assert not await second.acquire("openai")
        await first.release("openai")
        assert await second.acquire("openai")
        await second.release("openai")

    asyncio.run(run())


def test_concurrency_reserve_context_releases_slot():
    async def run():
        pool = InProcessConcurrencyLimiterPool(overrides={"openai": 1})
        async with pool.reserve("openai") as acquired:
            assert acquired
            assert not await pool.acquire("openai")
        assert await pool.acquire("openai")
        pool.release("openai")

    asyncio.run(run())


def test_call_maybe_async_supports_sync_and_async_methods():
    async def run():
        async def async_value():
            return "async"

        assert await call_maybe_async(lambda: "sync") == "sync"
        assert await call_maybe_async(async_value) == "async"

    asyncio.run(run())


def test_factory_returns_redis_backends_when_client_available():
    redis = _FakeAsyncRedis()
    settings = _settings("redis://redis:6379/0")

    assert isinstance(make_circuit_breaker_pool(settings, redis_client=redis), RedisCircuitBreakerPool)
    assert isinstance(make_rate_limiter_pool(settings, redis_client=redis), RedisRateLimiterPool)
    assert isinstance(make_concurrency_limiter(settings, redis_client=redis), RedisConcurrencyLimiterPool)


def test_factory_memory_uri_returns_in_process_and_warns(caplog):
    settings = _settings("memory://", fallback_warning=True)
    caplog.set_level(logging.WARNING)

    pool = make_circuit_breaker_pool(settings)

    assert isinstance(pool, InProcessCircuitBreakerPool)
    assert "Redis not configured" in caplog.text
    assert "Multi-worker deployments require Redis" in caplog.text


def test_factory_redis_uri_without_client_falls_back_with_warning(caplog, monkeypatch):
    from mnemos.core import resilience

    settings = _settings("redis://redis:6379/0", fallback_warning=True)
    monkeypatch.setattr(resilience, "_get_lifecycle_redis_client", lambda: None)
    caplog.set_level(logging.WARNING)

    pool = make_rate_limiter_pool(settings)

    assert isinstance(pool, InProcessRateLimiterPool)
    assert "Redis resilience backend requested but unavailable" in caplog.text


def test_lifecycle_redis_unreachable_degrades_to_no_resilience_client(monkeypatch, caplog):
    async def run():
        from mnemos.core import lifecycle

        class FalsyPool:
            def __bool__(self):
                return False

            async def close(self):
                return None

        class RedisUnavailable:
            async def ping(self):
                raise RuntimeError("redis down")

            async def aclose(self):
                return None

        async def create_pool(**_kwargs):
            return FalsyPool()

        settings = _settings("redis://redis:6379/0")
        monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
        monkeypatch.setattr(lifecycle, "_load_config", lambda: {"worker": {"enabled": False}})
        monkeypatch.setattr(lifecycle, "_background_tasks", set())
        monkeypatch.setattr(lifecycle, "_worker_tasks", set())
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setattr(lifecycle, "_lifespan_worker_factories", {})
        monkeypatch.setattr(lifecycle, "_provider_manifest_reloader", None)
        monkeypatch.setattr(lifecycle.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(lifecycle.aioredis, "from_url", lambda *_args, **_kwargs: RedisUnavailable())

        app = SimpleNamespace(state=SimpleNamespace())
        async with lifecycle.lifespan(app):
            assert lifecycle.get_redis_client() is None
            assert app.state.redis_client is None

    caplog.set_level(logging.WARNING)
    asyncio.run(run())

    assert "Redis resilience backend unavailable" in caplog.text

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mnemos.domain.graeae._cache import RedisResponseCache, make_response_cache
from mnemos.domain.graeae._quality import (
    RedisQualityTracker,
    _REDIS_QUALITY_EWMA_LUA,
    make_quality_tracker,
)


def _settings(storage_uri: str = "redis://redis:6379/1"):
    return SimpleNamespace(
        rate_limit=SimpleNamespace(storage_uri=storage_uri),
        resilience=SimpleNamespace(
            fallback_warning=False,
            circuit_breaker_redis_prefix="test:cb:",
            rate_limiter_redis_prefix="test:rl:",
            concurrency_redis_prefix="test:conc:",
        ),
    )


@pytest.mark.asyncio
async def test_redis_response_cache_get_and_set_use_stable_key_shape():
    redis = SimpleNamespace(get=AsyncMock(), set=AsyncMock(), mget=AsyncMock())
    payload = {"openai": {"status": "success", "response_text": "ok"}}
    redis.get.return_value = json.dumps(payload)
    cache = RedisResponseCache(redis, ttl_seconds=123)

    result = await cache.get("Hello", "reasoning", "gpt-5")
    await cache.set("Hello", "reasoning", payload, "gpt-5")

    expected_digest = hashlib.sha256("hello:gpt-5:reasoning".encode()).hexdigest()
    expected_key = f"mnemos:graeae:cache:{expected_digest}"
    assert result == payload
    redis.get.assert_awaited_once_with(expected_key)
    redis.set.assert_awaited_once_with(
        expected_key,
        json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
        ex=123,
    )


@pytest.mark.asyncio
async def test_redis_response_cache_batch_lookup_uses_mget():
    redis = SimpleNamespace(
        get=AsyncMock(),
        set=AsyncMock(),
        mget=AsyncMock(return_value=[json.dumps({"a": 1}), None]),
    )
    cache = RedisResponseCache(redis)

    found = await cache.get_many([
        ("p1", "reasoning", "m1"),
        ("p2", "reasoning", "m2"),
    ])

    assert found == {("p1", "reasoning", "m1"): {"a": 1}}
    redis.mget.assert_awaited_once()
    assert all(key.startswith("mnemos:graeae:cache:") for key in redis.mget.await_args.args[0])


@pytest.mark.asyncio
async def test_redis_quality_tracker_updates_provider_zset_with_lua():
    redis = SimpleNamespace(eval=AsyncMock(return_value=[0.8, 120.0]), zscore=AsyncMock(return_value=0.8))
    tracker = RedisQualityTracker(redis, {"openai": 0.9})

    await tracker.record_success("openai", 120)
    weight = await tracker.dynamic_weight("openai")

    args = redis.eval.await_args.args
    assert args[0] == _REDIS_QUALITY_EWMA_LUA
    assert args[1] == 1
    assert args[2] == "mnemos:graeae:quality:openai"
    assert args[3:] == (1.0, 120, 0.2, 0.9)
    assert "ZADD" in args[0]
    redis.zscore.assert_awaited_once_with("mnemos:graeae:quality:openai", "success_ewma")
    assert weight == 0.81


def test_graeae_state_factories_select_redis_when_configured():
    redis = SimpleNamespace()
    settings = _settings()

    assert isinstance(make_response_cache(settings, redis_client=redis), RedisResponseCache)
    assert isinstance(make_quality_tracker(settings, {"openai": 0.9}, redis_client=redis), RedisQualityTracker)

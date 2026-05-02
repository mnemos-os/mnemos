from __future__ import annotations

"""Response cache backends for the GRAEAE engine.

Per-process LRU cache keyed on sha256(task_type + normalized_prompt).
No cross-process sharing (4 uvicorn workers = 4 independent caches) —
this is an acceptable tradeoff: cache warmup is fast since LLM round-trips
are the bottleneck, and avoiding shared-state complexity is worth it.

Key design choices:
- Exact-match on normalized prompt (lowercased, stripped). Semantic/embedding
  similarity lookup is not used here — the embedding round-trip adds latency
  comparable to skipping the cache for the less-common near-duplicate case.
- TTL-based expiry (default 1 hour). Architectural questions don't change
  minute-to-minute; caching them avoids redundant API spend.
- LRU eviction at max_entries to bound memory usage.
"""
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from mnemos.core.resilience import (
    _get_lifecycle_redis_client,
    _redis_requested,
    _warn_fallback,
)

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600   # seconds
_MAX_ENTRIES = 500
_REDIS_KEY_PREFIX = "mnemos:graeae:cache:"


class ResponseCache:
    """Thread-safe LRU cache for GRAEAE consensus responses."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL, max_entries: int = _MAX_ENTRIES):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, prompt: str, task_type: str, model: str = "") -> str:
        normalized = f"{task_type}:{model}:{prompt.strip().lower()}"
        return hashlib.sha256(normalized.encode()).hexdigest()

    def get(self, prompt: str, task_type: str, model: str = "") -> Any | None:
        key = self._key(prompt, task_type, model)
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return None
            stored_at, value = self._store[key]
            if time.monotonic() - stored_at > self.ttl:
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, prompt: str, task_type: str, value: Any, model: str = "") -> None:
        key = self._key(prompt, task_type, model)
        with self._lock:
            self._store[key] = (time.monotonic(), value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


class RedisResponseCache:
    """Redis-backed response cache shared across worker processes."""

    def __init__(
        self,
        redis_client: Any,
        ttl_seconds: int = _DEFAULT_TTL,
        key_prefix: str = _REDIS_KEY_PREFIX,
    ):
        self.redis = redis_client
        self.ttl = ttl_seconds
        self.key_prefix = key_prefix
        self._hits = 0
        self._misses = 0

    def _key(self, prompt: str, task_type: str, model: str = "") -> str:
        digest = hashlib.sha256(
            f"{prompt.strip().lower()}:{model}:{task_type}".encode()
        ).hexdigest()
        return f"{self.key_prefix}{digest}"

    async def get(self, prompt: str, task_type: str, model: str = "") -> Any | None:
        raw = await self.redis.get(self._key(prompt, task_type, model))
        if raw is None:
            self._misses += 1
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        self._hits += 1
        return json.loads(raw)

    async def set(self, prompt: str, task_type: str, value: Any, model: str = "") -> None:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
        await self.redis.set(self._key(prompt, task_type, model), payload, ex=self.ttl)

    async def get_many(
        self,
        items: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], Any]:
        if not items:
            return {}
        keys = [self._key(prompt, task_type, model) for prompt, task_type, model in items]
        values = await self.redis.mget(keys)
        found: dict[tuple[str, str, str], Any] = {}
        for item, raw in zip(items, values):
            if raw is None:
                self._misses += 1
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            self._hits += 1
            found[item] = json.loads(raw)
        return found

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "backend": "redis",
            "entries": None,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }


def make_response_cache(
    settings: Any,
    *,
    ttl_seconds: int = _DEFAULT_TTL,
    max_entries: int = _MAX_ENTRIES,
    redis_client: Any | None = None,
) -> ResponseCache | RedisResponseCache:
    if _redis_requested(settings):
        client = redis_client if redis_client is not None else _get_lifecycle_redis_client()
        if client is not None:
            return RedisResponseCache(client, ttl_seconds=ttl_seconds)
        _warn_fallback(settings, "Redis response cache requested but unavailable")
    else:
        _warn_fallback(settings, "Redis not configured")
    return ResponseCache(ttl_seconds=ttl_seconds, max_entries=max_entries)

from __future__ import annotations

"""In-memory response cache for the GRAEAE engine.

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
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600   # seconds
_MAX_ENTRIES = 500


class ResponseCache:
    """Thread-safe LRU cache for GRAEAE consensus responses."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL, max_entries: int = _MAX_ENTRIES):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, prompt: str, task_type: str) -> str:
        normalized = f"{task_type}:{prompt.strip().lower()}"
        return hashlib.sha256(normalized.encode()).hexdigest()

    def get(self, prompt: str, task_type: str) -> Any | None:
        key = self._key(prompt, task_type)
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

    def set(self, prompt: str, task_type: str, value: Any) -> None:
        key = self._key(prompt, task_type)
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

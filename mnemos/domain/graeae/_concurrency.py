from __future__ import annotations

"""Per-provider concurrency limiter for the GRAEAE engine.

Inspired by Triton Inference Server's per-backend instance-slot model:
instead of queuing requests for a busy provider, we shed load gracefully —
a provider that has hit its concurrency limit is skipped for this request
and the engine returns the best response from remaining available providers.

This prevents slow/overloaded providers from holding open N connections and
blocking asyncio.gather until they all time out. The circuit breaker handles
sustained failures; this handles transient saturation.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# Max concurrent in-flight requests per provider.
# Tune conservatively: most LLM API calls are 2-5s; 3 concurrent = 15s max exposure.
_PROVIDER_SLOTS: dict[str, int] = {
    "perplexity":  3,
    "groq":        4,
    "claude_opus": 3,
    "xai":         3,
    "openai":      3,
    "gemini":      3,
    "nvidia":      3,
    "together":    3,
}
_DEFAULT_SLOTS = 3


class ProviderConcurrencyLimiter:
    """asyncio.Semaphore-backed slot limiter for one provider."""

    def __init__(self, provider: str, max_concurrent: int):
        self.provider = provider
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0
        self._lock = asyncio.Lock()

    def is_available(self) -> bool:
        """Non-blocking check: True if at least one slot is free."""
        # Semaphore._value is internal but reliable across CPython asyncio.
        return self._sem._value > 0  # type: ignore[attr-defined]

    async def acquire(self) -> bool:
        """Try to acquire a slot without waiting. Returns False if none available."""
        if self.is_available():
            await self._sem.acquire()
            self._in_flight += 1
            return True
        logger.info(
            f"[CONC] {self.provider}: all {self.max_concurrent} slots occupied — skipping"
        )
        return False

    def release(self) -> None:
        self._sem.release()
        self._in_flight = max(0, self._in_flight - 1)

    def status(self) -> dict:
        return {"in_flight": self._in_flight, "max": self.max_concurrent}


class ConcurrencyLimiterPool:
    """Pool of concurrency limiters, one per provider.

    Instantiated inside the engine's async lifespan (first consult call)
    rather than at module import time, because asyncio.Semaphore must be
    created within a running event loop in Python < 3.10.
    """

    def __init__(self, overrides: dict[str, int] | None = None):
        slots = {**_PROVIDER_SLOTS, **(overrides or {})}
        self._limiters: dict[str, ProviderConcurrencyLimiter] = {
            p: ProviderConcurrencyLimiter(p, s) for p, s in slots.items()
        }

    def _get(self, provider: str) -> ProviderConcurrencyLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = ProviderConcurrencyLimiter(provider, _DEFAULT_SLOTS)
        return self._limiters[provider]

    def is_available(self, provider: str) -> bool:
        return self._get(provider).is_available()

    async def acquire(self, provider: str) -> bool:
        return await self._get(provider).acquire()

    def release(self, provider: str) -> None:
        self._get(provider).release()

    def status(self) -> dict[str, dict]:
        return {p: lim.status() for p, lim in self._limiters.items()}

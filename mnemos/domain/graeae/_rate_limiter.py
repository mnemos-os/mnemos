from __future__ import annotations

"""Per-provider sliding-window rate limiter for the GRAEAE engine.

In-memory only — resets on process restart, which is intentional: rate limit
state is volatile operational data, not something that needs to survive deploys.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Conservative defaults well below typical provider free-tier limits.
# These exist to prevent us from hammering a provider during a retry storm,
# not to mirror the provider's actual quota (they will 429 us first if we exceed it).
_PROVIDER_RPM: dict[str, int] = {
    "perplexity":  50,
    "groq":        60,
    "claude_opus": 40,
    "xai":         30,
    "openai":      60,
    "gemini":      60,
    "nvidia":      50,
    "together":    60,
}
_DEFAULT_RPM = 50


class RateLimiter:
    """Sliding-window rate limiter for a single provider."""

    def __init__(self, provider: str, rpm: int):
        self.provider = provider
        self.rpm = rpm
        self._window = 60.0
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """Check and record a request. Returns False if rate limit exceeded."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            # Trim expired entries
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self.rpm:
                logger.warning(f"[RL] {self.provider}: rate limit reached ({self.rpm} rpm)")
                return False
            self._timestamps.append(now)
            return True

    def current_rpm(self) -> int:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            return sum(1 for t in self._timestamps if t > cutoff)


class RateLimiterPool:
    """Pool of rate limiters, one per provider."""

    def __init__(self, overrides: dict[str, int] | None = None):
        limits = {**_PROVIDER_RPM, **(overrides or {})}
        self._limiters: dict[str, RateLimiter] = {
            p: RateLimiter(p, rpm) for p, rpm in limits.items()
        }

    def _get(self, provider: str) -> RateLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = RateLimiter(provider, _DEFAULT_RPM)
        return self._limiters[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def status(self) -> dict[str, int]:
        return {p: rl.current_rpm() for p, rl in self._limiters.items()}

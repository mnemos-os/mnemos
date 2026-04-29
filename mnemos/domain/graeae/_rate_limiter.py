from __future__ import annotations

"""Compatibility exports for the in-process GRAEAE rate limiter."""

from mnemos.core.resilience import (
    InProcessRateLimiter as RateLimiter,
    InProcessRateLimiterPool as RateLimiterPool,
)

__all__ = ["RateLimiter", "RateLimiterPool"]

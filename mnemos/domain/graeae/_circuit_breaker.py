from __future__ import annotations

"""Compatibility exports for the in-process GRAEAE circuit breaker."""

from mnemos.core.resilience import (
    CircuitState,
    InProcessCircuitBreaker as CircuitBreaker,
    InProcessCircuitBreakerPool as CircuitBreakerPool,
)

__all__ = ["CircuitBreaker", "CircuitBreakerPool", "CircuitState"]

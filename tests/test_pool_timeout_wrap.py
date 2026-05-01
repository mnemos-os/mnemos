"""Regression tests for the ``TimeoutPool`` proxy.

Corpus review #6 (CORPUS-REVIEW-2026-04-29) flagged that 86+ raw
``_lc._pool.acquire()`` call sites bypassed the configured acquire
timeout, so under pool exhaustion they piled up indefinitely. The
fix wraps the asyncpg pool at lifecycle creation with
``mnemos.core.pool.TimeoutPool``, which injects the default timeout
into every ``.acquire()`` call. These tests pin the wrapper's
contract:

  * No-kwarg ``acquire()`` injects the default timeout.
  * Explicit ``timeout=...`` overrides the default.
  * Other pool methods pass through to the wrapped object.
  * The ``PoolManager`` shim continues to work against the wrapped
    pool (it passes timeout explicitly via kwarg).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from mnemos.core.pool import (
    DEFAULT_ACQUIRE_TIMEOUT,
    PoolManager,
    TimeoutPool,
    wrap_pool_with_timeout,
)


class _FakeAcquireCtx:
    """Sentinel returned by the underlying pool's acquire(); we
    don't actually enter the context manager — the assertion is
    on what timeout the wrapped acquire() was called with."""


class _FakePool:
    def __init__(self):
        self.acquire_calls: list[dict] = []
        self.release_calls: list[object] = []
        self.terminate_called = False

    def acquire(self, *args, **kwargs):
        self.acquire_calls.append({"args": args, "kwargs": kwargs})
        return _FakeAcquireCtx()

    def release(self, conn):
        self.release_calls.append(conn)

    def terminate(self):
        self.terminate_called = True


def test_acquire_no_kwargs_uses_default_timeout():
    inner = _FakePool()
    proxy = TimeoutPool(inner)
    proxy.acquire()
    assert len(inner.acquire_calls) == 1
    call = inner.acquire_calls[0]
    assert call["kwargs"] == {"timeout": DEFAULT_ACQUIRE_TIMEOUT}


def test_acquire_explicit_timeout_overrides_default():
    inner = _FakePool()
    proxy = TimeoutPool(inner)
    proxy.acquire(timeout=42)
    call = inner.acquire_calls[0]
    assert call["kwargs"] == {"timeout": 42}


def test_acquire_explicit_zero_timeout_passes_through():
    """Zero is a meaningful value — fail fast — and must not be
    rewritten to the default."""
    inner = _FakePool()
    proxy = TimeoutPool(inner)
    proxy.acquire(timeout=0)
    call = inner.acquire_calls[0]
    assert call["kwargs"] == {"timeout": 0}


def test_proxy_default_timeout_can_be_customised():
    inner = _FakePool()
    proxy = TimeoutPool(inner, default_timeout=7.5)
    proxy.acquire()
    call = inner.acquire_calls[0]
    assert call["kwargs"] == {"timeout": 7.5}


def test_proxy_passes_through_release():
    inner = _FakePool()
    proxy = TimeoutPool(inner)
    sentinel = object()
    proxy.release(sentinel)
    assert inner.release_calls == [sentinel]


def test_proxy_passes_through_terminate():
    inner = _FakePool()
    proxy = TimeoutPool(inner)
    proxy.terminate()
    assert inner.terminate_called is True


def test_pool_manager_works_against_wrapped_pool():
    """PoolManager.acquire() uses ``self._pool.acquire(timeout=...)``.
    When wrapped through TimeoutPool, the inner asyncpg pool still
    receives the kwarg timeout the manager intended — wrapping does
    not break the manager's own acquire-timeout enforcement.

    Direct unit assertion: PoolManager construction is benign on
    a wrapped pool, and a passthrough acquire(timeout=5) reaches
    the inner pool with the explicit timeout intact.
    """
    inner = _FakePool()
    proxy = TimeoutPool(inner, default_timeout=5)
    PoolManager(proxy)  # construction must not raise
    proxy.acquire(timeout=5)
    assert inner.acquire_calls[-1]["kwargs"] == {"timeout": 5}


def test_wrap_pool_with_timeout_helper():
    inner = _FakePool()
    out = wrap_pool_with_timeout(inner)
    assert isinstance(out, TimeoutPool)
    out.acquire()
    assert inner.acquire_calls[0]["kwargs"] == {"timeout": DEFAULT_ACQUIRE_TIMEOUT}


def test_wrap_pool_with_explicit_default():
    inner = _FakePool()
    out = wrap_pool_with_timeout(inner, default_timeout=12)
    out.acquire()
    assert inner.acquire_calls[0]["kwargs"] == {"timeout": 12}


def test_proxy_attribute_access_for_internals():
    """Internal-ish attributes that asyncpg exposes (e.g.
    ``_holders``, ``get_size``) pass through. We can't enumerate the
    real list without depending on asyncpg internals, but we can
    confirm a configurable attribute is delegated."""
    inner = MagicMock()
    inner.get_size = MagicMock(return_value=10)
    proxy = TimeoutPool(inner)
    assert proxy.get_size() == 10
    inner.get_size.assert_called_once()


# ── Distillation worker also routes through the wrap ───────────────────────


def test_distillation_worker_creates_wrapped_pool(monkeypatch):
    """Codex round-1 of the round-28 thread caught that the
    distillation worker bypassed the lifecycle wrap by calling
    ``asyncpg.create_pool`` directly. ``_create_pool`` now wraps
    the raw pool through ``wrap_pool_with_timeout`` so worker
    ``self.db_pool.acquire()`` sites also inherit the default
    timeout. Pin the contract directly."""
    import asyncio

    from mnemos.workers import distillation

    raw_sentinel = MagicMock(name="raw-asyncpg-pool")

    async def _fake_create_pool(**kwargs):
        return raw_sentinel

    monkeypatch.setattr(
        distillation.asyncpg, "create_pool", _fake_create_pool,
    )

    pool = asyncio.run(distillation.MemoryDistillationWorker._create_pool())
    assert isinstance(pool, TimeoutPool)
    # The wrapped instance is the raw pool we created.
    assert pool._pool is raw_sentinel

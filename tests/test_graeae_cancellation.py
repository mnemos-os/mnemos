from __future__ import annotations

import asyncio

import pytest

from mnemos.domain.graeae.engine import GraeaeEngine


pytestmark = pytest.mark.asyncio


class _AlwaysAllowed:
    async def is_allowed(self, _provider):
        return True

    async def record_failure(self, _provider):
        return None

    async def record_success(self, _provider):
        return None


class _Limiter:
    def __init__(self):
        self.in_flight = 0

    async def acquire(self, _provider):
        self.in_flight += 1
        return True

    async def release(self, _provider):
        self.in_flight -= 1


class _Cache:
    def get(self, *_args):
        return None

    def set(self, *_args):
        return None


async def test_consult_cancellation_releases_provider_slots(monkeypatch):
    engine = GraeaeEngine()
    engine.providers = {"p1": {"model": "m1", "weight": 1.0}, "p2": {"model": "m2", "weight": 1.0}}
    engine._circuit_breakers = _AlwaysAllowed()
    engine._rate_limiters = _AlwaysAllowed()
    engine._cache = _Cache()
    limiter = _Limiter()
    engine._concurrency = limiter

    started = asyncio.Event()

    async def query_provider(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "_query_provider", query_provider)

    task = asyncio.create_task(engine.consult("prompt", "reasoning"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.in_flight == 0

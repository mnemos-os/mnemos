from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_lifespan_shutdown_without_managed_inference_resource(monkeypatch, tmp_path):
    async def run():
        import mnemos.domain.graeae.engine as graeae_engine
        from mnemos.core import config as core_config
        from mnemos.core import lifecycle

        class FalsyPool:
            def __bool__(self):
                return False

        async def create_pool(**_kwargs):
            return FalsyPool()

        class RedisUnavailable:
            async def ping(self):
                raise RuntimeError("redis unavailable")

        class FakeGraeaeEngine:
            async def reload_from_registry(self, _pool):
                return None

        monkeypatch.setattr(lifecycle, "_background_tasks", set())
        monkeypatch.setattr(lifecycle, "_worker_tasks", set())
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        monkeypatch.setenv("MNEMOS_SQLITE_PATH", str(tmp_path / "mnemos.sqlite3"))
        core_config.reload_settings()
        monkeypatch.setattr(lifecycle, "_load_config", lambda: {"worker": {"enabled": False}})
        monkeypatch.setattr(lifecycle.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(lifecycle.aioredis, "from_url", lambda *_args, **_kwargs: RedisUnavailable())
        monkeypatch.setattr(graeae_engine, "get_graeae_engine", lambda: FakeGraeaeEngine())

        app = SimpleNamespace(state=SimpleNamespace())

        async with lifecycle.lifespan(app):
            assert app.state.pool is lifecycle._pool
            assert app.state.cache is None
            assert lifecycle._worker_status["distillation_worker"] == "disabled"
            assert lifecycle._worker_tasks == set()

        assert lifecycle._cache is None

    asyncio.run(run())

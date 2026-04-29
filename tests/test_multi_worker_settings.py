"""Multi-worker settings and startup warning coverage."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from mnemos.core import config
from mnemos.core import lifecycle


_ENV_KEYS = (
    "MNEMOS_CONFIG_PATH",
    "MNEMOS_WORKERS",
    "RATE_LIMIT_STORAGE_URI",
    "RATE_LIMIT_STORAGE",
)


@contextmanager
def _isolated_settings(
    monkeypatch: pytest.MonkeyPatch,
    config_path: str,
    env: dict[str, str] | None = None,
) -> Iterator[None]:
    with monkeypatch.context() as scoped:
        for key in _ENV_KEYS:
            scoped.delenv(key, raising=False)
        scoped.setenv("MNEMOS_CONFIG_PATH", config_path)
        for key, value in (env or {}).items():
            scoped.setenv(key, value)
        config._reset_settings_for_tests()
        yield
    config._reset_settings_for_tests()


def test_workers_default_to_one(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml")):
        assert config.get_settings().server.workers == 1


def test_workers_override_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml"), {"MNEMOS_WORKERS": "4"}):
        assert config.get_settings().server.workers == 4


def test_multi_worker_memory_storage_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {
        "MNEMOS_WORKERS": "2",
        "RATE_LIMIT_STORAGE_URI": "memory://",
    }
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml"), env):
        settings = config.get_settings()
        caplog.set_level(logging.WARNING, logger="mnemos.core.lifecycle")

        lifecycle._warn_if_multi_worker_without_redis(settings)

    assert "multi-worker without Redis will produce drift" in caplog.text
    assert "rate limit and circuit breaker state" in caplog.text


def test_multi_worker_redis_storage_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {
        "MNEMOS_WORKERS": "2",
        "RATE_LIMIT_STORAGE_URI": "redis://redis:6379/1",
    }
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml"), env):
        settings = config.get_settings()
        caplog.set_level(logging.WARNING, logger="mnemos.core.lifecycle")

        lifecycle._warn_if_multi_worker_without_redis(settings)

    assert "multi-worker without Redis will produce drift" not in caplog.text

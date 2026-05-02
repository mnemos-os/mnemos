"""Settings singleton and compatibility export tests."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from mnemos.core import config


_ENV_KEYS = (
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
    "PG_POOL_MIN",
    "PG_POOL_MAX",
    "MNEMOS_CONFIG_PATH",
    "MNEMOS_GRAEAE_NATS_FANOUT",
    "MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING",
    "MNEMOS_NATS_AUDIT_CONSUMER_ENABLED",
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


def test_get_settings_returns_singleton(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml")):
        assert config.get_settings() is config.get_settings()


def test_default_values_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml")):
        settings = config.get_settings()
        assert settings.database.host == "localhost"
        assert settings.database.port == 5432
        assert settings.database.database == "mnemos"
        assert settings.database.user == "mnemos_user"
        assert settings.database.password == ""
        assert settings.server.port == 5002
        assert settings.graeae.nats_fanout is False
        assert settings.nats.publish_pantheon_routing is False
        assert settings.nats.audit_consumer_enabled is False


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml"), {"PG_HOST": "foo"}):
        assert config.get_settings().database.host == "foo"


def test_graeae_nats_fanout_feature_flag_defaults_off_and_can_enable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        str(tmp_path / "missing.toml"),
        {"MNEMOS_GRAEAE_NATS_FANOUT": "true"},
    ):
        settings = config.get_settings()
        assert settings.graeae.nats_fanout is True
        assert config.GRAEAE_CONFIG["nats_fanout"] is True


def test_nats_substrate_flags_default_off_and_can_enable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        str(tmp_path / "missing.toml"),
        {
            "MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING": "1",
            "MNEMOS_NATS_AUDIT_CONSUMER_ENABLED": "true",
        },
    ):
        settings = config.get_settings()
        assert settings.nats.publish_pantheon_routing is True
        assert settings.nats.audit_consumer_enabled is True


def test_config_toml_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
host = "toml-host"
port = 6543
""".lstrip(),
        encoding="utf-8",
    )

    with _isolated_settings(monkeypatch, str(config_file), {"PG_HOST": "env-host"}):
        settings = config.get_settings()
        assert settings.database.host == "toml-host"
        assert settings.database.port == 6543


def test_pg_config_backwards_compat_dict(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, str(tmp_path / "missing.toml")):
        assert config.PG_CONFIG["host"] == "localhost"
        assert config.PG_CONFIG["database"] == "mnemos"
        assert config.PG_CONFIG["pool_min_size"] == 5

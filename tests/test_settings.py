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


def test_empty_toml_password_does_not_shadow_pg_password_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The documented production shape is `[database].password = ""` with the
    secret supplied via PG_PASSWORD. Empty-string TOML must NOT win over the
    env var or the service starts with no DB password after --upgrade.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = "postgres"
host = "192.168.207.67"
database = "mnemos_prod"
user = "mnemos_user"
password = ""
""".lstrip(),
        encoding="utf-8",
    )
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {"PG_PASSWORD": "from-env"},
    ):
        settings = config.get_settings()
        assert settings.database.password == "from-env"


def test_empty_toml_database_field_does_not_shadow_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Same logic for the full set of empty-string-in-TOML fields. Operator
    leaves the field blank in config.toml expecting env to fill it."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = "postgres"
host = ""
database = ""
user = ""
password = ""
""".lstrip(),
        encoding="utf-8",
    )
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {
            "PG_HOST": "from-env-host",
            "PG_DATABASE": "from-env-db",
            "PG_USER": "from-env-user",
            "PG_PASSWORD": "from-env-pw",
        },
    ):
        settings = config.get_settings()
        assert settings.database.host == "from-env-host"
        assert settings.database.database == "from-env-db"
        assert settings.database.user == "from-env-user"
        assert settings.database.password == "from-env-pw"


def test_empty_toml_backend_does_not_shadow_pg_backend_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Round-16 codex finding: `backend = ""` in config blocks PG_BACKEND
    and lifecycle would refuse to start with 'Unsupported persistence
    backend'. Empty-string strip must cover backend too."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = ""
host = "localhost"
""".lstrip(),
        encoding="utf-8",
    )
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {"PG_BACKEND": "postgres"},
    ):
        settings = config.get_settings()
        assert settings.database.backend == "postgres"


def test_empty_toml_port_does_not_shadow_pg_port_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Same check for port — operators sometimes leave it blank for env override."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = "postgres"
port = ""
""".lstrip(),
        encoding="utf-8",
    )
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {"PG_PORT": "5433"},
    ):
        settings = config.get_settings()
        assert settings.database.port == 5433


def test_empty_toml_sqlite_path_does_not_shadow_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """sqlite_path is env-aliased via MNEMOS_SQLITE_PATH/PG_SQLITE_PATH. An
    empty TOML value would otherwise resolve to the cwd."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = "sqlite"
sqlite_path = ""
""".lstrip(),
        encoding="utf-8",
    )
    expected_path = tmp_path / "subdir" / "mnemos.db"
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {"MNEMOS_SQLITE_PATH": str(expected_path)},
    ):
        settings = config.get_settings()
        assert str(settings.database.sqlite_path) == str(expected_path)


def test_non_empty_toml_password_still_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Sanity: a non-empty config.toml password must still override env.
    Operators who explicitly write the password into config (not the
    documented shape, but supported) shouldn't be surprised by env."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
backend = "postgres"
host = "toml-host"
password = "from-toml"
""".lstrip(),
        encoding="utf-8",
    )
    with _isolated_settings(
        monkeypatch,
        str(config_file),
        {"PG_PASSWORD": "from-env"},
    ):
        settings = config.get_settings()
        assert settings.database.password == "from-toml"

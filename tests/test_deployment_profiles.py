from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mnemos.cli import main as cli_main
from mnemos.core import config
from mnemos.core import lifecycle
from mnemos.persistence import PostgresBackend, SqliteBackend


runner = CliRunner()

_ENV_KEYS = (
    "MNEMOS_CONFIG_PATH",
    "MNEMOS_PROFILE",
    "MNEMOS_PROFILE_OVERRIDE",
    "MNEMOS_PERSISTENCE_BACKEND",
    "PERSISTENCE_BACKEND",
    "PG_BACKEND",
    "MNEMOS_DATABASE_DSN",
    "DATABASE_DSN",
    "PG_DSN",
    "MNEMOS_DATABASE_URL",
    "DATABASE_URL",
    "PG_URL",
    "MNEMOS_SQLITE_PATH",
    "SQLITE_DB_PATH",
    "PG_SQLITE_PATH",
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
    "RATE_LIMIT_STORAGE_URI",
    "RATE_LIMIT_STORAGE",
    "MNEMOS_WORKERS",
    "GRAEAE_MODE_DEFAULT",
    "MNEMOS_LOG_LEVEL",
    "MNEMOS_COMPRESSION_WORKERS",
    "MNEMOS_LOOSE_TIMEOUTS",
)


@contextmanager
def _isolated_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    env: dict[str, str] | None = None,
    config_text: str | None = None,
) -> Iterator[config.Settings]:
    with monkeypatch.context() as scoped:
        for key in _ENV_KEYS:
            scoped.delenv(key, raising=False)
        if config_text is None:
            config_path = tmp_path / "missing.toml"
        else:
            config_path = tmp_path / "config.toml"
            config_path.write_text(config_text, encoding="utf-8")
        scoped.setenv("MNEMOS_CONFIG_PATH", str(config_path))
        for key, value in (env or {}).items():
            scoped.setenv(key, value)
        settings = config.reload_settings()
        try:
            yield settings
        finally:
            config.reload_settings()


def test_profile_server_sets_server_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "server"}) as settings:
        assert settings.profile == "server"
        assert settings.database.backend == "postgres"
        assert settings.rate_limit.storage_uri == "redis://localhost:6379/1"
        assert settings.graeae.mode_default == "auto"
        assert settings.logging.level == "INFO"
        assert settings.compression.workers == 4


def test_profile_server_selects_and_instantiates_postgres(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "server"}) as settings:
        assert lifecycle._select_persistence_backend(settings) == "postgres"
        backend = lifecycle._build_postgres_backend(SimpleNamespace(), settings)
        assert isinstance(backend, PostgresBackend)


@pytest.mark.asyncio
async def test_profile_edge_selects_sqlite_and_loads_sqlite_vec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def fake_load_sqlite_vec(self, _conn) -> None:
        self._vec_loaded = True

    async def fake_create_vec_virtual_table(self, _conn) -> None:
        self._vec_loaded = True

    monkeypatch.setattr(SqliteBackend, "_load_sqlite_vec", fake_load_sqlite_vec)
    monkeypatch.setattr(SqliteBackend, "_create_vec_virtual_table", fake_create_vec_virtual_table)
    sqlite_path = tmp_path / "edge.db"
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_PROFILE": "edge", "MNEMOS_SQLITE_PATH": str(sqlite_path)},
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "sqlite"
        backend = await lifecycle._build_sqlite_backend(settings.database.sqlite_path, settings)
        try:
            assert isinstance(backend, SqliteBackend)
            assert backend.vec_loaded is True
            assert backend.uses_sqlite_vec is True
        finally:
            await backend.close()


def test_profile_dev_selects_sqlite_and_debug_logging(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "dev"}) as settings:
        assert settings.profile == "dev"
        assert settings.database.backend == "sqlite"
        assert settings.logging.level == "DEBUG"
        assert settings.runtime.loose_timeouts is True
        assert lifecycle._select_persistence_backend(settings) == "sqlite"


def test_profile_personal_aliases_to_edge(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "personal"}) as settings:
        assert settings.profile == "edge"
        assert settings.database.backend == "sqlite"
        assert settings.graeae.mode_default == "single"


def test_profile_unknown_fails_with_clear_message(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with monkeypatch.context() as scoped:
        for key in _ENV_KEYS:
            scoped.delenv(key, raising=False)
        scoped.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        scoped.setenv("MNEMOS_PROFILE", "unknown")
        with pytest.raises(ValueError, match="Unsupported MNEMOS profile 'unknown'"):
            config.reload_settings()
    config.reload_settings()


def test_explicit_pg_host_overrides_edge_profile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_PROFILE": "edge", "PG_HOST": "postgres.internal"},
    ) as settings:
        assert settings.database.backend == "sqlite"
        assert lifecycle._select_persistence_backend(settings) == "postgres"


def test_explicit_sqlite_dsn_overrides_server_profile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_PROFILE": "server", "MNEMOS_DATABASE_DSN": "sqlite:///tmp/hybrid.db"},
    ) as settings:
        assert settings.database.backend == "postgres"
        assert lifecycle._select_persistence_backend(settings) == "sqlite"


def test_toml_backend_override_wins_over_profile_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        config_text="""
[server]
profile = "server"

[database]
backend = "sqlite"
sqlite_path = "/tmp/profile-override.db"
""".lstrip(),
    ) as settings:
        assert settings.profile == "server"
        assert settings.database.backend == "sqlite"
        assert lifecycle._select_persistence_backend(settings) == "sqlite"


def test_cli_serve_profile_flag_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: dict[str, object] = {}

    def fake_uvicorn_run(app_path: str, *, host: str, port: int, workers: int) -> None:
        calls.update(
            {
                "app_path": app_path,
                "host": host,
                "port": port,
                "workers": workers,
                "profile": config.get_settings().profile,
            }
        )

    with monkeypatch.context() as scoped:
        for key in _ENV_KEYS:
            scoped.delenv(key, raising=False)
        scoped.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        scoped.setenv("MNEMOS_PROFILE", "server")
        config.reload_settings()
        import uvicorn

        scoped.setattr(uvicorn, "run", fake_uvicorn_run)
        result = runner.invoke(cli_main.app, ["serve", "--profile", "dev"])

    assert result.exit_code == 0, result.output
    assert calls["app_path"] == "mnemos.api.main:app"
    assert calls["profile"] == "dev"
    assert calls["workers"] == 1
    config.reload_settings()


def test_cli_install_profile_flag_forwards_to_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_run_module_main(module_name: str, argv: list[str], *, prog: str | None = None) -> None:
        calls.append((module_name, argv, prog))

    monkeypatch.setattr(cli_main, "_run_module_main", fake_run_module_main)
    result = runner.invoke(cli_main.app, ["install", "--profile", "edge", "--check"])

    assert result.exit_code == 0, result.output
    assert calls == [("mnemos.installer.__main__", ["--check", "--profile", "edge"], "mnemos install")]


@pytest.mark.asyncio
async def test_health_returns_active_profile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from mnemos.api.routes import health

    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "edge"}):
        monkeypatch.setattr(health._lc, "_pool", None)
        monkeypatch.setattr(health._lc, "_persistence_backend", object())
        monkeypatch.setattr(health._lc, "_worker_status", {"distillation_worker": "idle"})
        response = await health.health_check()

    assert response.profile == "edge"
    assert response.database_connected is True

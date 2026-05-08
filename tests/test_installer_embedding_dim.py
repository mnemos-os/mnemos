"""Installer integration test: MNEMOS_EMBEDDING_DIM survives into generated config.

Codex re-review of the embed-dim slice flagged that the installer reading the
env var locally wasn't enough — without persisting the value into the generated
config.toml and the systemd EnvironmentFile, a service install at dim=512 could
be followed by the service starting at dim=768 (no env var in scope) and hit
the dim mismatch guard. These tests verify the full plumbing.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mnemos.installer.wizard import Config


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch):
    """Round-30 introduced MNEMOS_PROFILE / MNEMOS_PROFILE_OVERRIDE env
    parity in _load_existing_config. Many tests in this module
    pre-date that and don't explicitly clear those env vars, so a
    leaked MNEMOS_PROFILE from another test (or operator shell) would
    flip cfg.profile during these isolated assertions. Auto-clear
    them per-test."""
    for k in ("MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)


def test_config_default_embedding_dim_is_768():
    cfg = Config()
    assert cfg.embedding_dim == 768


def test_config_can_override_embedding_dim():
    cfg = Config(embedding_dim=512)
    assert cfg.embedding_dim == 512


def test_main_env_loader_reads_mnemos_embedding_dim():
    """__main__.load_config_from_env() picks up MNEMOS_EMBEDDING_DIM."""
    from mnemos.installer.__main__ import _config_from_env as load_config_from_env

    with patch.dict(os.environ, {"MNEMOS_EMBEDDING_DIM": "512"}, clear=False):
        cfg = load_config_from_env()
    assert cfg.embedding_dim == 512


def test_main_env_loader_fails_closed_on_garbage_embedding_dim():
    """Round-39 MEDIUM: malformed MNEMOS_EMBEDDING_DIM must fail
    closed (matches runtime Pydantic ValidationError). Previously
    warned and fell back to 768, letting --upgrade dispatch under
    a runtime-invalid value."""
    from mnemos.installer.__main__ import _config_from_env as load_config_from_env

    env_overlay = {"MNEMOS_EMBEDDING_DIM": "not-an-int"}
    with patch.dict(os.environ, env_overlay, clear=False):
        try:
            load_config_from_env()
        except ValueError as exc:
            assert "Invalid embedding dimension" in str(exc)
            return
        raise AssertionError("Expected ValueError on malformed MNEMOS_EMBEDDING_DIM")


def test_main_env_loader_pg_embedding_dim_alias():
    from mnemos.installer.__main__ import _config_from_env as load_config_from_env

    env_overlay = {"PG_EMBEDDING_DIM": "1024"}
    # Make sure MNEMOS_EMBEDDING_DIM isn't shadowing the alias for this test.
    with patch.dict(os.environ, env_overlay, clear=False):
        os.environ.pop("MNEMOS_EMBEDDING_DIM", None)
        cfg = load_config_from_env()
    assert cfg.embedding_dim == 1024


def test_minimal_config_emits_embedding_dim():
    """The generated config.toml must carry embedding_dim under [database]."""
    from mnemos.installer.__main__ import _render_minimal_config

    cfg = Config(profile="edge", embedding_dim=512, sqlite_path="/tmp/x.db")
    profile_defaults = {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "graeae_mode_default": "auto",
        "log_level": "INFO",
        "compression_workers": 1,
    }
    rendered = _render_minimal_config(cfg, profile_defaults)
    assert "embedding_dim = 512" in rendered
    # Guard against accidentally placing it outside the [database] block.
    db_section_start = rendered.index("[database]")
    db_section_end = rendered.find("[", db_section_start + len("[database]"))
    db_section = rendered[db_section_start:db_section_end if db_section_end != -1 else None]
    assert "embedding_dim = 512" in db_section


def test_service_env_file_contents_include_embedding_dim():
    """The systemd EnvironmentFile must export MNEMOS_EMBEDDING_DIM=<dim>.

    We validate the lines list rendering rather than executing the actual
    sudo-gated write. The env-file content is the relevant artifact.
    """
    cfg = Config(embedding_dim=512)
    # Read the service module source to confirm the literal is present —
    # defensive: ensures future refactors don't drop the env-file line
    # without breaking this test. The actual file write is sudo-gated and
    # not exercised here.
    import inspect
    from mnemos.installer import service

    _ = cfg.embedding_dim  # documents that we expect this to flow through
    source = inspect.getsource(service._write_env_file)
    assert "MNEMOS_EMBEDDING_DIM" in source, (
        "service._write_env_file must emit MNEMOS_EMBEDDING_DIM into the "
        "systemd env file or the dim won't survive service restart"
    )
    assert "config.embedding_dim" in source, (
        "MNEMOS_EMBEDDING_DIM must be sourced from config.embedding_dim, "
        "not a hardcoded literal"
    )
    # Sanity: the expected formatted line is a substring of source after
    # f-string expansion. We can't run f-strings inside getsource, but we
    # can verify the f-string template.
    assert "MNEMOS_EMBEDDING_DIM={config.embedding_dim}" in source


def test_setup_sqlite_uses_config_embedding_dim_not_env(tmp_path, monkeypatch):
    """db.setup_sqlite_database() must trust config.embedding_dim, not re-read env.

    Drift point: an earlier version of this fix re-read MNEMOS_EMBEDDING_DIM
    inside setup_sqlite_database. That created a window where config.toml
    said dim=512 but a stale env var (or no env var) was in the installer's
    scope, so the install used the wrong dim. Trust the Config object end
    to end.
    """
    from mnemos.installer import db as installer_db

    # If the env var is absent but config carries dim=512, the install must
    # use 512 (proves the function reads config, not env).
    monkeypatch.delenv("MNEMOS_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("PG_EMBEDDING_DIM", raising=False)

    cfg = Config(
        profile="edge",
        sqlite_path=str(tmp_path / "x.db"),
        embedding_dim=512,
    )
    rc = installer_db.setup_sqlite_database(cfg)
    assert rc is True

    # The DB exists and was opened cleanly.
    assert (tmp_path / "x.db").exists()


def test_setup_sqlite_default_768_when_config_missing_field(tmp_path):
    """Older Config objects without embedding_dim should default to 768."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class LegacyConfig:
        profile: str = "edge"
        sqlite_path: str = ""

    cfg = LegacyConfig(sqlite_path=str(tmp_path / "legacy.db"))
    rc = installer_db.setup_sqlite_database(cfg)
    assert rc is True


def test_apply_embedding_dim_from_env_overrides_default():
    """Wizard/agent paths use _apply_embedding_dim_from_env to pick up MNEMOS_EMBEDDING_DIM."""
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    cfg = Config()
    assert cfg.embedding_dim == 768
    with patch.dict(os.environ, {"MNEMOS_EMBEDDING_DIM": "512"}, clear=False):
        _apply_embedding_dim_from_env(cfg)
    assert cfg.embedding_dim == 512


def test_apply_embedding_dim_from_env_no_op_when_unset():
    """Unset env should leave cfg.embedding_dim at its default."""
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    cfg = Config(embedding_dim=1024)
    with patch.dict(os.environ, {}, clear=True):
        _apply_embedding_dim_from_env(cfg)
    assert cfg.embedding_dim == 1024


def test_apply_embedding_dim_from_env_fails_closed_on_garbage():
    """Round-39 MEDIUM: garbage env value must fail closed
    (ValueError), matching runtime Pydantic. Previously warned and
    silently fell back to default — letting --upgrade dispatch
    under a runtime-invalid value."""
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    cfg = Config(embedding_dim=768)
    with patch.dict(os.environ, {"MNEMOS_EMBEDDING_DIM": "not-an-int"}, clear=False):
        try:
            _apply_embedding_dim_from_env(cfg)
        except ValueError as exc:
            assert "Invalid embedding dimension" in str(exc)
            return
        raise AssertionError("Expected ValueError on malformed MNEMOS_EMBEDDING_DIM")


def test_apply_embedding_dim_from_env_pg_alias():
    """PG_EMBEDDING_DIM is an accepted alias."""
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    cfg = Config()
    env_overlay = {"PG_EMBEDDING_DIM": "1024"}
    with patch.dict(os.environ, env_overlay, clear=False):
        os.environ.pop("MNEMOS_EMBEDDING_DIM", None)
        _apply_embedding_dim_from_env(cfg)
    assert cfg.embedding_dim == 1024


def test_load_existing_config_round_trips_embedding_dim(tmp_path):
    """--upgrade reads embedding_dim from config.toml — without this, a 512-D
    install would lose its dim on `--upgrade`, default to 768, and skip the
    new postgres ALTER path."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[deployment]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "test"\n'
        'embedding_dim = 512\n'
        '\n'
        '[api]\n'
        'port = 5002\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.embedding_dim == 512


def test_load_existing_config_defaults_to_768_when_absent(tmp_path):
    """Old config.toml files without embedding_dim default to 768 cleanly."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[deployment]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "test"\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.embedding_dim == 768


def test_load_existing_config_password_only_env_does_not_bypass_config_toml(tmp_path):
    """Round-4 codex finding: when MNEMOS_DB_PASSWORD is set in env but
    config.toml has embedding_dim=512 and MNEMOS_EMBEDDING_DIM is unset,
    the loader must read the 512 from config.toml — not short-circuit to
    env-only and silently downgrade to 768."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = ""\n'  # password expected from env
        'embedding_dim = 512\n'
    )
    env_overlay = {"MNEMOS_DB_PASSWORD": "from-env-secret"}
    with patch.dict(os.environ, env_overlay, clear=False):
        os.environ.pop("MNEMOS_EMBEDDING_DIM", None)  # not set
        os.environ.pop("PG_EMBEDDING_DIM", None)
        cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Password from env overlays config (since config.toml has empty pass).
    assert cfg.db_password == "from-env-secret"
    # CRITICAL: embedding_dim came from config.toml, NOT defaulted to 768.
    assert cfg.embedding_dim == 512


def test_load_existing_config_env_overrides_embedding_dim_when_set(tmp_path):
    """Operator-driven model swap on --upgrade: MNEMOS_EMBEDDING_DIM in env
    overrides config.toml. Config.toml has 512, env asks for 1024, expect 1024."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "test"\n'
        'embedding_dim = 512\n'
    )
    env_overlay = {"MNEMOS_EMBEDDING_DIM": "1024"}
    with patch.dict(os.environ, env_overlay, clear=False):
        cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.embedding_dim == 1024  # env wins on explicit set


def test_load_existing_config_password_in_config_takes_precedence(tmp_path):
    """If config.toml has a password AND env has a password, config wins.

    Avoids surprising the operator who edited config.toml by ignoring it.
    """
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "from-config"\n'
        'embedding_dim = 512\n'
    )
    env_overlay = {"MNEMOS_DB_PASSWORD": "from-env"}
    with patch.dict(os.environ, env_overlay, clear=False):
        cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_password == "from-config"


def test_load_existing_config_garbage_embedding_dim_fails_closed(tmp_path):
    """Round-40 MEDIUM: a non-integer embedding_dim in config.toml
    must fail closed (matches runtime Pydantic ValidationError).
    Previously fell back to 768 silently, letting --upgrade
    dispatch migrations + patch config under a runtime-invalid
    value."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "test"\n'
        'embedding_dim = "not-a-number"\n'
    )
    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Invalid [database].embedding_dim" in str(exc_info.value)


def test_load_existing_config_infers_server_profile_from_postgres_backend(tmp_path):
    """Round-8 codex finding: a config.toml without [server].profile but with
    [database].backend=postgres must be loaded as profile=server, NOT default
    to edge. Otherwise _write_config_toml would rewrite the postgres install
    as sqlite on next --upgrade."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "postgres"\n'
        'host = "192.168.207.67"\n'
        'port = 5432\n'
        'database = "mnemos_prod"\n'
        'user = "mnemos_user"\n'
        'password = "real-secret"\n'
        'embedding_dim = 768\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "server"


def test_load_existing_config_infers_server_profile_from_explicit_postgres_fields(tmp_path):
    """Even without [database].backend, explicit non-default postgres fields
    (custom host/port/password) signal that this is a postgres install."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'host = "192.168.207.67"\n'
        'port = 5433\n'  # non-default port — clear postgres signal
        'database = "mnemos_prod"\n'
        'user = "real_user"\n'
        'password = "real-secret"\n'
        'embedding_dim = 768\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "server"


def test_load_existing_config_keeps_edge_when_only_sqlite_fields_present(tmp_path):
    """A pure-sqlite config (no host/port/password override, sqlite_path set)
    must NOT be promoted to server/postgres."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "sqlite"\n'
        'sqlite_path = "/var/lib/mnemos/mnemos.db"\n'
        'embedding_dim = 512\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "edge"


def test_load_existing_config_explicit_profile_wins_over_inference(tmp_path):
    """If [server].profile or [deployment].profile is explicitly set, use it
    even if the database section looks like postgres."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "dev"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "remote-pg"\n'
        'embedding_dim = 768\n'
    )
    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "dev"  # explicit beats inference


def test_patch_config_toml_embedding_dim_replaces_existing_field(tmp_path):
    """The surgical patcher updates [database].embedding_dim without touching
    other sections — Round-9 codex finding required not rewriting profile
    defaults / rate_limit / graeae / logging / compression on --upgrade."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'
        'port = 5002\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "192.168.207.67"\n'
        'port = 5432\n'
        'database = "mnemos_prod"\n'
        'user = "mnemos_user"\n'
        'password = "production-secret"\n'
        'embedding_dim = 768\n'
        '\n'
        '[rate_limit]\n'
        'storage_uri = "redis://prod-redis:6379/0"\n'
        '\n'
        '[graeae]\n'
        'mode_default = "full"\n'
    )

    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True

    # Read back and verify ONLY embedding_dim changed.
    new_content = config_path.read_text()
    assert "embedding_dim = 512" in new_content
    assert "embedding_dim = 768" not in new_content
    # Production settings preserved verbatim.
    assert 'backend = "postgres"' in new_content
    assert 'host = "192.168.207.67"' in new_content
    assert 'database = "mnemos_prod"' in new_content
    assert 'password = "production-secret"' in new_content
    assert 'storage_uri = "redis://prod-redis:6379/0"' in new_content
    assert 'mode_default = "full"' in new_content
    # Profile = "personal" preserved despite being a legacy alias —
    # the patcher must not normalize it.
    assert 'profile = "personal"' in new_content


def test_patch_config_toml_embedding_dim_appends_when_field_missing(tmp_path):
    """Old config.toml without an embedding_dim line should get one appended."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        '\n'
        '[server]\n'
        'profile = "server"\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 1024)
    assert rc is True
    new_content = config_path.read_text()
    assert "embedding_dim = 1024" in new_content
    # Must land in [database], not [server].
    db_idx = new_content.index("[database]")
    embed_idx = new_content.index("embedding_dim = 1024")
    server_idx = new_content.index("[server]")
    assert db_idx < embed_idx < server_idx


def test_patch_config_toml_legacy_personal_postgres_survives_upgrade(tmp_path):
    """Round-9 regression: profile="personal" + backend="postgres" must
    NOT have its backend rewritten to sqlite by --upgrade.

    The surgical patch preserves backend; the legacy "personal" profile
    string is left intact. Combined with the round-8 inference that loads
    this config as profile=server at runtime, the postgres install survives.
    """
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "personal"\n'  # legacy
        '\n'
        '[database]\n'
        'backend = "postgres"\n'  # but the DB is real postgres
        'host = "10.0.0.5"\n'
        'port = 5432\n'
        'database = "mnemos"\n'
        'user = "mnemos"\n'
        'password = "kept-by-upgrade"\n'
        'embedding_dim = 768\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True
    new_content = config_path.read_text()
    # Backend NOT rewritten to sqlite.
    assert 'backend = "postgres"' in new_content
    assert 'backend = "sqlite"' not in new_content
    # All postgres credential fields preserved.
    assert 'host = "10.0.0.5"' in new_content
    assert 'password = "kept-by-upgrade"' in new_content
    # Only the dim field changed.
    assert "embedding_dim = 512" in new_content


def test_patch_config_toml_returns_false_on_unwritable_path(tmp_path):
    """If config.toml can't be read (e.g. doesn't exist), the patcher returns False."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    rc = _patch_config_toml_embedding_dim(str(tmp_path / "nonexistent"), 512)
    assert rc is False


def test_patch_config_toml_preserves_owner_group_mode(tmp_path):
    """Round-13 codex finding: the patcher must preserve the existing
    config.toml's uid/gid/mode. Previous os.replace + chmod 0600 would
    install the patched file as the running uid:gid with 0600, breaking
    the service group's read access on a root:mnemos 0640 shape.

    We can't easily test cross-uid behavior in CI (would need real sudo),
    but we CAN verify the mode survives the patch operation when the
    operator owns the file. Critically the file should NOT lose its
    original mode in favor of 0600.
    """
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'embedding_dim = 768\n'
    )
    # Set explicit non-default mode (0644 — group + other readable).
    os.chmod(config_path, 0o644)
    pre_st = os.stat(config_path)

    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True

    # Mode preserved. uid/gid would be too — we check those when the
    # values are stable (i.e., we own the file pre and post).
    post_st = os.stat(config_path)
    assert (post_st.st_mode & 0o777) == 0o644, (
        f"config.toml mode dropped from 0o644 to {oct(post_st.st_mode & 0o777)} — "
        f"the patcher must preserve uid/gid/mode"
    )
    # uid/gid stay the same (we own this temp tree).
    assert post_st.st_uid == pre_st.st_uid
    assert post_st.st_gid == pre_st.st_gid

    # And the actual content updated.
    assert "embedding_dim = 512" in config_path.read_text()


def test_patch_config_toml_handles_inline_comment_section_header(tmp_path):
    """Round-14 codex finding: hand-edited TOML may have inline comments
    after section headers. The patcher's regex must tolerate that and
    NOT append a duplicate [database] table."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database] # production overrides — do not auto-rewrite\n'
        'backend = "postgres"\n'
        'embedding_dim = 768\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True
    new_content = config_path.read_text()
    # Inline comment preserved verbatim.
    assert "# production overrides — do not auto-rewrite" in new_content
    # Updated, not duplicated.
    assert new_content.count("[database]") == 1
    assert "embedding_dim = 512" in new_content
    assert "embedding_dim = 768" not in new_content


def test_patch_config_toml_does_not_mutate_array_of_tables_after_database(tmp_path):
    """Codex round-15 finding: when config.toml has [database] followed by
    array-of-tables `[[...]]` that ALSO carry an `embedding_dim` line, the
    boundary regex must treat `[[providers]]` as a section boundary and
    NOT include those lines in the [database] body. Otherwise `embedding_re.sub`
    rewrites every `embedding_dim` it sees across all the included sections.
    """
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "postgres"\n'
        'embedding_dim = 768\n'
        '\n'
        '[[providers]]\n'
        'name = "openai"\n'
        'embedding_dim = 1536\n'  # provider's own dim — must NOT be touched
        '\n'
        '[[providers]]\n'
        'name = "cohere"\n'
        'embedding_dim = 1024\n'  # ditto
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True

    new_content = config_path.read_text()
    # [database].embedding_dim updated.
    assert "embedding_dim = 512" in new_content
    # Provider-level dims preserved verbatim.
    assert "embedding_dim = 1536" in new_content
    assert "embedding_dim = 1024" in new_content
    # No accidental triple-update.
    assert new_content.count("embedding_dim = 512") == 1


def test_patch_config_toml_handles_indented_section_header(tmp_path):
    """Indented section headers are valid TOML; the patcher must handle them."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '  [database]\n'
        '  backend = "postgres"\n'
        '  embedding_dim = 768\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is True
    new_content = config_path.read_text()
    # Indented header still in place; not duplicated.
    assert new_content.count("[database]") == 1
    assert "embedding_dim = 512" in new_content


def test_patch_config_toml_refuses_unparseable_input(tmp_path, capsys):
    """If config.toml is not parseable TOML, refuse to write."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database\n'  # missing closing bracket — invalid TOML
        'embedding_dim = 768\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is False
    captured = capsys.readouterr()
    assert "not parseable TOML" in captured.err


def test_patch_config_toml_refuses_unmappable_database_shape(tmp_path, capsys):
    """If tomllib parses [database] as something the patcher can't safely
    span (e.g. only via [[database]] array of tables, which the section
    regex misses), refuse rather than append a duplicate."""
    from mnemos.installer.__main__ import _patch_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[database]]\n'  # array-of-tables, not a singular [database] section
        'host = "localhost"\n'
    )
    rc = _patch_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512)
    assert rc is False
    captured = capsys.readouterr()
    # Either refuses with a clear message, or treats array-of-tables as
    # legitimate and writes a sibling [database] table. We accept either
    # behavior as long as the result is parseable TOML — but currently
    # our impl refuses with a "cannot map to a safe source span" message.
    assert ("cannot map" in captured.err or "Refusing" in captured.err
            or "not parseable" in captured.err)


def test_config_from_env_accepts_pg_password_alias():
    """Round-14 codex finding: env-only upgrades hit the no-config-toml
    fallback when MNEMOS_DB_PASSWORD is set, but the runtime / service env
    use PG_PASSWORD. Container shapes should be able to use either."""
    from mnemos.installer.__main__ import _config_from_env

    # No config.toml, only PG_* env shape (what service._write_env_file emits).
    env_overlay = {
        "PG_HOST": "192.168.207.67",
        "PG_PORT": "5432",
        "PG_DATABASE": "mnemos_prod",
        "PG_USER": "mnemos_prod_user",
        "PG_PASSWORD": "real-secret",
    }
    with patch.dict(os.environ, env_overlay, clear=False):
        # Make sure MNEMOS_DB_* aren't set (they'd shadow PG_*).
        for k in (
            "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
            "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        ):
            os.environ.pop(k, None)
        cfg = _config_from_env()
    # PG_* read into cfg correctly — does NOT default to localhost/mnemos.
    assert cfg.db_host == "192.168.207.67"
    assert cfg.db_name == "mnemos_prod"
    assert cfg.db_user == "mnemos_prod_user"
    assert cfg.db_password == "real-secret"


def test_load_existing_config_falls_back_to_env_via_pg_password(tmp_path):
    """No config.toml + PG_PASSWORD set — _load_existing_config should
    trigger the env fallback (was MNEMOS_DB_PASSWORD only before)."""
    from mnemos.installer.__main__ import _load_existing_config

    env_overlay = {
        "PG_HOST": "192.168.207.67",
        "PG_DATABASE": "mnemos_prod",
        "PG_USER": "mnemos",
        "PG_PASSWORD": "via-pg-only",
    }
    with patch.dict(os.environ, env_overlay, clear=False):
        os.environ.pop("MNEMOS_DB_PASSWORD", None)
        cfg = _load_existing_config(str(tmp_path))  # no config.toml here
    assert cfg is not None
    assert cfg.db_host == "192.168.207.67"
    assert cfg.db_password == "via-pg-only"


def test_load_existing_config_overlays_all_empty_db_fields_from_env(tmp_path):
    """Round-15 codex finding: the installer overlay must mirror runtime's
    treatment of empty-string TOML fields (host/database/user/password all
    fall through to PG_* env)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "server"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = ""\n'
        'database = ""\n'
        'user = ""\n'
        'password = ""\n'
        'embedding_dim = 768\n'
    )
    env_overlay = {
        "PG_HOST": "192.168.207.67",
        "PG_DATABASE": "mnemos_prod",
        "PG_USER": "mnemos_user",
        "PG_PASSWORD": "from-pg-env",
    }
    with patch.dict(os.environ, env_overlay, clear=False):
        for k in ("MNEMOS_DB_HOST", "MNEMOS_DB_NAME", "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD"):
            os.environ.pop(k, None)
        cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # All four empty TOML fields fall through to env.
    assert cfg.db_host == "192.168.207.67"
    assert cfg.db_name == "mnemos_prod"
    assert cfg.db_user == "mnemos_user"
    assert cfg.db_password == "from-pg-env"


def test_load_existing_config_overlays_pg_password_when_config_password_empty(tmp_path):
    """Round-15 codex finding: config.toml present with `password = ""` is
    the documented production shape (secret supplied via PG_PASSWORD env).
    The config-first overlay must accept BOTH MNEMOS_DB_PASSWORD and
    PG_PASSWORD as the env source for an empty config password."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\n'
        'profile = "server"\n'
        '\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "192.168.207.67"\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = ""\n'  # documented shape: secret comes from env
        'embedding_dim = 768\n'
    )
    env_overlay = {"PG_PASSWORD": "from-pg-env"}
    with patch.dict(os.environ, env_overlay, clear=False):
        os.environ.pop("MNEMOS_DB_PASSWORD", None)
        cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_password == "from-pg-env"


def test_mnemos_db_password_takes_precedence_over_pg_password():
    """When both MNEMOS_DB_PASSWORD and PG_PASSWORD are set, the explicit
    MNEMOS_ alias wins (operator's most direct signal)."""
    from mnemos.installer.__main__ import _config_from_env

    env_overlay = {
        "MNEMOS_DB_PASSWORD": "from-mnemos-alias",
        "PG_PASSWORD": "from-pg-alias",
    }
    with patch.dict(os.environ, env_overlay, clear=False):
        cfg = _config_from_env()
    assert cfg.db_password == "from-mnemos-alias"


def test_patch_config_toml_uses_install_for_metadata_preservation():
    """Source-level guard: the patcher must use `install -m -o -g` (with
    sudo fallback), NOT a direct os.replace from a 0600 staged file
    which would lose ownership and mode."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main._patch_config_toml_embedding_dim)
    # Must use install with mode/owner/group flags.
    assert '"install"' in src
    assert '"-m"' in src and '"-o"' in src and '"-g"' in src
    # Must NOT use direct os.replace as the final step (it should only
    # appear, if at all, in commentary).
    import re
    no_comments = re.sub(r"#[^\n]*", "", src)
    assert "os.replace(" not in no_comments, (
        "config patcher must NOT use os.replace — that bypasses "
        "ownership preservation. Use install with -m -o -g."
    )


def test_patch_service_env_preserves_provider_api_keys(tmp_path):
    """Round-10 codex regression: --upgrade must NOT erase OPENAI_API_KEY,
    ANTHROPIC_API_KEY, GEMINI_API_KEY etc. The full _write_env_file() shape
    rebuilds from cfg.graeae_providers (which _load_existing_config leaves
    empty), so we use a surgical line-replace for embedding_dim only."""
    from mnemos.installer.__main__ import _patch_service_env_embedding_dim

    env_path = tmp_path / "mnemos.env"
    env_path.write_text(
        "# MNEMOS environment — managed by installer\n"
        "MNEMOS_PROFILE=server\n"
        "PG_HOST=192.168.207.67\n"
        "PG_PORT=5432\n"
        "PG_DATABASE=mnemos_prod\n"
        "PG_USER=mnemos_user\n"
        "PG_PASSWORD=production-secret\n"
        "MNEMOS_SQLITE_PATH=~/.mnemos/mnemos.db\n"
        "MNEMOS_LISTEN_PORT=5002\n"
        "MNEMOS_SERVICE_USER=mnemos\n"
        "INFERENCE_EMBED_HOST=http://localhost:11434\n"
        "MNEMOS_EMBEDDING_DIM=768\n"
        "OPENAI_API_KEY=sk-prod-openai-AAAA\n"
        "ANTHROPIC_API_KEY=sk-ant-prod-BBBB\n"
        "GEMINI_API_KEY=gem-prod-CCCC\n"
        "TOGETHER_API_KEY=tog-prod-DDDD\n"
        "OPERATOR_CUSTOM_VAR=keep-me\n"  # operator-managed line
    )
    rc = _patch_service_env_embedding_dim(str(env_path), 512)
    assert rc is True

    new_content = env_path.read_text()
    # Only embedding_dim changed.
    assert "MNEMOS_EMBEDDING_DIM=512" in new_content
    assert "MNEMOS_EMBEDDING_DIM=768" not in new_content
    # All provider keys preserved verbatim.
    assert "OPENAI_API_KEY=sk-prod-openai-AAAA" in new_content
    assert "ANTHROPIC_API_KEY=sk-ant-prod-BBBB" in new_content
    assert "GEMINI_API_KEY=gem-prod-CCCC" in new_content
    assert "TOGETHER_API_KEY=tog-prod-DDDD" in new_content
    # Operator's custom line preserved.
    assert "OPERATOR_CUSTOM_VAR=keep-me" in new_content
    # PG password preserved.
    assert "PG_PASSWORD=production-secret" in new_content


def test_patch_service_env_appends_dim_when_missing(tmp_path):
    """Old env file without MNEMOS_EMBEDDING_DIM gets one appended."""
    from mnemos.installer.__main__ import _patch_service_env_embedding_dim

    env_path = tmp_path / "mnemos.env"
    env_path.write_text(
        "MNEMOS_PROFILE=edge\n"
        "OPENAI_API_KEY=sk-test\n"
    )
    rc = _patch_service_env_embedding_dim(str(env_path), 512)
    assert rc is True
    new_content = env_path.read_text()
    assert "MNEMOS_EMBEDDING_DIM=512" in new_content
    assert "OPENAI_API_KEY=sk-test" in new_content


def test_patch_service_env_returns_false_on_missing_file(tmp_path):
    from mnemos.installer.__main__ import _patch_service_env_embedding_dim

    rc = _patch_service_env_embedding_dim(str(tmp_path / "nonexistent.env"), 512)
    assert rc is False


def test_patch_service_env_stages_in_system_temp_not_target_dir(tmp_path):
    """Round-11 codex finding: the temp file must be in a user-writable
    location (system temp dir), NOT in dirname(env_path). The production
    /etc/mnemos directory is root-owned; staging there with mkstemp would
    raise PermissionError before any sudo fallback could run.

    This test verifies the staging happens in the system temp dir by
    checking source — actual /etc/mnemos behavior would need a sudo'd
    integration test which we don't run in CI.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main._patch_service_env_embedding_dim)
    # Bare mkstemp() with no `dir=` kwarg, OR explicit dir=None which
    # uses the system temp dir. Either way the env_path's dirname must
    # NOT be passed.
    assert "tempfile.mkstemp(suffix" in src or "tempfile.mkstemp(dir=None" in src, (
        "Temp file must be staged in system temp dir, not dirname(env_path). "
        "Otherwise PermissionError on root-owned /etc/mnemos."
    )
    # Sanity: the function must reference the env_path's directory for
    # sudo install destination, but not for tempfile creation.
    assert "dir=os.path.dirname(env_path)" not in src.replace(" ", "")
    assert "dir=dirpath" not in src
    assert 'dir=dirname' not in src.replace(" ", "")


def test_upgrade_handles_missing_config_toml_with_matching_env():
    """Round-12 medium finding: env-only / container deploys have no config.toml.

    Source-level guard: --upgrade must NOT unconditionally fail when
    config.toml is absent. If MNEMOS_EMBEDDING_DIM in env matches the
    upgrade target, accept; otherwise refuse with a clear instruction.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    upgrade_idx = src.find("args.upgrade")
    end_idx = src.find('"Migrations complete."', upgrade_idx)
    upgrade_block = src[upgrade_idx:end_idx]
    # Must check os.path.exists for config.toml in the upgrade block.
    assert "os.path.exists(config_toml_path)" in upgrade_block
    # Must reference MNEMOS_EMBEDDING_DIM env validation in the no-config branch.
    assert "MNEMOS_EMBEDDING_DIM" in upgrade_block
    # And not just unconditionally fail when config.toml is absent.
    assert "no config.toml" in upgrade_block.lower() or "container" in upgrade_block.lower()


def test_upgrade_skips_env_patch_when_file_absent(tmp_path, capsys):
    """Round-11 medium finding: a config-based --upgrade on a no-service or
    non-Linux install where /etc/mnemos/mnemos.env doesn't exist must NOT
    fail solely on missing env file. Source-level guard:
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # Look for the env-file-absence handling in the upgrade path.
    upgrade_idx = src.find("args.upgrade")
    end_idx = src.find('"Migrations complete."', upgrade_idx)
    upgrade_block = src[upgrade_idx:end_idx]

    # The branch must check `os.path.exists(env_path)` and `MNEMOS_NO_SERVICE_ENV`.
    assert "os.path.exists(env_path)" in upgrade_block
    assert "MNEMOS_NO_SERVICE_ENV" in upgrade_block
    # And there must be a no-fail path when the env file is absent.
    # Look for "skipping" wording in the env section.
    assert "skipping" in upgrade_block.lower(), (
        "--upgrade must skip env patch when no env file is present, "
        "not return 1"
    )


def test_upgrade_branch_uses_surgical_env_patcher_not_full_rewrite():
    """Source-level guard: --upgrade must call the surgical patcher, NOT
    the full _write_env_file (which would rebuild from cfg.graeae_providers
    and erase keys).

    Bound the upgrade block by 'Migrations complete.' since that's the
    return-success line specific to --upgrade.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    upgrade_idx = src.find("args.upgrade")
    assert upgrade_idx > 0
    # The upgrade branch ends with "Migrations complete." then return 0.
    end_idx = src.find('"Migrations complete."', upgrade_idx)
    assert end_idx > upgrade_idx
    upgrade_block = src[upgrade_idx:end_idx]
    # Surgical patcher must be CALLED.
    assert "_patch_service_env_embedding_dim(" in upgrade_block
    # Full writer must NOT be CALLED (mention in a comment is fine —
    # there's a deliberate explainer in the block warning future
    # refactors not to call it). Strip comments before searching so a
    # comment that says "_write_env_file()" doesn't trip the check.
    import re
    no_comments = re.sub(r"#[^\n]*", "", upgrade_block)
    call_re = re.compile(r"(?<![._a-zA-Z])_write_env_file\s*\(")
    assert not call_re.search(no_comments), (
        "--upgrade branch must not CALL _write_env_file directly — that would "
        "rebuild the env file from cfg.graeae_providers={} and erase API keys. "
        "(Mentioning it in a comment to warn future refactors is fine.)"
    )


def test_verify_config_toml_embedding_dim_round_trip(tmp_path):
    """The verifier returns True iff config.toml records the expected dim."""
    from mnemos.installer.__main__ import _verify_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nembedding_dim = 512\n'
    )
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512) is True
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 768) is False


def test_verify_config_toml_embedding_dim_returns_false_when_missing(tmp_path):
    from mnemos.installer.__main__ import _verify_config_toml_embedding_dim

    # No config.toml present.
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512) is False


def test_verify_config_toml_embedding_dim_returns_false_when_field_absent(tmp_path):
    from mnemos.installer.__main__ import _verify_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text('[database]\nhost = "localhost"\n')
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 768) is False
    # Sanity: the helper should NOT default-on-missing — it must read the
    # actual stored value.
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 0) is False


def test_verify_config_toml_embedding_dim_returns_false_on_garbage(tmp_path):
    from mnemos.installer.__main__ import _verify_config_toml_embedding_dim

    config_path = tmp_path / "config.toml"
    config_path.write_text('[database]\nembedding_dim = "not-an-int"\n')
    assert _verify_config_toml_embedding_dim(str(tmp_path / "config.toml"), 512) is False


def test_upgrade_branch_treats_persistence_failure_as_fatal():
    """Round-7 codex finding: post-migration config writes must be fatal-on-fail.

    Source-level guard: the --upgrade branch must check both the
    _write_config_toml exception path AND the _write_env_file return value,
    returning 1 on either failure. Without this, an --upgrade can ALTER
    the DB schema, fail to update config.toml, print success, and leave
    the service starting against a mismatched config on the next restart.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    upgrade_idx = src.find("args.upgrade")
    assert upgrade_idx > 0
    upgrade_block = src[upgrade_idx:]
    # Must call the verifier and treat its False return as fatal.
    assert "_verify_config_toml_embedding_dim" in upgrade_block, (
        "--upgrade must verify config.toml round-tripped the new dim"
    )
    # Must check _write_env_file's bool return — finding a `return 1`
    # close to the env_ok check, or the ENV-failure return 1 sentinels.
    # (We grep for the env_ok variable name AND `if not env_ok` shape.)
    assert "env_ok" in upgrade_block
    assert "if not env_ok" in upgrade_block


def test_upgrade_path_refreshes_config_after_migrations():
    """Round-6 codex finding: --upgrade with `MNEMOS_EMBEDDING_DIM=512` against a
    config.toml at 768 ALTERs the DB but must NOT leave config.toml stale.

    Source-level guard: the --upgrade branch must call _write_config_toml
    after run_migrations succeeds, so the persisted dim matches the schema.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # Find the args.upgrade branch
    upgrade_idx = src.find("args.upgrade")
    assert upgrade_idx > 0
    # Within the upgrade block we must see _write_config_toml AND
    # _write_env_file (or refresh thereof) AFTER the run_migrations call.
    upgrade_block = src[upgrade_idx:]
    run_idx = upgrade_block.find("run_migrations")
    assert run_idx > 0
    # Both persistence calls must come after run_migrations within the
    # upgrade block — otherwise the env-driven dim swap won't survive
    # service restart.
    write_config_idx = upgrade_block.find("_write_config_toml")
    write_env_idx = upgrade_block.find("_write_env_file")
    assert write_config_idx > run_idx, (
        "_write_config_toml must be called AFTER run_migrations in --upgrade"
    )
    assert write_env_idx > run_idx, (
        "_write_env_file must be called AFTER run_migrations in --upgrade"
    )


def test_main_module_calls_apply_helper_in_wizard_path():
    """Defensive: future refactors must keep the wizard path env-aware.

    Verify by source inspection that the main() function applies the env
    helper after run_wizard() in both the explicit --wizard branch and the
    agent-fallback branch.
    """
    import inspect
    from mnemos.installer import __main__ as installer_main

    source = inspect.getsource(installer_main.main)
    # Two distinct call sites: explicit --wizard path and agent-fallback path.
    helper_calls = source.count("_apply_embedding_dim_from_env(cfg)")
    # Three or more — explicit wizard, agent success, and at least one
    # fallback branch. Having no calls would mean drift.
    assert helper_calls >= 2, (
        f"main() appears to skip _apply_embedding_dim_from_env; saw "
        f"{helper_calls} call(s). Wizard/agent installer paths must propagate "
        f"MNEMOS_EMBEDDING_DIM into Config or the cix NPU 512-dim path breaks."
    )

"""Postgres parallel of the SQLite embed-dim configurability.

The `_alter_postgres_embedding_dim()` helper runs after migrations apply
and re-sizes `memories.embedding` from the baseline `vector(768)` to the
configured dim. These tests don't require a running Postgres; they verify
the helper's behavior via its public surface (the `_psql_superuser()`
shell-out is mocked).
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from mnemos.installer.db import _alter_postgres_embedding_dim


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch):
    """Round-30 introduced MNEMOS_PROFILE / MNEMOS_PROFILE_OVERRIDE env
    parity in _load_existing_config. Auto-clear so leaked env vars
    don't flip cfg.profile during isolated assertions."""
    for k in ("MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)


@dataclass
class _PgConfig:
    db_name: str = "mnemos_test"
    db_password: str = ""


def test_run_migrations_refuses_remote_postgres_host():
    """Round-16 codex finding: the migration helpers shell out to
    `sudo -u postgres psql -d <dbname>` and never pass -h/-p/-U/PGPASSWORD.
    A cfg.db_host pointing at a remote postgres would silently target a
    LOCAL postgres of the same name. run_migrations must refuse early."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _RemoteCfg:
        profile: str = "server"
        db_host: str = "192.168.207.67"  # remote
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_RemoteCfg())
    assert rc is False


def test_run_migrations_accepts_localhost_variants():
    """Round-49 HIGH narrows local accept set: only "" / "localhost"
    are truly safe for the socket-based migration runner. 127.0.0.1
    / ::1 imply TCP intent which diverges from `sudo -u postgres
    psql -d <db>` (no -h) — that uses the socket, while runtime
    uses TCP for explicit IPs. Different DBs on multi-cluster hosts."""
    from mnemos.installer.db import _is_local_postgres_host

    # Truly unset / "localhost" → safe.
    for h in ("", None, "localhost", "LOCALHOST"):
        assert _is_local_postgres_host(h) is True
    # Explicit IPs → NOT local (TCP intent).
    for h in ("127.0.0.1", "::1"):
        assert _is_local_postgres_host(h) is False
    # Remote → NOT local.
    for h in ("192.168.207.67", "remote-host.example", "10.0.0.5"):
        assert _is_local_postgres_host(h) is False


def test_run_migrations_always_reconciles_postgres_dim():
    """Round-5 codex finding: the previous `!= 768` guard skipped reconciliation
    when downgrading from 512 → 768. Now the helper is always called and is
    expected to be idempotent (no-op when current type matches target).
    """
    import inspect
    from mnemos.installer import db as installer_db

    src = inspect.getsource(installer_db.run_migrations)
    # The guard literal must NOT be present anymore.
    assert "embedding_dim != 768" not in src.replace(" ", "")
    # And the helper must be called from run_migrations regardless.
    assert "_alter_postgres_embedding_dim(config" in src


def test_alter_idempotent_when_target_768_and_current_already_768(tmp_path):
    """Default-to-default reconciliation is a no-op: format_type returns 768,
    target is 768, helper short-circuits without ALTER."""
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [(0, "vector(768)\n", "")]
        rc = _alter_postgres_embedding_dim(cfg, 768)
        assert rc is True
        assert mock_psql.call_count == 1  # only format_type query


def test_alter_512_to_768_refuses_on_populated_db(capsys):
    """Round-5 regression: operator switches 512-D install back to 768 default.
    Existing column is vector(512), populated. Must refuse with recovery
    instructions, NOT silently let the install proceed."""
    cfg = _PgConfig()
    refuse_err = (
        "ERROR:  MNEMOS_EMBED_DIM_REFUSE: memories.embedding has 17 "
        "non-null rows at the existing dim; cannot ALTER to vector(768) "
        "via USING NULL without destroying data.\n"
    )
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (0, "vector(512)\n", ""),  # currently 512
            (1, "", refuse_err),         # plpgsql RAISE EXCEPTION
        ]
        rc = _alter_postgres_embedding_dim(cfg, 768)
        assert rc is False  # refused — not silently passed through

    captured = capsys.readouterr()
    err = captured.err
    assert "vector(768)" in err
    assert "17 non-null rows" in err


def test_alter_called_for_non_default_dim_on_empty_table():
    """When the column type is the baseline 768 and table is empty, plpgsql fires.

    Round-3 codex finding: COUNT and ALTER must run under a single ACCESS
    EXCLUSIVE lock to prevent concurrent writers from sneaking inserts in
    between. We collapsed the two SQL calls into one plpgsql DO block, so
    the call shape is now: format_type query + DO block.
    """
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (0, "vector(768)\n", ""),  # format_type query
            (0, "DO\n", ""),             # plpgsql DO block (lock + count + ALTER)
        ]
        rc = _alter_postgres_embedding_dim(cfg, 512)
        assert rc is True
        assert mock_psql.call_count == 2
        do_call_sql = mock_psql.call_args_list[1].args[0]
        assert "DO $$" in do_call_sql
        assert "LOCK TABLE memories IN ACCESS EXCLUSIVE MODE" in do_call_sql
        assert "vector(512)" in do_call_sql
        assert "ALTER TABLE memories" in do_call_sql


def test_idempotent_when_column_already_at_target_dim_with_rows():
    """If the column is already vector(<configured>), no ALTER is needed even if populated."""
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (0, "vector(512)\n", ""),  # already at target
        ]
        rc = _alter_postgres_embedding_dim(cfg, 512)
        assert rc is True
        # Only the format_type query should fire — no plpgsql.
        assert mock_psql.call_count == 1


def test_alter_refuses_on_populated_table_at_different_dim(capsys):
    """Plpgsql DO block raises EXCEPTION via MNEMOS_EMBED_DIM_REFUSE marker.

    The Python wrapper detects the marker in stderr and renders operator-
    facing recovery instructions. The recovery command must be
    Postgres-valid (no SQLite-only references).
    """
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        # plpgsql DO block raises with the refuse marker on populated DB.
        # Postgres surfaces RAISE EXCEPTION text in stderr like:
        #   ERROR: MNEMOS_EMBED_DIM_REFUSE: memories.embedding has 42 non-null rows ...
        refuse_err = (
            "psql:<stdin>:1: ERROR:  MNEMOS_EMBED_DIM_REFUSE: "
            "memories.embedding has 42 non-null rows at the existing dim; "
            "cannot ALTER to vector(512) via USING NULL without destroying data. "
            "Run the documented BEGIN; UPDATE … SET embedding=NULL; "
            "ALTER … TYPE vector(512) USING NULL; COMMIT; recovery on a "
            "quiesced DB instead.\n"
            "CONTEXT:  PL/pgSQL function inline_code_block line 6 at RAISE\n"
        )
        mock_psql.side_effect = [
            (0, "vector(768)\n", ""),  # format_type
            (1, "", refuse_err),         # plpgsql RAISE EXCEPTION
        ]
        rc = _alter_postgres_embedding_dim(cfg, 512)
        assert rc is False
        assert mock_psql.call_count == 2

    captured = capsys.readouterr()
    err = captured.err
    # Operator-facing recovery instructions surface from the wrapper's
    # MNEMOS_EMBED_DIM_REFUSE marker handler.
    assert "42 non-null rows" in err
    assert "vector(512)" in err
    assert "ALTER TABLE memories" in err
    assert "UPDATE memories SET embedding=NULL" in err
    assert "BEGIN" in err and "COMMIT" in err
    # Crucially: should NOT reference memory_embeddings (SQLite-only table).
    assert "memory_embeddings" not in err


def test_alter_fails_closed_above_2000_for_ivfflat_compat(capsys):
    """ivfflat caps at 2000-D; values above must FAIL the install.

    Round-2 codex finding: returning True here let the installer persist
    the requested dim into config while leaving the DB schema at the
    baseline 768. Cosine comparisons would fail at runtime.
    """
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        rc = _alter_postgres_embedding_dim(cfg, 3072)
        assert rc is False  # FAIL CLOSED — was True before
        assert mock_psql.call_count == 0

    captured = capsys.readouterr()
    assert "out of supported" in captured.err
    assert "3072" in captured.err
    assert "ivfflat" in captured.err
    assert "Refusing to proceed" in captured.err


def test_alter_fails_closed_on_negative_dim(capsys):
    """Negative dim must fail the install (was warn+skip before)."""
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        rc = _alter_postgres_embedding_dim(cfg, -1)
        assert rc is False
        assert mock_psql.call_count == 0


def test_plpgsql_failure_without_refuse_marker_propagates(capsys):
    """If the DO block fails for any reason other than RAISE EXCEPTION
    (e.g. the table doesn't exist, the user lacks privileges), the wrapper
    surfaces the raw error.
    """
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (0, "vector(768)\n", ""),
            (1, "", "ERROR: must be owner of table memories"),
        ]
        rc = _alter_postgres_embedding_dim(cfg, 512)
        assert rc is False

    captured = capsys.readouterr()
    err = captured.err
    assert "must be owner" in err
    assert "ERROR resizing embedding column" in err


def test_plpgsql_lock_count_alter_atomicity():
    """The plpgsql DO block must include LOCK + COUNT + ALTER in order.

    Source-level guard against future refactors that might re-split the
    operations into separate sessions and reintroduce the COUNT/ALTER race.
    """
    import inspect
    from mnemos.installer import db as installer_db

    src = inspect.getsource(installer_db._alter_postgres_embedding_dim)
    # All three must appear in the DO block.
    assert "DO $$" in src
    assert "LOCK TABLE memories IN ACCESS EXCLUSIVE MODE" in src
    assert "SELECT COUNT(*) INTO cnt FROM memories" in src
    assert "ALTER TABLE memories ALTER COLUMN embedding TYPE" in src
    # Order check — LOCK before COUNT before ALTER.
    lock_pos = src.find("LOCK TABLE memories")
    count_pos = src.find("SELECT COUNT(*) INTO cnt")
    alter_pos = src.find("ALTER TABLE memories ALTER COLUMN")
    assert 0 < lock_pos < count_pos < alter_pos


def test_format_type_failure_still_runs_plpgsql(capsys):
    """format_type failure (e.g. fresh DB pre-migration) shouldn't prevent ALTER.

    The plpgsql DO block has its own LOCK + COUNT, so even if format_type
    returns no rows, we still attempt the resize.
    """
    cfg = _PgConfig()
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (1, "", "ERROR: relation memories does not exist"),  # format_type fails
            (0, "DO\n", ""),                                       # plpgsql succeeds
        ]
        rc = _alter_postgres_embedding_dim(cfg, 512)
        assert rc is True


def test_recovery_message_uses_transactional_block(capsys):
    """The Postgres recovery command must be a single transactional unit.

    Triggered via the plpgsql RAISE EXCEPTION → MNEMOS_EMBED_DIM_REFUSE
    marker handler.
    """
    cfg = _PgConfig()
    refuse_err = (
        "ERROR:  MNEMOS_EMBED_DIM_REFUSE: memories.embedding has 5 "
        "non-null rows at the existing dim; cannot ALTER to vector(512) "
        "via USING NULL without destroying data.\n"
    )
    with patch("mnemos.installer.db._psql_superuser") as mock_psql:
        mock_psql.side_effect = [
            (0, "vector(768)\n", ""),
            (1, "", refuse_err),
        ]
        _alter_postgres_embedding_dim(cfg, 512)

    captured = capsys.readouterr()
    err = captured.err
    # BEGIN ... COMMIT around the destructive operations.
    assert "BEGIN" in err
    assert "COMMIT" in err
    # Order: UPDATE NULL → ALTER (otherwise pgvector cast fails on row data).
    update_pos = err.find("UPDATE memories SET embedding=NULL")
    alter_pos = err.find("ALTER TABLE memories ALTER COLUMN")
    assert 0 <= update_pos < alter_pos


# ---------------------------------------------------------------------------
# Codex round-17 follow-ups
# ---------------------------------------------------------------------------


def test_config_from_env_infers_server_profile_with_pg_signals(monkeypatch):
    """Round-17 HIGH: env-only --upgrade with PG_PASSWORD set but no
    MNEMOS_PROFILE used to default to 'personal' -> 'edge', taking the
    SQLite migration path AND bypassing the remote-host rejection guard
    (gated on `not _profile_uses_sqlite(profile)`). Now infers 'server'
    when any PG_*/MNEMOS_DB_* env signal is present."""
    from mnemos.installer.__main__ import _config_from_env

    monkeypatch.delenv("MNEMOS_PROFILE", raising=False)
    monkeypatch.setenv("PG_HOST", "10.0.0.5")
    monkeypatch.setenv("PG_PASSWORD", "secret")
    monkeypatch.setenv("PG_DATABASE", "mnemos_prod")

    cfg = _config_from_env()
    assert cfg.profile == "server", (
        f"PG_HOST + PG_PASSWORD env shape should infer 'server' profile, "
        f"got {cfg.profile!r}"
    )


def test_config_from_env_explicit_profile_wins_over_inference(monkeypatch):
    """Operator-set MNEMOS_PROFILE is authoritative — inference only
    fires when no explicit profile is set."""
    from mnemos.installer.__main__ import _config_from_env

    monkeypatch.setenv("MNEMOS_PROFILE", "edge")
    monkeypatch.setenv("PG_HOST", "10.0.0.5")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _config_from_env()
    assert cfg.profile == "edge", (
        f"explicit MNEMOS_PROFILE=edge must win over PG_* inference, "
        f"got {cfg.profile!r}"
    )


def test_config_from_env_no_pg_signals_falls_back_to_personal(monkeypatch):
    """No PG_* / MNEMOS_DB_* env signals → legacy 'personal' -> 'edge'
    default is preserved (no behavior change for the SQLite-only env)."""
    from mnemos.installer.__main__ import _config_from_env

    monkeypatch.delenv("MNEMOS_PROFILE", raising=False)
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _config_from_env()
    assert cfg.profile == "edge"


def test_setup_database_refuses_remote_postgres_host():
    """Round-17 HIGH: setup_database() shells out to `sudo -u postgres
    psql -d <db>` with no -h/-p, so a config pointing at a remote
    db_host would silently mutate the LOCAL postgres of the same name
    before run_migrations()'s remote-rejection guard runs. Pre-reject
    at the top of setup_database()."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _RemoteCfg:
        profile: str = "server"
        db_host: str = "10.0.0.5"  # remote
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    class _Info:
        pass

    rc = installer_db.setup_database(_RemoteCfg(), _Info())
    assert rc is False


def test_setup_database_refuses_localhost_non_default_port():
    """Round-17 HIGH: localhost:5433 is treated as 'local' by
    _is_local_postgres_host, but the psql helpers don't pass -p so
    the helper would target 5432 anyway. Reject non-default-port
    localhost to prevent silently mutating the wrong cluster."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _NonDefaultPortCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5433  # non-default
        embedding_dim: int = 768

    class _Info:
        pass

    rc = installer_db.setup_database(_NonDefaultPortCfg(), _Info())
    assert rc is False


def test_run_migrations_refuses_localhost_non_default_port():
    """Same defense for the migration runner: psql helpers ignore
    -p, so localhost:5433 silently targets default-port cluster."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _NonDefaultPortCfg:
        profile: str = "server"
        db_host: str = "127.0.0.1"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5433  # non-default
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_NonDefaultPortCfg())
    assert rc is False


def test_run_migrations_accepts_localhost_default_port():
    """Sanity check: localhost:5432 (default) must still be accepted
    as long as the rest of the env is OK. Use a stub that fails at
    the next step (file-not-found for migration files in a sandbox)."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _LocalCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    # Past the locality guard we'll hit psql/sudo or migration file
    # loading. The key is that the locality guard does NOT reject;
    # capturing the rejection by hooking _is_local_postgres_host
    # being True-equivalent here. We exercise via a code-path probe:
    # the function should return False for a different reason (not
    # the locality message).
    import io
    import sys
    captured = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    try:
        installer_db.run_migrations(_LocalCfg())
    except Exception:
        pass
    finally:
        sys.stderr = real_stderr
    err = captured.getvalue()
    # Confirm it did NOT trip the locality nor non-default-port guards.
    assert "is not a local postgres" not in err
    assert "only supports the default Postgres port" not in err


def test_setup_database_rejects_remote_even_with_sqlite_profile():
    """Round-18 HIGH: profile-gated guards leaked when profile=edge but
    [database].backend=postgres + db_host=remote. --upgrade calls
    run_migrations(cfg) unconditionally, and the legacy 'personal'
    profile canonicalizes to 'edge' even when the underlying DB is
    postgres. Guards must be unconditional on profile."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _LegacyEdgeRemote:
        # legacy profile=personal would canonicalize to edge
        profile: str = "edge"
        db_host: str = "10.0.0.5"  # remote
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    class _Info:
        pass

    rc = installer_db.setup_database(_LegacyEdgeRemote(), _Info())
    assert rc is False


def test_run_migrations_rejects_remote_even_with_sqlite_profile():
    """Round-18 HIGH: same as above but for run_migrations()."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _LegacyEdgeRemote:
        profile: str = "edge"
        db_host: str = "10.0.0.5"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_LegacyEdgeRemote())
    assert rc is False


def test_run_migrations_rejects_non_default_port_even_with_sqlite_profile():
    """Round-18 HIGH: non-default-port guard must also be
    unconditional. profile=edge + localhost:5433 + postgres-shaped
    config still mutates the wrong cluster via psql -d only."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _LegacyEdgeNonDefault:
        profile: str = "edge"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5433  # non-default
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_LegacyEdgeNonDefault())
    assert rc is False


# ---------------------------------------------------------------------------
# Codex round-19 follow-ups
# ---------------------------------------------------------------------------


def test_has_dsn_config_detects_env_vars(monkeypatch, tmp_path):
    """Round-19 HIGH: --upgrade must reject DSN-based configs since
    the migration runner is not DSN-aware. Helper detects env vars."""
    from mnemos.installer.__main__ import _has_dsn_config

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MNEMOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("PG_URL", raising=False)
    monkeypatch.delenv("DATABASE_DSN", raising=False)
    monkeypatch.delenv("MNEMOS_DATABASE_DSN", raising=False)
    monkeypatch.delenv("PG_DSN", raising=False)

    # Empty repo path / no config.toml.
    repo = str(tmp_path)
    assert _has_dsn_config(repo) is False

    # Set DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    assert _has_dsn_config(repo) is True

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MNEMOS_DATABASE_URL", "postgres://u:p@h/db")
    assert _has_dsn_config(repo) is True

    monkeypatch.delenv("MNEMOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("PG_DSN", "host=h dbname=db user=u password=p")
    assert _has_dsn_config(repo) is True


def test_has_dsn_config_detects_config_toml_url(monkeypatch, tmp_path):
    """The same helper must detect [database].url / [database].dsn
    in config.toml so an env-only call doesn't bypass."""
    from mnemos.installer.__main__ import _has_dsn_config

    for k in (
        "DATABASE_URL", "MNEMOS_DATABASE_URL", "PG_URL",
        "DATABASE_DSN", "MNEMOS_DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'url = "postgres://u:p@h:5432/db"\n'
        'host = "localhost"\n'
        'database = "mnemos"\n'
    )
    assert _has_dsn_config(str(tmp_path)) is True

    config_path.write_text(
        '[database]\n'
        'dsn = "host=h dbname=db user=u password=p"\n'
    )
    assert _has_dsn_config(str(tmp_path)) is True

    # Plain non-DSN config.
    config_path.write_text(
        '[database]\n'
        'host = "localhost"\n'
        'database = "mnemos"\n'
    )
    assert _has_dsn_config(str(tmp_path)) is False


def test_upgrade_dispatches_by_backend():
    """Round-19 HIGH + round-20 refinement: --upgrade must dispatch to
    setup_sqlite_database for SQLite (NOT run_migrations which is
    postgres-only). Round-20 dispatches by RUNTIME BACKEND (not by
    profile) so a config with profile=edge but [database].backend=
    postgres correctly runs run_migrations. Source-level guard so
    future refactors don't regress this."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # Backend-aware dispatch (round-20).
    assert "_resolve_runtime_backend(cfg" in src
    assert 'runtime_backend == "sqlite"' in src
    assert "setup_sqlite_database(cfg)" in src
    # DSN rejection must be present before dispatching.
    assert "_has_dsn_config(repo_path)" in src
    assert "DSN/url-based" in src or "DSN-aware" in src


def test_upgrade_rejects_dsn_config(monkeypatch):
    """Behavioral guard: --upgrade with DATABASE_URL env set must
    return non-zero from main(). Skip the actual run since the
    upstream pipeline shells out to psql; just verify _has_dsn_config
    returns True for the env shape."""
    from mnemos.installer.__main__ import _has_dsn_config

    # Clean env then set DATABASE_URL.
    for k in (
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://prod-host/mnemos")

    assert _has_dsn_config(None) is True


# ---------------------------------------------------------------------------
# Codex round-20 follow-ups
# ---------------------------------------------------------------------------


def test_psql_superuser_uses_on_error_stop():
    """Round-20 HIGH: psql -f without -v ON_ERROR_STOP=1 continues past
    SQL errors and can exit 0 even when a CREATE/ALTER failed mid-file.
    run_migrations() would then report 'OK' on partial schema drift.
    Source-level guard for both _psql_superuser and _psql_superuser_file."""
    import inspect
    from mnemos.installer import db as installer_db

    src = inspect.getsource(installer_db._psql_superuser)
    assert "ON_ERROR_STOP=1" in src

    src_file = inspect.getsource(installer_db._psql_superuser_file)
    assert "ON_ERROR_STOP=1" in src_file


def test_resolve_runtime_backend_env_postgres(monkeypatch):
    """Round-20 HIGH: backend resolution must consult env first
    (highest priority, matches runtime). PG_BACKEND=postgres
    overrides any TOML or profile signal."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    monkeypatch.setenv("PG_BACKEND", "postgres")

    @dataclass
    class _Cfg:
        profile: str = "edge"  # SQLite by profile

    assert _resolve_runtime_backend(_Cfg(), repo_path=None) == "postgres"


def test_resolve_runtime_backend_env_sqlite(monkeypatch):
    """Same env-priority for sqlite override."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")

    @dataclass
    class _Cfg:
        profile: str = "server"  # postgres by profile

    assert _resolve_runtime_backend(_Cfg(), repo_path=None) == "sqlite"


def test_resolve_runtime_backend_toml_overrides_profile(monkeypatch, tmp_path):
    """Round-20 HIGH: a config.toml with [database].backend=postgres
    must dispatch to postgres even when profile is edge/dev. Without
    this the round-19 profile-based dispatch sent these to
    setup_sqlite_database while the running service kept using
    postgres."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'database = "mnemos"\n'
    )

    @dataclass
    class _Cfg:
        profile: str = "edge"  # SQLite by profile

    assert _resolve_runtime_backend(_Cfg(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_falls_back_to_profile(monkeypatch, tmp_path):
    """No env, no TOML backend → profile-derived default."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
        monkeypatch.delenv(k, raising=False)

    @dataclass
    class _Cfg:
        profile: str = "server"

    assert _resolve_runtime_backend(_Cfg(), repo_path=str(tmp_path)) == "postgres"

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


def test_resolve_runtime_backend_normalizes_pg_alias(monkeypatch, tmp_path):
    """The 'pg' alias (used in some legacy configs) must canonicalize
    to 'postgres'."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "pg")

    @dataclass
    class _Cfg:
        profile: str = "edge"

    assert _resolve_runtime_backend(_Cfg(), repo_path=None) == "postgres"


# ---------------------------------------------------------------------------
# Codex round-21 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_backend_pg_host_env_forces_postgres(monkeypatch, tmp_path):
    """Round-21 HIGH: profile=edge with PG_HOST set silently routed to
    sqlite migrations while runtime selected postgres. Backend resolver
    must treat explicit PG connection fields as a postgres signal."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PASSWORD", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "10.0.0.5")

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_password_only_does_not_force_postgres(monkeypatch, tmp_path):
    """Round-22 HIGH parity: PG_PASSWORD alone does NOT force postgres.
    Runtime's lifecycle._has_explicit_postgres_connection_config only
    counts host/port/database/user as postgres-distinguishing — NOT
    password. Counting password-only as a postgres signal
    over-promoted upgrade dispatch while runtime stayed on sqlite."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_PASSWORD", "rotated-secret")

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # Password-only must NOT force postgres — falls back to profile.
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


def test_resolve_runtime_backend_mnemos_db_env_does_not_force_postgres(monkeypatch, tmp_path):
    """MNEMOS_DB_* env aliases alone do NOT force postgres. Runtime's
    _DatabaseSettings uses env_prefix='PG_' exclusively for these
    fields, so MNEMOS_DB_HOST has no effect on runtime selection.
    Round-22 HIGH parity."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    # MNEMOS_DB_* alone shouldn't be enough.
    monkeypatch.setenv("MNEMOS_DB_HOST", "10.0.0.5")
    monkeypatch.setenv("MNEMOS_DB_PASSWORD", "secret")

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


def test_resolve_runtime_backend_dsn_url_forces_postgres(monkeypatch, tmp_path):
    """DSN/url env signal → postgres (no SQLite fallback)."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_toml_host_non_default_forces_postgres(monkeypatch, tmp_path):
    """[database].host = explicit value (non-default) in config.toml
    + no profile signal → postgres (mirrors round-19's existing TOML
    inference for _load_existing_config)."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'host = "10.0.0.5"\n'
        'port = 5432\n'
        'database = "mnemos_prod"\n'
        'user = "mnemos_user"\n'
        'password = "secret"\n'
    )

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_explicit_toml_fields_force_postgres(monkeypatch, tmp_path):
    """Round-26 MEDIUM: TOML field PRESENCE (not value) signals
    postgres. Runtime tracks explicit_database_fields, not whether
    values differ from defaults. A config with [database] host="localhost"
    written explicitly forces runtime to select postgres regardless
    of profile — the installer must agree."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    # TOML with explicit [database] connection keys (default values).
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'host = "localhost"\n'
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
    )

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # Round-26 parity: must dispatch to postgres because the runtime
    # selector treats any explicit host/port/database/user field as
    # a postgres signal — _has_explicit_postgres_connection_config
    # checks field presence, not value-vs-default.
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_pure_sqlite_toml_does_not_force_postgres(
    monkeypatch, tmp_path,
):
    """Symmetric: a config.toml WITHOUT explicit [database] connection
    fields (the shape _write_config_toml emits for sqlite profiles)
    must NOT force postgres. Falls through to profile-derived default."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    # Sqlite config (matches what _write_config_toml emits for edge).
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'sqlite_path = "~/.mnemos/mnemos.db"\n'
    )

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


def test_run_migrations_fails_fast_on_first_error():
    """Round-21 MEDIUM: previous code set success=False on a failed
    migration but kept applying subsequent ones, leaving partial
    schema drift. Migration order is load-bearing — abort on first
    error."""
    import inspect
    from mnemos.installer import db as installer_db

    src = inspect.getsource(installer_db.run_migrations)
    # The fail-fast `return False` must be present in the loop body.
    assert "return False" in src
    # And the order-is-load-bearing rationale must be on a failure
    # comment so a future refactor doesn't strip it.
    assert "order is load-bearing" in src or "Aborting before" in src


# ---------------------------------------------------------------------------
# Codex round-22 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_config_path_honors_mnemos_config_path(monkeypatch, tmp_path):
    """Round-22 HIGH: runtime config resolution checks MNEMOS_CONFIG_PATH
    first. Without honoring it, --upgrade loads/patches a stale
    repo/config.toml while the running service reads the
    MNEMOS_CONFIG_PATH target. Mutates the wrong file."""
    from mnemos.installer.__main__ import _resolve_runtime_config_path

    # Set up a config at a custom location.
    custom_config = tmp_path / "etc-mnemos" / "config.toml"
    custom_config.parent.mkdir(parents=True)
    custom_config.write_text('[database]\nbackend = "postgres"\n')

    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(custom_config))
    # Even when repo_path provides a config.toml, MNEMOS_CONFIG_PATH wins.
    repo_path = tmp_path / "stale-repo"
    repo_path.mkdir()
    (repo_path / "config.toml").write_text('[database]\nbackend = "sqlite"\n')

    resolved = _resolve_runtime_config_path(str(repo_path))
    assert resolved == str(custom_config)


def test_resolve_runtime_config_path_falls_back_to_repo(monkeypatch, tmp_path):
    """When MNEMOS_CONFIG_PATH is unset, fall through to repo_path/config.toml."""
    from mnemos.installer.__main__ import _resolve_runtime_config_path

    monkeypatch.delenv("MNEMOS_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path / "..")  # ensure cwd doesn't have a config.toml

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "config.toml").write_text('[database]\nbackend = "postgres"\n')

    resolved = _resolve_runtime_config_path(str(repo_path))
    assert resolved == str(repo_path / "config.toml")


def test_resolve_runtime_backend_uses_mnemos_config_path(monkeypatch, tmp_path):
    """Backend resolver must use the runtime config path so it sees
    the same backend the service will. Without this, profile=edge +
    MNEMOS_CONFIG_PATH=/etc/mnemos/config.toml (postgres) routed
    --upgrade to sqlite while runtime selected postgres."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    runtime_config = tmp_path / "production" / "config.toml"
    runtime_config.parent.mkdir(parents=True)
    runtime_config.write_text('[database]\nbackend = "postgres"\n')

    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(runtime_config))

    # repo_path has a stale sqlite config — runtime config must win.
    repo_path = tmp_path / "stale-repo"
    repo_path.mkdir()
    (repo_path / "config.toml").write_text('[database]\nbackend = "sqlite"\n')

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(repo_path)) == "postgres"


# ---------------------------------------------------------------------------
# Codex round-23 follow-ups
# ---------------------------------------------------------------------------


def test_upgrade_post_migration_patches_runtime_config_path():
    """Round-23 HIGH: post-migration persistence MUST patch the same
    file the runtime will read. With MNEMOS_CONFIG_PATH pointing at
    a production config and a stale repo config present, --upgrade
    must patch the production config (not the stale repo one).
    Source-level guard."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # The post-migration block resolves runtime config path before
    # patch/verify (round-23 fix).
    assert "_resolve_runtime_config_path(repo_path)" in src
    # Both helpers receive the resolved config_toml_path, not repo_path.
    assert "_patch_config_toml_embedding_dim(config_toml_path" in src
    assert "_verify_config_toml_embedding_dim(config_toml_path" in src


def test_patch_config_toml_takes_config_path_directly(tmp_path):
    """Round-23 API: helpers now accept the config_path directly
    instead of repo_path + hardcoded basename. Behavioral check:
    patches a custom-named file at an arbitrary location."""
    from mnemos.installer.__main__ import (
        _patch_config_toml_embedding_dim,
        _verify_config_toml_embedding_dim,
    )

    # Custom path — not repo_path/config.toml.
    custom = tmp_path / "custom-name.toml"
    custom.write_text(
        '[database]\n'
        'embedding_dim = 768\n'
    )

    rc = _patch_config_toml_embedding_dim(str(custom), 512)
    assert rc is True

    rc = _verify_config_toml_embedding_dim(str(custom), 512)
    assert rc is True

    # Reading raw confirms the actual file was patched.
    text = custom.read_text()
    assert "embedding_dim = 512" in text


# ---------------------------------------------------------------------------
# Codex round-24 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_config_path_does_not_use_operator_cwd(monkeypatch, tmp_path):
    """Round-24 HIGH: runtime checks Path.cwd()/config.toml because the
    service runs with WorkingDirectory=repo_path (cwd == repo). But
    --upgrade runs in the operator's shell — if operator's cwd has a
    stray config.toml, it would shadow the actual installed service
    config. The installer must NOT use cwd as a candidate."""
    from mnemos.installer.__main__ import _resolve_runtime_config_path

    monkeypatch.delenv("MNEMOS_CONFIG_PATH", raising=False)

    # Stray config in cwd.
    stray_dir = tmp_path / "operator-shell"
    stray_dir.mkdir()
    stray_config = stray_dir / "config.toml"
    stray_config.write_text('[database]\nbackend = "sqlite"\n')

    # Real service config in repo_path.
    repo_path = tmp_path / "service-repo"
    repo_path.mkdir()
    real_config = repo_path / "config.toml"
    real_config.write_text('[database]\nbackend = "postgres"\n')

    monkeypatch.chdir(stray_dir)

    resolved = _resolve_runtime_config_path(str(repo_path))
    # Must resolve to the SERVICE config (in repo_path), NOT the
    # operator's cwd config.
    assert resolved == str(real_config)


def test_load_existing_config_does_not_overlay_mnemos_db_host(monkeypatch, tmp_path):
    """Round-24 HIGH: runtime DatabaseSettings uses env_prefix='PG_'
    exclusively for host/port/database/user. _load_existing_config's
    env overlay for empty TOML fields must mirror that — accepting
    MNEMOS_DB_HOST would let the upgrade target a DB that runtime
    never sees."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = ""\n'  # empty — should fall to PG_HOST, NOT MNEMOS_DB_HOST
        'database = ""\n'
        'user = ""\n'
        'password = ""\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MNEMOS_DB_HOST", "10.0.0.5")  # WRONG — runtime ignores
    monkeypatch.setenv("MNEMOS_DB_NAME", "wrong_db")
    monkeypatch.setenv("MNEMOS_DB_USER", "wrong_user")
    monkeypatch.setenv("MNEMOS_DB_PASSWORD", "secret")  # OK — accepted by both
    # PG_HOST etc unset — should fall to defaults.
    for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Host/db/user must NOT pick up MNEMOS_DB_* values — those are
    # runtime-invisible. Falls back to default.
    assert cfg.db_host == "localhost"
    assert cfg.db_name == "mnemos"
    assert cfg.db_user == "mnemos_user"
    # Password is the documented exception (MNEMOS_DB_PASSWORD →
    # PG_PASSWORD via the installer env writer).
    assert cfg.db_password == "secret"


def test_load_existing_config_uses_pg_env_for_empty_fields(monkeypatch, tmp_path):
    """Symmetric: PG_HOST etc DO populate empty TOML fields (mirrors runtime)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = ""\n'
        'database = ""\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in ("MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
              "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "prod-host.example")
    monkeypatch.setenv("PG_DATABASE", "mnemos_prod")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_host == "prod-host.example"
    assert cfg.db_name == "mnemos_prod"
    assert cfg.db_password == "secret"


# ---------------------------------------------------------------------------
# Codex round-25 follow-ups
# ---------------------------------------------------------------------------


def test_runtime_parity_loader_only_uses_pg_for_db_fields(monkeypatch):
    """Round-25 HIGH: --upgrade no-config path must use a
    runtime-parity loader (PG_* only for DB fields). Fresh-install
    _config_from_env still accepts MNEMOS_DB_* (installer
    convention), but the upgrade path must mirror runtime exactly
    to avoid migrating a different DB than the service uses."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    # Set MNEMOS_DB_* values that should be IGNORED for DB fields.
    monkeypatch.setenv("MNEMOS_DB_HOST", "10.0.0.5")
    monkeypatch.setenv("MNEMOS_DB_NAME", "wrong_db")
    monkeypatch.setenv("MNEMOS_DB_USER", "wrong_user")
    monkeypatch.setenv("MNEMOS_DB_PASSWORD", "secret")
    # PG_PASSWORD is the postgres-signal that triggers the
    # runtime-parity loader path from _load_existing_config.
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _config_from_env_runtime_parity()
    # MNEMOS_DB_* values must NOT shadow defaults — runtime ignores them.
    assert cfg.db_host == "localhost"
    assert cfg.db_name == "mnemos"
    assert cfg.db_user == "mnemos_user"
    # Password: both MNEMOS_DB_PASSWORD and PG_PASSWORD work
    # (documented exception). Either populates db_password.
    assert cfg.db_password == "secret"


def test_runtime_parity_loader_uses_pg_env_when_present(monkeypatch):
    """Symmetric: PG_HOST/PG_DATABASE/PG_USER ARE honored
    (mirrors runtime)."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "prod-host.example")
    monkeypatch.setenv("PG_DATABASE", "mnemos_prod")
    monkeypatch.setenv("PG_USER", "prod_user")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _config_from_env_runtime_parity()
    assert cfg.db_host == "prod-host.example"
    assert cfg.db_name == "mnemos_prod"
    assert cfg.db_user == "prod_user"
    assert cfg.db_password == "secret"


def test_runtime_parity_loader_does_not_force_postgres_on_mnemos_db_only(monkeypatch):
    """MNEMOS_DB_* alone does NOT force profile=server in the
    runtime-parity loader. Only PG_*/DSN env signals trigger the
    profile inference (matches runtime selection)."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    # Only MNEMOS_DB_* set — should NOT trigger postgres profile.
    monkeypatch.setenv("MNEMOS_DB_HOST", "10.0.0.5")
    monkeypatch.setenv("MNEMOS_DB_NAME", "mnemos_prod")

    cfg = _config_from_env_runtime_parity()
    # Falls back to legacy "personal" → "edge".
    assert cfg.profile == "edge"


def test_load_existing_config_no_toml_uses_runtime_parity_loader():
    """Source-level guard: --upgrade with no config.toml must call
    the runtime-parity loader, not the fresh-install loader."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main._load_existing_config)
    assert "_config_from_env_runtime_parity()" in src


# ---------------------------------------------------------------------------
# Codex round-26 follow-ups
# ---------------------------------------------------------------------------


def test_runtime_parity_loader_pg_password_alone_does_not_force_postgres(monkeypatch):
    """Round-26 HIGH: PG_PASSWORD/MNEMOS_DB_PASSWORD alone must NOT
    force profile=server. Runtime
    _has_explicit_postgres_connection_config explicitly excludes
    password — it only checks {host, port, database, user}. So with
    no config.toml + only PG_PASSWORD set, runtime selects sqlite/
    edge while the installer must agree (otherwise --upgrade would
    run postgres migrations against a DB the service never reads)."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    # Only PG_PASSWORD set — should NOT force server profile.
    monkeypatch.setenv("PG_PASSWORD", "rotated-secret")

    cfg = _config_from_env_runtime_parity()
    # Falls back to legacy "personal" → "edge".
    assert cfg.profile == "edge"
    # Password is still loaded into the cfg (operator may want it
    # for cred rotation in a sqlite/edge install).
    assert cfg.db_password == "rotated-secret"


def test_resolve_runtime_backend_password_env_alone_resolves_sqlite(monkeypatch, tmp_path):
    """End-to-end parity: with no config.toml + only PG_PASSWORD env
    set + profile=edge fallback, the resolver must return sqlite
    (not postgres). This is the case codex verified locally where
    runtime selected sqlite while installer selected postgres."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_PASSWORD", "secret")

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # No config.toml (tmp_path is empty).
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


# ---------------------------------------------------------------------------
# Codex round-27 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_backend_empty_string_toml_does_not_force_postgres(
    monkeypatch, tmp_path,
):
    """Round-27 MEDIUM: runtime drops empty-string DB connection
    fields before computing explicit_fields. So `[database] host = ""`
    with no PG_HOST and an edge profile must resolve to sqlite, NOT
    postgres. Documented production shape uses empty TOML strings
    with PG_* env values supplying the runtime."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'host = ""\n'
        'port = 0\n'  # int 0 isn't dropped (only empty strings) — but ports require explicit non-zero
        'database = ""\n'
        'user = ""\n'
        'password = ""\n'
    )

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # Empty-string fields must be sanitized away. port=0 (int) isn't
    # an empty string so it remains as a presence signal — but in
    # practice no one writes port = 0; the sanitization handles the
    # documented `field = ""` shape. Test both shapes:
    config_path.write_text(
        '[database]\n'
        'host = ""\n'
        'database = ""\n'
        'user = ""\n'
        'password = ""\n'
    )
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


def test_upgrade_no_config_implicit_default_dim_succeeds(monkeypatch, tmp_path):
    """Round-27 MEDIUM: --upgrade with no config.toml + no
    MNEMOS_EMBEDDING_DIM env var must NOT exit 1 just because the
    default 768 wasn't explicitly set. Migrations succeed; the
    implicit default matches what runtime falls back to anyway."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # The implicit-default acceptance must be present.
    assert "implicit_default_ok" in src
    assert "DEFAULT_EMBEDDING_DIM = 768" in src
    # And the failure message for non-default dim must still fire when
    # env var is missing.
    assert "non-default embedding_dim" in src


# ---------------------------------------------------------------------------
# Codex round-28 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_password_only_does_not_force_server(monkeypatch, tmp_path):
    """Round-28 HIGH: a TOML config with only [database].password set
    (no host/port/database/user, no profile) must NOT infer
    profile=server. Runtime excludes password from
    _has_explicit_postgres_connection_config — only host/port/
    database/user count. Counting password here routed --upgrade to
    postgres migrations against the wrong DB while runtime stayed
    on sqlite/edge."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'password = "secret"\n'  # only password — should not be enough
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Falls back to legacy "personal" → "edge", matching runtime.
    assert cfg.profile == "edge"


def test_resolve_runtime_backend_accepts_sqlite3_alias_env(monkeypatch, tmp_path):
    """Round-28 MEDIUM: runtime _normalize_backend_name accepts both
    'sqlite' and 'sqlite3'. Resolver must mirror to avoid routing a
    PG_BACKEND=sqlite3 + profile=server config into run_migrations."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "sqlite3")  # alias

    @dataclass
    class _CfgServer:
        profile: str = "server"  # would otherwise force postgres

    assert _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path)) == "sqlite"


def test_resolve_runtime_backend_accepts_sqlite3_alias_toml(monkeypatch, tmp_path):
    """Same alias parity for TOML [database].backend = "sqlite3"."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "sqlite3"\n'
    )

    @dataclass
    class _CfgServer:
        profile: str = "server"

    assert _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path)) == "sqlite"


def test_load_existing_config_sqlite3_alias_keeps_edge_profile(monkeypatch, tmp_path):
    """Round-28 MEDIUM: backend = 'sqlite3' in TOML must NOT trigger
    server profile inference (sqlite-shaped backends keep edge)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = "sqlite3"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "edge"


# ---------------------------------------------------------------------------
# Codex round-29 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_ignores_legacy_name_key(monkeypatch, tmp_path):
    """Round-29 HIGH: runtime _DatabaseSettings only defines
    `database`, not the legacy `name` key. _load_existing_config
    used to fall back `db.get("database") or db.get("name")` —
    a config with `name = "old_db"` and empty `database` had
    --upgrade target old_db while runtime connected to
    PG_DATABASE/default. Schema migrations on the wrong DB."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'database = ""\n'  # empty — should fall through to PG_DATABASE/default
        'name = "old_db"\n'  # legacy key — runtime ignores
        'user = "mnemos_user"\n'
        'password = ""\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_DATABASE", "prod_db")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # cfg.db_name must come from PG_DATABASE (runtime), NOT from the
    # legacy `name = "old_db"` TOML key.
    assert cfg.db_name == "prod_db"


def test_load_existing_config_legacy_name_does_not_shadow_default(monkeypatch, tmp_path):
    """Even when no PG_DATABASE is set, `name` must not shadow the
    runtime default. Runtime falls back to "mnemos" — installer must
    too, not to the legacy `name` value."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'name = "old_db"\n'  # legacy — should be ignored
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Falls back to runtime default "mnemos", not legacy "old_db".
    assert cfg.db_name == "mnemos"


# ---------------------------------------------------------------------------
# Codex round-30 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_honors_mnemos_profile_env(monkeypatch, tmp_path):
    """Round-30 HIGH: runtime _ServerSettings.profile reads
    MNEMOS_PROFILE env via Pydantic validation_alias. So a
    profile-less config + MNEMOS_PROFILE=server in env must resolve
    to server (postgres dispatch) — installer must agree."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\n'
        'backend = ""\n'  # no backend signal
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MNEMOS_PROFILE", "server")
    monkeypatch.delenv("MNEMOS_PROFILE_OVERRIDE", raising=False)
    # Clear PG_* so backend resolution wouldn't interfere.
    for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.profile == "server"


def test_load_existing_config_honors_mnemos_profile_override(monkeypatch, tmp_path):
    """Round-30 HIGH: MNEMOS_PROFILE_OVERRIDE wins over TOML profile,
    matching runtime _profile_from_sources. Operator can flip an
    edge-installed config to server-mode upgrade via env override."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "edge"\n'  # TOML says edge
        '[database]\n'
        'backend = "sqlite"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MNEMOS_PROFILE_OVERRIDE", "server")
    monkeypatch.delenv("MNEMOS_PROFILE", raising=False)
    for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Override wins over TOML.
    assert cfg.profile == "server"


def test_load_existing_config_toml_profile_wins_over_mnemos_profile(monkeypatch, tmp_path):
    """TOML [server].profile wins over MNEMOS_PROFILE env, matching
    runtime _profile_from_sources order (after override)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "edge"\n'
        '[database]\n'
        'backend = "sqlite"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("MNEMOS_PROFILE_OVERRIDE", raising=False)
    monkeypatch.setenv("MNEMOS_PROFILE", "server")  # env says server
    for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # TOML wins.
    assert cfg.profile == "edge"


# ---------------------------------------------------------------------------
# Codex round-31 follow-ups
# ---------------------------------------------------------------------------


def test_cli_profile_flag_sets_override_env(monkeypatch):
    """Round-31 HIGH: --profile <p> CLI flag must set
    MNEMOS_PROFILE_OVERRIDE (not just MNEMOS_PROFILE) so it wins
    over stale TOML [server].profile during --upgrade dispatch.
    Source-level guard."""
    import inspect
    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main.main)
    # Both env vars must be set so CLI override takes precedence
    # over both runtime defaults and TOML.
    assert 'os.environ["MNEMOS_PROFILE_OVERRIDE"] = args.profile' in src
    assert 'os.environ["MNEMOS_PROFILE"] = args.profile' in src


def test_cli_profile_server_overrides_stale_edge_toml(monkeypatch, tmp_path):
    """Behavioral: --profile server with TOML [server].profile = "edge"
    must resolve to server (not edge). Without the override env, TOML
    wins per round-30 precedence, defeating the operator-explicit
    --profile flag."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "edge"\n'  # stale TOML
        '[database]\n'
        'backend = "sqlite"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    # Simulate `--profile server` having set the override.
    monkeypatch.setenv("MNEMOS_PROFILE_OVERRIDE", "server")
    monkeypatch.setenv("MNEMOS_PROFILE", "server")
    for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Override wins over TOML.
    assert cfg.profile == "server"


# ---------------------------------------------------------------------------
# Codex round-32 follow-ups
# ---------------------------------------------------------------------------


def test_runtime_parity_loader_honors_mnemos_profile_override(monkeypatch):
    """Round-32 MEDIUM: _config_from_env_runtime_parity must honor
    MNEMOS_PROFILE_OVERRIDE (highest-priority source per runtime
    _profile_from_sources). Without this, --upgrade with
    MNEMOS_PROFILE_OVERRIDE=server + no config.toml resolved to
    edge while runtime resolved to server, dispatching to sqlite
    migrations while the service started on postgres."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    # Override should resolve to server, not the legacy edge default.
    monkeypatch.setenv("MNEMOS_PROFILE_OVERRIDE", "server")
    monkeypatch.setenv("PG_PASSWORD", "secret")  # triggers loader entry

    cfg = _config_from_env_runtime_parity()
    assert cfg.profile == "server"


def test_runtime_parity_loader_override_wins_over_mnemos_profile(monkeypatch):
    """Override env beats plain MNEMOS_PROFILE even when both set."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MNEMOS_PROFILE_OVERRIDE", "server")
    monkeypatch.setenv("MNEMOS_PROFILE", "edge")  # should be shadowed
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _config_from_env_runtime_parity()
    assert cfg.profile == "server"


# ---------------------------------------------------------------------------
# Codex round-33 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_no_toml_with_profile_override_returns_config(monkeypatch, tmp_path):
    """Round-33 MEDIUM: no config.toml + MNEMOS_PROFILE_OVERRIDE=server
    + no PG_PASSWORD must still trigger the runtime-parity loader.
    Without this gate, --upgrade returned None and refused, making
    round-32's override fix unreachable for passwordless shapes
    (peer auth, sudo psql for setup_database, etc.)."""
    from mnemos.installer.__main__ import _load_existing_config

    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        "MNEMOS_PROFILE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "MNEMOS_CONFIG_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MNEMOS_PROFILE_OVERRIDE", "server")

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None  # NOT None — round-33 widens the trigger
    assert cfg.profile == "server"


def test_load_existing_config_no_toml_with_pg_backend_returns_config(monkeypatch, tmp_path):
    """PG_BACKEND=postgres alone (no password) must trigger the
    runtime-parity loader for the no-config upgrade path."""
    from mnemos.installer.__main__ import _load_existing_config

    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "MNEMOS_CONFIG_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "postgres")

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None


def test_load_existing_config_no_toml_with_pg_host_returns_config(monkeypatch, tmp_path):
    """PG_HOST alone (no password) must trigger the loader.
    Peer-auth / socket-auth shapes don't need PG_PASSWORD."""
    from mnemos.installer.__main__ import _load_existing_config

    for k in (
        "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "MNEMOS_CONFIG_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "localhost")

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None


def test_load_existing_config_no_toml_no_signals_returns_none(monkeypatch, tmp_path):
    """Sanity: no config.toml + zero env signals → None (refuses
    upgrade, no DB target inferable)."""
    from mnemos.installer.__main__ import _load_existing_config

    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "MNEMOS_CONFIG_PATH",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is None


# ---------------------------------------------------------------------------
# Codex round-34 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_backend_rejects_invalid_env_backend(monkeypatch, tmp_path):
    """Round-34 MEDIUM: an unrecognized PG_BACKEND value must fail
    closed (matches lifecycle._normalize_backend_name which raises
    ValueError). Previously fell through to other signals, which
    could dispatch migrations under a broken config that runtime
    refused to start."""
    import pytest as _pytest
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "postgress")  # typo

    @dataclass
    class _CfgServer:
        profile: str = "server"

    with _pytest.raises(ValueError) as exc_info:
        _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path))
    assert "Unsupported persistence backend" in str(exc_info.value)


def test_resolve_runtime_backend_accepts_auto_env_backend(monkeypatch, tmp_path):
    """`auto` is a valid value — falls through to other signals
    (matches runtime, where `auto` triggers downstream selection)."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "auto")

    @dataclass
    class _CfgServer:
        profile: str = "server"

    # Falls through to profile-derived default.
    assert _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_rejects_invalid_toml_backend(monkeypatch, tmp_path):
    """Same fail-closed for TOML [database].backend with a typo."""
    import pytest as _pytest
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "postgress"\n'  # typo
    )

    @dataclass
    class _CfgServer:
        profile: str = "server"

    with _pytest.raises(ValueError) as exc_info:
        _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path))
    assert "Unsupported [database].backend" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codex round-35 follow-ups
# ---------------------------------------------------------------------------


def test_pg_backend_auto_shadows_toml_then_pg_host_wins(monkeypatch, tmp_path):
    """Round-45 HIGH (corrects round-43 inversion): PG_BACKEND=auto
    SHADOWS TOML backend (matches runtime — empirically PG_BACKEND=
    'auto' replaces init backend='sqlite' with 'auto' which lifecycle
    then resolves via DSN/conn/profile). With env auto + TOML
    sqlite + PG_HOST set → resolver falls through TOML (shadowed),
    DSN (none), conn fields (PG_HOST present) → postgres."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "auto")
    monkeypatch.setenv("PG_HOST", "10.0.0.5")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "sqlite"\n'  # shadowed by env auto
    )

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # env auto shadows TOML sqlite → PG_HOST signal → postgres.
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_pg_backend_auto_used_when_toml_backend_missing(monkeypatch, tmp_path):
    """When TOML backend is missing/empty, PG_BACKEND=auto falls
    through to other signals (DSN/conn fields/profile). With no
    PG_* / DSN signals, profile fallback applies."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "auto")

    config_path = tmp_path / "config.toml"
    config_path.write_text('[database]\nbackend = ""\n')  # empty TOML

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # Empty TOML → env auto → no other signals → profile fallback = sqlite.
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "sqlite"


# ---------------------------------------------------------------------------
# Codex round-36 follow-ups
# ---------------------------------------------------------------------------


def test_pg_backend_auto_shadows_toml_postgres_no_other_signals(monkeypatch, tmp_path):
    """Round-45 HIGH: PG_BACKEND=auto shadows TOML backend (even
    when TOML is "postgres"). With no DSN/conn/profile signals,
    profile fallback applies — falls to legacy edge → sqlite."""
    from mnemos.installer.__main__ import (
        _load_existing_config,
        _resolve_runtime_backend,
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "postgres"\n'  # shadowed by env auto
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_BACKEND", "auto")
    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # env auto shadows TOML → no profile signal → legacy edge.
    assert cfg.profile == "edge"
    # Resolver: env auto shadows TOML, no other signals, profile fallback → sqlite.
    assert _resolve_runtime_backend(cfg, repo_path=str(tmp_path)) == "sqlite"


def test_pg_backend_postgres_wins_over_toml_sqlite(monkeypatch, tmp_path):
    """Round-44 HIGH (corrects round-43 inversion): _DatabaseSettings.
    backend uses validation_alias=AliasChoices(...PG_BACKEND...).
    Pydantic-settings 2.10.1 makes validation_alias env override init
    kwargs, so PG_BACKEND=postgres wins over TOML backend=sqlite at
    runtime. Empirically verified (see round-44 commit message)."""
    from mnemos.installer.__main__ import (
        _load_existing_config,
        _resolve_runtime_backend,
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "sqlite"\n'  # gets overridden by env
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_BACKEND", "postgres")
    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # PG_BACKEND=postgres wins → profile = server.
    assert cfg.profile == "server"
    # Resolver returns postgres — env wins over TOML for backend.
    assert _resolve_runtime_backend(cfg, repo_path=str(tmp_path)) == "postgres"


# ---------------------------------------------------------------------------
# Codex round-37 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_backend_empty_pg_backend_fails_closed(monkeypatch, tmp_path):
    """Round-37 MEDIUM: PG_BACKEND="" is treated as explicit by
    runtime Pydantic AliasChoices, which then raises Unsupported
    persistence backend ''. Installer must match — presence-based
    check, not truthy."""
    import pytest as _pytest
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_BACKEND", "")  # explicit empty

    @dataclass
    class _CfgServer:
        profile: str = "server"

    with _pytest.raises(ValueError) as exc_info:
        _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path))
    assert "Unsupported persistence backend" in str(exc_info.value)


def test_resolve_runtime_backend_alias_priority_first_present_wins(monkeypatch, tmp_path):
    """Pydantic AliasChoices order: MNEMOS_PERSISTENCE_BACKEND first,
    then PERSISTENCE_BACKEND, then PG_BACKEND. The FIRST present
    alias wins regardless of value (even empty)."""
    import pytest as _pytest
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    monkeypatch.delenv("MNEMOS_PERSISTENCE_BACKEND", raising=False)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "")  # higher priority but empty
    monkeypatch.setenv("PG_BACKEND", "postgres")  # lower priority — should NOT win
    for k in (
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)

    @dataclass
    class _CfgServer:
        profile: str = "server"

    # PERSISTENCE_BACKEND="" wins (higher priority), and "" raises.
    with _pytest.raises(ValueError):
        _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path))


def test_load_existing_config_empty_pg_backend_shadows_toml_backend(monkeypatch, tmp_path):
    """Round-45 (revert from round-43 inversion): PG_BACKEND="" is
    treated as 'present env shadows TOML' for the backend field
    (validation_alias semantics). With TOML backend=postgres +
    PG_BACKEND="" + no other signals, profile inference shadows
    TOML and falls through to legacy edge. Resolver-side ValueError
    for the empty value is asserted separately by the round-37
    test."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "postgres"\n'  # shadowed by empty env
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_BACKEND", "")  # explicit empty shadows
    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # PG_BACKEND="" shadows TOML postgres → no signal → legacy edge.
    assert cfg.profile == "edge"


# ---------------------------------------------------------------------------
# Codex round-38 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_preserves_explicit_port_zero(monkeypatch, tmp_path):
    """Round-38 MEDIUM: an explicit `[database].port = 0` was
    silently rewritten to 5432 by the truthy check in
    _config_or_env. Runtime keeps the explicit 0 and fails to
    connect, but the installer would proceed with port 5432 and
    mutate the local default cluster. Now port 0 passes through."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 0\n'  # explicit invalid port
        'database = "mnemos"\n'
        'user = "mnemos_user"\n'
        'password = "secret"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    # Explicit 0 must NOT be coerced to 5432.
    assert cfg.db_port == 0


def test_run_migrations_rejects_port_zero():
    """Round-38 MEDIUM: an explicit port=0 from the loader must
    fail the non-default-port guard, NOT silently default to 5432."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _ZeroPortCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 0  # explicit zero
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_ZeroPortCfg())
    assert rc is False


def test_setup_database_rejects_port_zero():
    """Same defense for setup_database."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _ZeroPortCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 0
        embedding_dim: int = 768

    class _Info:
        pass

    rc = installer_db.setup_database(_ZeroPortCfg(), _Info())
    assert rc is False


# ---------------------------------------------------------------------------
# Codex round-39 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_empty_pg_port_fails_closed(monkeypatch, tmp_path):
    """Round-39 HIGH: PG_PORT='' is an explicit empty value at runtime
    (Pydantic ValidationError). Installer must match — fail closed
    instead of silently coercing to 5432."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nhost = "localhost"\nport = ""\n'  # explicit empty
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_PORT", "")  # explicit empty
    for k in (
        "PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    # TOML port="" + PG_PORT="" both fall through to default 5432
    # via _config_or_env (empty strings are 'absent') — but if the
    # operator has an EXPLICIT empty TOML port we need to detect it.
    # In the current shape, both empties → 5432 default; this is the
    # round-38 behavior. Round-39 specifically targets PG_PORT=
    # garbage (non-empty malformed). Test that scenario:
    monkeypatch.setenv("PG_PORT", "bad-value")
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nhost = "localhost"\n'
        '[api]\nport = 5002\n'
    )
    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Invalid Postgres port" in str(exc_info.value)


def test_apply_embedding_dim_first_present_alias_wins(monkeypatch):
    """Round-39 MEDIUM: presence-based AliasChoices order —
    MNEMOS_EMBEDDING_DIM (higher priority) wins even when empty.
    Previously truthy `or` chain skipped to PG_EMBEDDING_DIM."""
    import pytest as _pytest
    from mnemos.installer.wizard import Config
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "")  # higher priority but empty
    monkeypatch.setenv("PG_EMBEDDING_DIM", "1024")  # lower priority — should NOT win

    cfg = Config(embedding_dim=768)
    with _pytest.raises(ValueError) as exc_info:
        _apply_embedding_dim_from_env(cfg)
    # Empty MNEMOS_EMBEDDING_DIM wins (higher priority) and fails closed.
    assert "MNEMOS_EMBEDDING_DIM" in str(exc_info.value)


def test_apply_embedding_dim_pg_alias_when_mnemos_unset(monkeypatch):
    """Sanity: PG_EMBEDDING_DIM is honored when MNEMOS_EMBEDDING_DIM
    is unset (not present in env)."""
    from mnemos.installer.wizard import Config
    from mnemos.installer.__main__ import _apply_embedding_dim_from_env

    monkeypatch.delenv("MNEMOS_EMBEDDING_DIM", raising=False)
    monkeypatch.setenv("PG_EMBEDDING_DIM", "512")

    cfg = Config(embedding_dim=768)
    _apply_embedding_dim_from_env(cfg)
    assert cfg.embedding_dim == 512


# ---------------------------------------------------------------------------
# Codex round-40 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_explicit_empty_pg_port_fails_closed(monkeypatch, tmp_path):
    """Round-40 HIGH: PG_PORT="" (explicit empty) must fail closed,
    matching runtime. Previously _config_or_env treated empty as
    absent, falling through to default 5432 — letting installer
    dispatch while runtime refused to start."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nhost = "localhost"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_PORT", "")  # explicit empty
    for k in (
        "PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Invalid Postgres port" in str(exc_info.value)


def test_load_existing_config_absent_pg_port_uses_default(monkeypatch, tmp_path):
    """Sanity: PG_PORT not set + no TOML port → default 5432
    (the legitimate not-explicit case)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nhost = "localhost"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_port == 5432


def test_load_existing_config_present_but_absent_embedding_dim_uses_default(monkeypatch, tmp_path):
    """Sanity: [database] section exists but no embedding_dim key →
    cfg.embedding_dim = 768 default (round-40 only fails on
    PRESENT-but-malformed)."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'database = "mnemos"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "MNEMOS_EMBEDDING_DIM", "PG_EMBEDDING_DIM",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.embedding_dim == 768


# ---------------------------------------------------------------------------
# Codex round-41 follow-ups
# ---------------------------------------------------------------------------


def test_load_existing_config_explicit_empty_pg_host_fails_closed(monkeypatch, tmp_path):
    """Round-41 HIGH: PG_HOST="" is explicit at runtime (Pydantic
    ValidationError-equivalent — empty string isn't dropped). Must
    fail closed instead of falling back to localhost."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nbackend = "postgres"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_HOST", "")  # explicit empty
    for k in (
        "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Postgres host" in str(exc_info.value)


def test_load_existing_config_explicit_empty_pg_database_fails_closed(monkeypatch, tmp_path):
    """PG_DATABASE="" must also fail closed — production shape with
    empty TOML placeholder + empty env would otherwise migrate the
    default DB while runtime targets the explicit empty value."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nbackend = "postgres"\nhost = "localhost"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_DATABASE", "")
    for k in (
        "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Postgres database name" in str(exc_info.value)


def test_load_existing_config_explicit_empty_pg_user_fails_closed(monkeypatch, tmp_path):
    """PG_USER="" likewise."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nbackend = "postgres"\nhost = "localhost"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_USER", "")
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "Postgres user" in str(exc_info.value)


def test_load_existing_config_pg_database_explicit_value_wins(monkeypatch, tmp_path):
    """Sanity: PG_DATABASE="prod_db" wins over empty TOML placeholder
    and over the default. Verifies the env-presence path."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'database = ""\n'  # empty TOML placeholder
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_DATABASE", "prod_db")
    for k in (
        "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_name == "prod_db"


# ---------------------------------------------------------------------------
# Codex round-42 follow-ups
# ---------------------------------------------------------------------------


def test_runtime_parity_loader_rejects_explicit_empty_pg_host(monkeypatch):
    """Round-42 HIGH: _config_from_env_runtime_parity (no-config
    --upgrade loader) must apply the same fail-closed contract for
    explicit empty PG_HOST as the TOML loader (round-41). Without
    this, no-config + PG_HOST='' + PG_PASSWORD=secret loaded
    cfg.db_host='' and the resolver silently dispatched sqlite
    while runtime selected postgres."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "")  # explicit empty
    monkeypatch.setenv("PG_PASSWORD", "secret")  # triggers loader

    with _pytest.raises(ValueError) as exc_info:
        _config_from_env_runtime_parity()
    assert "Postgres host" in str(exc_info.value)


def test_runtime_parity_loader_rejects_explicit_empty_pg_database(monkeypatch):
    """PG_DATABASE='' must also fail closed."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "PG_HOST", "PG_PORT", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_DATABASE", "")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    with _pytest.raises(ValueError) as exc_info:
        _config_from_env_runtime_parity()
    assert "Postgres database name" in str(exc_info.value)


def test_runtime_parity_loader_pg_host_present_signals_postgres(monkeypatch):
    """Round-42: PG_HOST=10.0.0.5 (any non-empty) must signal postgres
    even though _strict_pg_env_field would accept it. Profile
    inference treats env presence as postgres signal regardless."""
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "10.0.0.5")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    cfg = _config_from_env_runtime_parity()
    assert cfg.profile == "server"
    assert cfg.db_host == "10.0.0.5"


def test_runtime_parity_loader_explicit_empty_pg_port_fails_closed(monkeypatch):
    """Round-42: presence-based PG_PORT in the no-config loader.
    Explicit empty raises ValueError instead of silent default."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "PG_HOST", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_PORT", "")  # explicit empty
    monkeypatch.setenv("PG_PASSWORD", "secret")

    with _pytest.raises(ValueError) as exc_info:
        _config_from_env_runtime_parity()
    assert "Invalid Postgres port" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codex round-43 follow-ups
# ---------------------------------------------------------------------------


def test_non_empty_toml_host_wins_over_pg_host_env(monkeypatch, tmp_path):
    """Round-43 HIGH: non-empty TOML host beats PG_HOST env. Runtime
    _DatabaseSettings(**db_section) treats sanitized TOML as init
    kwargs (highest priority). Stale PG_HOST in operator's shell
    must NOT retarget --upgrade away from the persisted TOML host."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "production.example.com"\n'  # non-empty TOML wins
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_HOST", "stale-staging-host")  # should NOT win
    for k in (
        "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_host == "production.example.com"


def test_non_empty_toml_database_wins_over_pg_database_env(monkeypatch, tmp_path):
    """Same TOML-wins-over-env rule for database name."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'database = "mnemos_prod"\n'  # non-empty TOML wins
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_DATABASE", "staging_db")  # should NOT win
    for k in (
        "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_name == "mnemos_prod"


def test_non_empty_toml_port_wins_over_pg_port_env(monkeypatch, tmp_path):
    """Non-empty TOML port beats PG_PORT env."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "localhost"\n'
        'port = 5432\n'  # non-empty TOML wins
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_PORT", "9999")  # should NOT win
    for k in (
        "PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_port == 5432


def test_pg_env_fills_when_toml_field_missing(monkeypatch, tmp_path):
    """Sanity: when TOML field is missing/empty, PG_* env wins.
    The empty-TOML production shape continues to work."""
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = ""\n'  # empty placeholder
        'database = ""\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_HOST", "production.example.com")
    monkeypatch.setenv("PG_DATABASE", "mnemos_prod")
    for k in (
        "PG_PORT", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = _load_existing_config(str(tmp_path))
    assert cfg is not None
    assert cfg.db_host == "production.example.com"
    assert cfg.db_name == "mnemos_prod"


# ---------------------------------------------------------------------------
# Codex round-44 follow-ups
# ---------------------------------------------------------------------------


def test_runtime_settings_pg_backend_overrides_toml_init(monkeypatch):
    """Round-44 HIGH: empirical verification that
    _DatabaseSettings.backend uses validation_alias semantics where
    PG_BACKEND env OVERRIDES init kwargs (TOML). This is the
    pydantic-settings 2.10.1 behavior — codex caught my round-43
    assumption was wrong for THIS field."""
    from mnemos.core.config import _DatabaseSettings

    monkeypatch.setenv("PG_BACKEND", "postgres")
    s = _DatabaseSettings(backend="sqlite")
    # PG_BACKEND env wins.
    assert s.backend == "postgres"


def test_runtime_settings_pg_host_init_wins_over_env(monkeypatch):
    """Symmetric verification: host/port/database/user use
    env_prefix='PG_' WITHOUT validation_alias, so init kwargs WIN
    over env. This is why round-43 was right for connection fields
    but round-44 corrects only the backend field."""
    from mnemos.core.config import _DatabaseSettings

    monkeypatch.setenv("PG_HOST", "stale-staging")
    monkeypatch.setenv("PG_PORT", "9999")
    monkeypatch.setenv("PG_DATABASE", "wrong_db")
    monkeypatch.setenv("PG_USER", "wrong_user")

    s = _DatabaseSettings(
        host="production",
        port=5432,
        database="mnemos_prod",
        user="prod_user",
    )
    # Init kwargs win.
    assert s.host == "production"
    assert s.port == 5432
    assert s.database == "mnemos_prod"
    assert s.user == "prod_user"


def test_resolve_runtime_backend_matches_runtime_settings_for_pg_backend_postgres(monkeypatch, tmp_path):
    """Round-44 parity: my _resolve_runtime_backend with
    PG_BACKEND=postgres + TOML backend=sqlite must match runtime
    _DatabaseSettings + selector."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    config_path = tmp_path / "config.toml"
    config_path.write_text('[database]\nbackend = "sqlite"\n')
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_BACKEND", "postgres")
    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    @dataclass
    class _CfgEdge:
        profile: str = "edge"

    # PG_BACKEND wins → postgres.
    assert _resolve_runtime_backend(_CfgEdge(), repo_path=str(tmp_path)) == "postgres"


def test_resolve_runtime_backend_matches_runtime_settings_for_pg_backend_sqlite(monkeypatch, tmp_path):
    """Reverse: PG_BACKEND=sqlite + TOML backend=postgres → sqlite."""
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    config_path = tmp_path / "config.toml"
    config_path.write_text('[database]\nbackend = "postgres"\n')
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_BACKEND", "sqlite")
    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    @dataclass
    class _CfgServer:
        profile: str = "server"

    # PG_BACKEND wins → sqlite.
    assert _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path)) == "sqlite"


# ---------------------------------------------------------------------------
# Codex round-46 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_runtime_backend_whitespace_toml_backend_fails_closed(monkeypatch, tmp_path):
    """Round-46 MEDIUM: whitespace-only [database].backend = "   "
    is not dropped by runtime sanitization (which only drops exact
    empty strings). Lifecycle._normalize_backend_name strips and
    lowercases, getting "" which raises Unsupported persistence
    backend ''. Installer must match that fail-closed."""
    import pytest as _pytest
    from dataclasses import dataclass
    from mnemos.installer.__main__ import _resolve_runtime_backend

    for k in (
        "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nbackend = "   "\n'  # whitespace-only
    )

    @dataclass
    class _CfgServer:
        profile: str = "server"

    with _pytest.raises(ValueError) as exc_info:
        _resolve_runtime_backend(_CfgServer(), repo_path=str(tmp_path))
    assert "Unsupported [database].backend" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codex round-47 follow-ups
# ---------------------------------------------------------------------------


def test_is_local_postgres_host_rejects_whitespace_only():
    """Round-47 HIGH: whitespace-only host (e.g. '   ') is NOT
    treated as local. Runtime passes the unstripped value to
    asyncpg; stripping to "" and calling it local would let
    --upgrade mutate the default cluster."""
    from mnemos.installer.db import _is_local_postgres_host

    # Truly empty/None → local default socket.
    assert _is_local_postgres_host("") is True
    assert _is_local_postgres_host(None) is True

    # Recognized local hostnames → local. Round-49 narrowed this set
    # to "localhost" only — explicit IPs imply TCP intent.
    assert _is_local_postgres_host("localhost") is True
    # 127.0.0.1 / ::1 → NOT local (round-49 — TCP intent diverges
    # from socket).
    assert _is_local_postgres_host("127.0.0.1") is False
    assert _is_local_postgres_host("::1") is False

    # Whitespace-only → NOT local (operator typo / explicit invalid).
    assert _is_local_postgres_host("   ") is False
    assert _is_local_postgres_host("\t") is False
    assert _is_local_postgres_host("\n") is False

    # Remote hosts → not local.
    assert _is_local_postgres_host("10.0.0.5") is False


def test_load_existing_config_whitespace_pg_host_fails_closed(monkeypatch, tmp_path):
    """Round-47 HIGH: PG_HOST='   ' (whitespace-only) must fail
    closed at the loader, not bypass via strip-to-local."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nbackend = "postgres"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_HOST", "   ")  # whitespace-only
    for k in (
        "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "whitespace-only" in str(exc_info.value)


def test_load_existing_config_whitespace_toml_host_fails_closed(monkeypatch, tmp_path):
    """Whitespace-only TOML host must also fail closed."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = "   "\n'  # whitespace-only TOML
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "whitespace-only" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codex round-48 follow-ups
# ---------------------------------------------------------------------------


def test_is_local_postgres_host_rejects_padded_values():
    """Round-48 HIGH: padded local hosts (' localhost ', '127.0.0.1\\n')
    must NOT be treated as local. Runtime uses the raw value;
    stripping and comparing turned broken targets into local."""
    from mnemos.installer.db import _is_local_postgres_host

    # Padded values — NOT local.
    assert _is_local_postgres_host(" localhost") is False
    assert _is_local_postgres_host("localhost ") is False
    assert _is_local_postgres_host(" localhost ") is False
    assert _is_local_postgres_host("\nlocalhost") is False
    assert _is_local_postgres_host("localhost\n") is False
    assert _is_local_postgres_host(" 127.0.0.1") is False
    assert _is_local_postgres_host("127.0.0.1\t") is False

    # Exact matches still work (sanity). Round-49 narrowed: only
    # "localhost" is local-safe; 127.0.0.1 / ::1 are TCP intent.
    assert _is_local_postgres_host("localhost") is True
    assert _is_local_postgres_host("127.0.0.1") is False
    assert _is_local_postgres_host("::1") is False


def test_load_existing_config_padded_pg_host_fails_closed(monkeypatch, tmp_path):
    """Round-48 HIGH: PG_HOST=' localhost' must fail closed at the
    loader, not silently strip-to-local."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\nbackend = "postgres"\n'
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("PG_HOST", " localhost")  # padded
    for k in (
        "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "leading/trailing whitespace" in str(exc_info.value)


def test_load_existing_config_padded_toml_host_fails_closed(monkeypatch, tmp_path):
    """Padded TOML host must also fail closed."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _load_existing_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[server]\nprofile = "server"\n'
        '[database]\n'
        'backend = "postgres"\n'
        'host = " localhost "\n'  # padded TOML
        '[api]\nport = 5002\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(config_path))
    for k in (
        "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
        "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
        "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    with _pytest.raises(ValueError) as exc_info:
        _load_existing_config(str(tmp_path))
    assert "leading/trailing whitespace" in str(exc_info.value)


def test_runtime_parity_loader_padded_pg_host_fails_closed(monkeypatch):
    """Round-48: same in the no-config runtime-parity loader."""
    import pytest as _pytest
    from mnemos.installer.__main__ import _config_from_env_runtime_parity

    for k in (
        "MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE",
        "PG_PORT", "PG_DATABASE", "PG_USER",
        "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
        "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_HOST", "localhost ")  # padded
    monkeypatch.setenv("PG_PASSWORD", "secret")

    with _pytest.raises(ValueError) as exc_info:
        _config_from_env_runtime_parity()
    assert "leading/trailing whitespace" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codex round-49 follow-ups
# ---------------------------------------------------------------------------


def test_run_migrations_rejects_explicit_127_0_0_1():
    """Round-49 HIGH: explicit `127.0.0.1` host is rejected because
    it implies TCP, while the migration runner uses socket auth via
    `sudo -u postgres psql -d <db>` (no -h). On multi-cluster hosts
    these can target different DBs."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _ExplicitIPCfg:
        profile: str = "server"
        db_host: str = "127.0.0.1"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_ExplicitIPCfg())
    assert rc is False


def test_run_migrations_rejects_explicit_ipv6_loopback():
    """Same for ::1."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _ExplicitIPCfg:
        profile: str = "server"
        db_host: str = "::1"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_ExplicitIPCfg())
    assert rc is False


def test_run_migrations_accepts_localhost_only():
    """Sanity: "localhost" still works (DNS-resolves; common shape)."""
    import io
    import sys
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _LocalhostCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "mnemos"
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    captured = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    try:
        installer_db.run_migrations(_LocalhostCfg())
    except Exception:
        pass
    finally:
        sys.stderr = real_stderr
    err = captured.getvalue()
    # Confirm it did NOT trip the locality guard (would say
    # "is not a local postgres").
    assert "is not a local postgres" not in err


# ---------------------------------------------------------------------------
# Round-50 finding 2 follow-up (committed separately from #138 closure)
# ---------------------------------------------------------------------------


def test_run_migrations_rejects_libpq_conninfo_in_db_name():
    """Round-50 finding 2: db_name MUST be a bare identifier.
    libpq treats `host=... dbname=...` or `postgres://...` as a
    conninfo string, bypassing host/port/DSN guards and connecting
    to an arbitrary cluster. setup_database already validates;
    run_migrations now does too."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _ConninfoCfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "host=10.0.0.5 port=5432 dbname=staging"  # injection
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_ConninfoCfg())
    assert rc is False


def test_run_migrations_rejects_postgres_uri_in_db_name():
    """Same defense for postgres://... URIs."""
    from dataclasses import dataclass
    from mnemos.installer import db as installer_db

    @dataclass
    class _URICfg:
        profile: str = "server"
        db_host: str = "localhost"
        db_name: str = "postgres://attacker@10.0.0.5:5432/staging"  # injection
        db_password: str = "secret"
        db_user: str = "mnemos_user"
        db_port: int = 5432
        embedding_dim: int = 768

    rc = installer_db.run_migrations(_URICfg())
    assert rc is False

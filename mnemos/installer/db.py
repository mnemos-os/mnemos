"""Database setup and migration operations for MNEMOS mnemos.installer."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from .wizard import Config


def _validate_identifier(value: str, name: str = "identifier") -> str:
    """Reject anything that is not a safe SQL identifier (letters/digits/underscore/hyphen)."""
    import re
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\-]{0,62}', value):
        raise ValueError(
            f"Unsafe SQL {name} '{value}': must match [A-Za-z_][A-Za-z0-9_-]{{0,62}}"
        )
    return value


def _run(
    cmd: list[str],
    timeout: int = 60,
    input_text: str | None = None,
    env: dict | None = None,
    input: str | None = None,
) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    import os as _os
    merged_env = _os.environ.copy()
    if env:
        merged_env.update(env)
    stdin_text = input if input is not None else input_text
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
            env=merged_env,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def _profile_uses_sqlite(profile: str) -> bool:
    return profile in {"edge", "dev"}


def _psql_superuser(sql: str, dbname: str = "postgres", timeout: int = 30) -> tuple[int, str, str]:
    """Run SQL as the postgres superuser via sudo.

    Uses `-v ON_ERROR_STOP=1` so psql exits non-zero on the first SQL
    error. Without this flag psql continues past non-fatal errors and
    can exit 0 even when DDL failed — letting run_migrations() report
    'OK' on partial schema drift. Codex round-20 HIGH.
    """
    return _run(
        ["sudo", "-u", "postgres", "psql", "-d", dbname, "-c", sql,
         "--no-password", "-A", "-t", "-v", "ON_ERROR_STOP=1"],
        timeout=timeout,
    )


def _psql_superuser_file(filepath: str, dbname: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run a SQL file as postgres superuser.

    Uses `-v ON_ERROR_STOP=1` so psql exits non-zero on the first SQL
    error in the file. Without this, a CREATE/ALTER failure mid-file
    would emit a warning and continue, leaving partial schema while
    psql reports success. Codex round-20 HIGH.
    """
    with open(filepath, encoding="utf-8") as f:
        sql = f.read()
    return _run(
        ["sudo", "-u", "postgres", "psql", "-d", dbname, "-f", "-",
         "--no-password", "-v", "ON_ERROR_STOP=1"],
        timeout=timeout,
        input=sql,
    )


def verify_connection(config: Config) -> bool:
    """Verify we can connect to the target database."""
    if _profile_uses_sqlite(config.profile):
        return Path(config.sqlite_path).expanduser().exists()

    env = os.environ.copy()
    env["PGPASSWORD"] = config.db_password

    pg_env = {"PGPASSWORD": config.db_password}
    rc, out, err = _run(
        [
            "psql",
            "-h", config.db_host,
            "-p", str(config.db_port),
            "-U", config.db_user,
            "-d", config.db_name,
            "-c", "SELECT 1",
            "-A", "-t",
        ],
        timeout=10,
        env=pg_env,
    )
    if rc == 0:
        return True

    # Fallback: try via asyncpg if available
    try:
        import asyncio

        import asyncpg

        async def _check() -> bool:
            conn = await asyncpg.connect(
                host=config.db_host,
                port=config.db_port,
                user=config.db_user,
                password=config.db_password,
                database=config.db_name,
                timeout=10,
            )
            await conn.close()
            return True

        return asyncio.run(_check())
    except Exception:
        pass

    return False


def setup_sqlite_database(config: Config) -> bool:
    """Create and migrate the SQLite database through the SQLite backend.

    Uses ``config.embedding_dim`` (collected at install time from
    MNEMOS_EMBEDDING_DIM, or carried over from a prior install) to size
    the vec0 virtual table. The same value gets persisted to config.toml
    and the systemd env file so subsequent service starts see the same
    dim — without that, CREATE VIRTUAL TABLE IF NOT EXISTS would be a
    no-op and the wrong-dim runtime would silently degrade.
    """
    import asyncio
    from types import SimpleNamespace

    from mnemos.persistence.sqlite import SqliteBackend

    db_path = Path(config.sqlite_path).expanduser()
    embedding_dim = getattr(config, "embedding_dim", 768)
    print(
        f"[db] Initializing SQLite database at {db_path} "
        f"(embedding dim: {embedding_dim})..."
    )

    settings_shim = SimpleNamespace(database=SimpleNamespace(embedding_dim=embedding_dim))

    async def _setup() -> bool:
        backend = SqliteBackend(db_path, settings_shim)
        try:
            await backend.open()
            print(f"[db] SQLite database ready. sqlite-vec loaded: {backend.vec_loaded}")
            return True
        finally:
            await backend.close()

    try:
        return asyncio.run(_setup())
    except Exception as exc:
        print(f"[db] ERROR initializing SQLite database: {exc}", file=sys.stderr)
        return False


# #187: removed `pgvector_installed` — defined but never called.
# Verified across mnemos/+tests/+docs/+scripts/+systemd/+console_
# scripts. Installer __main__ imports run_migrations / setup_*
# / create_api_key / verify_connection from this module but NOT
# pgvector_installed. The pgvector extension is installed
# unconditionally during `setup_database` via the `CREATE
# EXTENSION IF NOT EXISTS vector` SQL — no probing variant
# needed.


def setup_database(config: Config, info) -> bool:
    """Create the database user, database, and extensions. Idempotent."""

    # The setup helpers (_psql_superuser) shell out to `sudo -u postgres
    # psql -d <dbname>` with no `-h`/`-p`/`-U`/PGPASSWORD. That auth shape
    # only works against the LOCAL postgres on default port 5432. A
    # config that points at a remote db_host (e.g. cfg.db_host=10.0.0.5)
    # would otherwise mutate the LOCAL postgres of the same dbname
    # before run_migrations()'s remote-rejection guard ever runs,
    # leaving stray users/dbs/extensions on the wrong cluster.
    #
    # Round-18: guards are UNCONDITIONAL on profile. Profile is a
    # deployment-shape signal, not a DB-backend signal — operators
    # can set MNEMOS_PROFILE=edge while [database].backend="postgres"
    # with a remote host, and the legacy "personal" canonicalizes to
    # edge while the underlying config is postgres. Profile-gating
    # let those configs through; always check.
    if not _is_local_postgres_host(
        getattr(config, "db_host", "localhost")
    ):
        print(
            f"[db] ERROR cfg.db_host = {config.db_host!r} is not a local "
            f"postgres. setup_database() uses sudo -u postgres psql with "
            f"no -h/-p; it cannot create users/databases/extensions on a "
            f"remote host without silently mutating the LOCAL postgres "
            f"of the same name. Run --install on the host that owns the "
            f"DB (where psql peer-auth works), or use a DSN-aware setup "
            f"tool. Refusing to proceed before any local mutations.",
            file=sys.stderr,
        )
        return False

    # Same defense for non-default port on localhost. The psql helpers
    # don't pass -p, so localhost:5433 silently targets the default
    # local cluster on 5432. Unconditional on profile (round-18).
    # Round-38 MEDIUM: don't fold falsy values (e.g. 0) into the
    # default 5432 — that would let an explicit port=0 bypass the
    # non-default-port guard. Treat None as absent → default; treat
    # any other value as explicit so the guard fires correctly.
    raw_port = getattr(config, "db_port", None)
    db_port_int = 5432 if raw_port is None else int(raw_port)
    if db_port_int != 5432:
        print(
            f"[db] ERROR cfg.db_port = {db_port_int} but the installer "
            f"only supports the default Postgres port (5432). The setup "
            f"helpers (sudo -u postgres psql -d <db>) don't pass -p, so "
            f"a non-default port silently mutates the local 5432 cluster "
            f"instead. Use a DSN-aware setup tool, or run Postgres on "
            f"5432 for the installer-driven setup path.",
            file=sys.stderr,
        )
        return False

    try:
        _validate_identifier(config.db_user, "db_user")
        _validate_identifier(config.db_name, "db_name")
    except ValueError as exc:
        print(f"[db] ERROR: {exc}", file=sys.stderr)
        return False

    print(f"[db] Setting up database '{config.db_name}' as user '{config.db_user}'...")

    # 1. Create user (idempotent via DO block)
    escaped_pw = config.db_password.replace("'", "''")
    create_user_sql = (
        f"DO $$ BEGIN "
        f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{config.db_user}') THEN "
        f"    CREATE USER {config.db_user} WITH PASSWORD '{escaped_pw}'; "
        f"  ELSE "
        f"    ALTER USER {config.db_user} WITH PASSWORD '{escaped_pw}'; "
        f"  END IF; "
        f"END $$;"
    )
    rc, out, err = _psql_superuser(create_user_sql)
    if rc != 0:
        print(f"[db] ERROR creating user: {err}", file=sys.stderr)
        return False
    print(f"[db] User '{config.db_user}' ready.")

    # 2. Create database (idempotent)
    rc, out, _ = _psql_superuser(
        f"SELECT 1 FROM pg_database WHERE datname='{config.db_name}'"
    )
    if out.strip() != "1":
        rc, out, err = _psql_superuser(
            f"CREATE DATABASE {config.db_name} OWNER {config.db_user}"
        )
        if rc != 0:
            print(f"[db] ERROR creating database: {err}", file=sys.stderr)
            return False
        print(f"[db] Database '{config.db_name}' created.")
    else:
        print(f"[db] Database '{config.db_name}' already exists.")

    # 3. Grant privileges
    rc, _, err = _psql_superuser(
        f"GRANT ALL PRIVILEGES ON DATABASE {config.db_name} TO {config.db_user}",
        dbname="postgres",
    )
    if rc != 0:
        print(f"[db] WARNING granting privileges: {err}", file=sys.stderr)

    # 4. Create vector extension
    rc, _, err = _psql_superuser(
        "CREATE EXTENSION IF NOT EXISTS vector",
        dbname=config.db_name,
    )
    if rc != 0:
        print(f"[db] WARNING: pgvector extension not available: {err}", file=sys.stderr)
        print("[db] Install with: apt install postgresql-16-pgvector (or your pg version)")
    else:
        print("[db] pgvector extension ready.")

    # 5. Create pgcrypto extension
    rc, _, err = _psql_superuser(
        "CREATE EXTENSION IF NOT EXISTS pgcrypto",
        dbname=config.db_name,
    )
    if rc != 0:
        print(f"[db] WARNING: pgcrypto extension not available: {err}", file=sys.stderr)
    else:
        print("[db] pgcrypto extension ready.")

    # 6. Grant schema privileges to app user
    rc, _, err = _psql_superuser(
        f"GRANT ALL ON SCHEMA public TO {config.db_user}",
        dbname=config.db_name,
    )
    if rc != 0:
        print(f"[db] WARNING: schema grant failed: {err}", file=sys.stderr)

    return True


def _is_local_postgres_host(host: str) -> bool:
    """psql via `sudo -u postgres` only authenticates on the local socket
    when no `-h` is passed. The helpers in this module don't pass
    -h/-p/-U/PGPASSWORD. Anything else must be rejected before
    migrations run.

    Round-47 HIGH: whitespace-only values (e.g. "   ") are NOT
    treated as local. Runtime passes the unstripped host through to
    asyncpg, so a whitespace host is an explicit (broken) target —
    NOT the default socket.

    Round-48 HIGH: also reject any host with leading/trailing
    whitespace. Same wrong-cluster mutation class.

    Round-49 HIGH: reject explicit `127.0.0.1` / `::1`. Runtime
    connects via asyncpg using TCP for these values, while
    `sudo -u postgres psql -d <db>` (no -h) connects via the local
    SOCKET. On a host running multiple Postgres clusters where
    socket and TCP listeners hit different instances, the
    installer's socket-based psql can mutate one DB while runtime
    targets a different one over TCP. Until the migration runner
    is TCP/DSN-aware, only empty/None and "localhost" are safe —
    explicit IPs imply a TCP intent the installer cannot honor.
    """
    if host is None or host == "":
        return True  # truly unset → local default socket
    # Round-48: reject any value that differs from its stripped form.
    if host != host.strip():
        return False  # padded — explicit invalid, treat as non-local
    h = host.lower()
    if h == "":
        return False  # whitespace-only (handled by != strip above too)
    # Round-49: only "localhost" is acceptable for the socket-based
    # migration runner. 127.0.0.1 / ::1 imply TCP intent.
    return h == "localhost"


def run_migrations(config: Config) -> bool:
    """Run SQL migration files in order. Idempotent.

    Keep this list as the canonical migration order. Every new
    migration must be appended to the end, not inserted in the middle.
    Order is load-bearing —
    v2 migrations expect v1 tables, v3.1 expects v3, v3.1.2
    expects v3.1.
    """
    # Round-50 finding 2: validate db_name as a bare identifier BEFORE
    # any psql invocation. Without this, libpq treats values like
    # `host=127.0.0.1 port=5433 dbname=mnemos` or `postgres://...` as
    # full connection strings — bypassing the host/port/DSN/url
    # refusals and connecting to an arbitrary cluster. setup_database
    # has had this check since the original slice; run_migrations
    # was missing it.
    try:
        _validate_identifier(config.db_name, "db_name")
    except ValueError as exc:
        print(f"[db] ERROR: {exc}", file=sys.stderr)
        return False

    # The migration helpers (`_psql_superuser`, `_psql_superuser_file`)
    # use `sudo -u postgres psql -d <dbname>` and never pass
    # -h/-p/-U/PGPASSWORD. That works for a local install but would
    # silently target a LOCAL postgres of the same name when
    # cfg.db_host is remote — running migrations against the wrong DB
    # while the running service points elsewhere. Refuse early.
    #
    # Round-18: guards are UNCONDITIONAL on profile. --upgrade calls
    # run_migrations(cfg) without checking profile, and operators can
    # set MNEMOS_PROFILE=edge while [database].backend="postgres" with
    # a remote host. Profile-gating let those configs reach
    # _psql_superuser_file and silently mutate the local cluster.
    # Always check.
    if not _is_local_postgres_host(
        getattr(config, "db_host", "localhost")
    ):
        print(
            f"[db] ERROR cfg.db_host = {config.db_host!r} is not a local "
            f"postgres. The installer's migration runner only authenticates "
            f"via local socket / 127.0.0.1; it cannot run migrations against "
            f"a remote host. Run --upgrade on the host that owns the DB, or "
            f"use a DSN-aware migration tool. Refusing to proceed before "
            f"any migration/config patching.",
            file=sys.stderr,
        )
        return False

    # Same defense for non-default port on localhost. Unconditional
    # on profile (round-18).
    # Round-38 MEDIUM: don't fold falsy values (e.g. 0) into the
    # default 5432 — that would let an explicit port=0 bypass the
    # non-default-port guard. Treat None as absent → default; treat
    # any other value as explicit so the guard fires correctly.
    raw_port = getattr(config, "db_port", None)
    db_port_int = 5432 if raw_port is None else int(raw_port)
    if db_port_int != 5432:
        print(
            f"[db] ERROR cfg.db_port = {db_port_int} but the installer "
            f"only supports the default Postgres port (5432). The "
            f"migration helpers (sudo -u postgres psql -d <db>) don't "
            f"pass -p, so a non-default port would silently target the "
            f"default-port cluster on the same host. Use a DSN-aware "
            f"migration tool, or run Postgres on 5432 for the "
            f"installer-driven upgrade path.",
            file=sys.stderr,
        )
        return False

    repo_path = Path(__file__).resolve().parents[2]
    migration_files = [
        repo_path / "db" / "migrations.sql",
        repo_path / "db" / "migrations_v1_multiuser.sql",
        repo_path / "db" / "migrations_v2_versioning.sql",
        repo_path / "db" / "migrations_v2_sessions.sql",
        repo_path / "db" / "migrations_model_registry.sql",
        repo_path / "db" / "migrations_v3_dag.sql",
        repo_path / "db" / "migrations_v3_graeae_unified.sql",
        repo_path / "db" / "migrations_v3_webhooks.sql",
        repo_path / "db" / "migrations_v3_oauth.sql",
        repo_path / "db" / "migrations_v3_federation.sql",
        repo_path / "db" / "migrations_v3_ownership.sql",
        repo_path / "db" / "migrations_v3_1_compression.sql",
        repo_path / "db" / "migrations_v3_1_versioning_fix.sql",
        repo_path / "db" / "migrations_v3_1_2_kg_tenancy.sql",
        repo_path / "db" / "migrations_v3_1_2_audit_log_columns.sql",
        repo_path / "db" / "migrations_v3_2_user_namespace.sql",
        repo_path / "db" / "migrations_v3_2_entities_namespace.sql",
        repo_path / "db" / "migrations_v3_2_2_version_snapshot_new_values.sql",
        repo_path / "db" / "migrations_v3_3_morpheus.sql",
        repo_path / "db" / "migrations_v3_3_morpheus_namespace.sql",
        repo_path / "db" / "migrations_v3_3_recall_tracking.sql",
        repo_path / "db" / "migrations_charon_trigger_guard.sql",
        repo_path / "db" / "migrations_v3_4_federation_compat.sql",
        repo_path / "db" / "migrations_v3_5_trigger_same_memory_parent.sql",
        repo_path / "db" / "migrations_v3_5_rls_group_select_unix_bits.sql",
        repo_path / "db" / "migrations_v3_5_webhook_retry_terminal_state.sql",
        repo_path / "db" / "migrations_v3_5_webhook_attempt_lease.sql",
        repo_path / "db" / "migrations_v3_5_webhook_writer_revision.sql",
        repo_path / "db" / "migrations_v3_5_webhook_status_updated_at.sql",
        repo_path / "db" / "migrations_v3_5_webhook_superseded_marker.sql",
        repo_path / "db" / "migrations_v3_5_webhook_attempt_unique.sql",
        repo_path / "db" / "migrations_v3_5_webhook_succeeded_unique.sql",
        repo_path / "db" / "migrations_v3_5_webhook_succeeded_terminal_trigger.sql",
        repo_path / "db" / "migrations_v3_5_entities_namespace_unique.sql",
        repo_path / "db" / "migrations_v3_5_state_journal_namespace.sql",
        repo_path / "db" / "migrations_v3_5_session_compression_ratio_drop.sql",
        repo_path / "db" / "migrations_v3_5_session_compression_legacy_drop.sql",
        repo_path / "db" / "migrations_v3_5_sessions_consultations_namespace.sql",
        repo_path / "db" / "migrations_v4_2_users_username.sql",
        repo_path / "db" / "migrations_v4_2_compression_candidates_nullable_tokens.sql",
        repo_path / "db" / "migrations_v4_2_state_value_text.sql",
        repo_path / "db" / "migrations_v4_2_document_import_chunk_idempotency.sql",
        repo_path / "db" / "migrations_v4_2_deletion_requests.sql",
        repo_path / "db" / "migrations_v4_2_deletion_requests_blank_namespace_cleanup.sql",
        repo_path / "db" / "migrations_v4_2_deletion_requests_soft_delete_columns.sql",
        repo_path / "db" / "migrations_v4_2_deletion_requests_sweep_verifying.sql",
        repo_path / "db" / "migrations_v4_2_compression_dag.sql",
        repo_path / "db" / "migrations_v4_2_morpheus_consolidate.sql",
        repo_path / "db" / "migrations_v4_2_morpheus_extract.sql",
        repo_path / "db" / "migrations_v4_2_persephone.sql",
        repo_path / "db" / "migrations_v4_2_pantheon_routing_audit.sql",
        repo_path / "db" / "migrations_v5_0_consolidated_at.sql",
        repo_path / "db" / "migrations_v5_0_morpheus_extract_run_memories.sql",
        repo_path / "db" / "migrations_v5_0_2_artemis_dedup.sql",
        repo_path / "db" / "migrations_v5_0_3_timestamp_tz_upgrade.sql",
        repo_path / "db" / "migrations_v5_1_0_deletion_log.sql",
        repo_path / "db" / "migrations_v5_2_0_nats_outbox_idempotency.sql",
        repo_path / "db" / "migrations_v5_2_2_fts_gin_index.sql",
        repo_path / "db" / "migrations_v5_3_3_deletion_log_export_index.sql",
        repo_path / "db" / "migrations_v5_3_4_mcp_audit_log.sql",
        repo_path / "db" / "migrations_v5_3_5_model_registry_capabilities_gin.sql",
    ]

    print("[db] Running migrations...")

    # Round-21: fail FAST on the first migration error. The migration
    # order is documented as load-bearing (v2 expects v1 tables, v3.1
    # expects v3, etc.) so applying later migrations on top of a
    # failed early one creates ambiguous schema drift that the
    # operator can't cleanly recover from. Combined with round-20's
    # ON_ERROR_STOP=1 inside psql, the first error returns False
    # immediately — no partial-apply window.
    for mig_path in migration_files:
        if not mig_path.exists():
            print(f"[db] Skipping {mig_path.name} (not found)")
            continue

        print(f"[db] Applying {mig_path.name}...", end=" ")
        rc, out, err = _psql_superuser_file(str(mig_path), config.db_name)
        if rc != 0:
            print("FAILED")
            print(
                f"[db] ERROR in {mig_path.name}:\n{err}\n"
                f"[db] Aborting before applying later migrations — "
                f"order is load-bearing. Inspect the failed migration, "
                f"fix the root cause (schema drift, locked table, "
                f"missing extension), and re-run --upgrade. The DB "
                f"may be partially migrated through "
                f"{mig_path.name.replace('.sql', '')}.",
                file=sys.stderr,
            )
            return False
        print("OK")

    # Postgres parallel of the SQLite embed-dim story: db/migrations.sql
    # creates `embedding vector(768)` which pgvector freezes at column-type
    # creation. We always reconcile against the configured dim — even when
    # it's the 768 default — because an existing DB might already be at a
    # non-default dim from a prior install. The helper is idempotent: it
    # short-circuits OK when the column type already matches the target,
    # and refuses safely on populated mismatch. The previous `!= 768` guard
    # silently downgraded an existing 512-D install when the operator
    # switched config back to the default model.
    embedding_dim = getattr(config, "embedding_dim", 768)
    return _alter_postgres_embedding_dim(config, embedding_dim)


def _alter_postgres_embedding_dim(config: Config, embedding_dim: int) -> bool:
    """Re-size `memories.embedding` to vector(<dim>) when MNEMOS_EMBEDDING_DIM != 768.

    Idempotent: queries the actual stored dim via `format_type(atttypid, atttypmod)`
    and short-circuits if the column already matches. Safe on a fresh install
    (table has no rows; ALTER is cheap). On an existing install with rows at
    a different dim, pgvector refuses the implicit cast and the operator must
    drop+rebuild — same posture as the SQLite path. We detect that explicitly
    and refuse with a postgres-correct migration instruction set.

    Postgres-specific constraints:
    - pgvector ivfflat index supports up to 2000 dimensions. The existing
      `idx_memories_embedding` is ivfflat, so we cap the supported dim at 2000.
      Larger dims need a different ANN strategy (halfvec / no ANN index)
      which isn't wired into the migration baseline yet.
    """
    try:
        # ivfflat ceiling. Larger dims would need halfvec/no-ANN index strategy
        # (not currently wired into the migration baseline). Fail closed —
        # don't accept a config the schema can't actually serve.
        if not 1 <= embedding_dim <= 2000:
            print(
                f"[db] ERROR MNEMOS_EMBEDDING_DIM={embedding_dim} out of supported "
                "range [1, 2000] for pgvector ivfflat index (used by the "
                "baseline idx_memories_embedding). Larger dims need a different "
                "ANN strategy (halfvec / no-ANN index) that is not currently "
                "wired into migrations. Refusing to proceed — accepting this "
                "would persist embedding_dim into config while leaving the DB "
                "schema at vector(768), causing runtime cosine comparisons to "
                "fail.",
                file=sys.stderr,
            )
            return False

        # Check the actual stored column type first — idempotency. If it
        # already matches, this is a no-op even when rows exist.
        rc, out, err = _psql_superuser(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'memories'::regclass "
            "AND attname = 'embedding';",
            dbname=config.db_name,
        )
        current_type = ""
        if rc == 0 and out.strip():
            for line in out.strip().splitlines():
                line = line.strip()
                if line.startswith("vector("):
                    current_type = line
                    break
        target_type = f"vector({embedding_dim})"
        if current_type == target_type:
            print(f"[db] memories.embedding already at {target_type}; nothing to do")
            return True

        # Type mismatch. The COUNT and ALTER must run under one ACCESS
        # EXCLUSIVE lock — separate sessions race against any concurrent
        # writer that inserts a row between our COUNT and our ALTER, and
        # the ALTER ... USING NULL would silently clear that row.
        #
        # Wrap the whole sequence in a plpgsql DO block: lock first, count
        # second, ALTER iff zero, RAISE EXCEPTION otherwise. The exception
        # rolls back the lock + the (not-yet-issued) ALTER. The fail-closed
        # behavior is enforced by postgres itself rather than by Python
        # parsing of psql output.
        plpgsql = (
            "DO $$\n"
            "DECLARE\n"
            "  cnt INTEGER;\n"
            "BEGIN\n"
            "  LOCK TABLE memories IN ACCESS EXCLUSIVE MODE;\n"
            "  SELECT COUNT(*) INTO cnt FROM memories WHERE embedding IS NOT NULL;\n"
            "  IF cnt > 0 THEN\n"
            "    RAISE EXCEPTION 'MNEMOS_EMBED_DIM_REFUSE: memories.embedding has % "
            "non-null rows at the existing dim; cannot ALTER to {target_type} via "
            "USING NULL without destroying data. Run the documented BEGIN; "
            "UPDATE … SET embedding=NULL; ALTER … TYPE {target_type} USING NULL; "
            "COMMIT; recovery on a quiesced DB instead.', cnt;\n"
            "  END IF;\n"
            "  ALTER TABLE memories ALTER COLUMN embedding TYPE {target_type} USING NULL;\n"
            "END;\n"
            "$$;"
        ).format(target_type=target_type)

        print(f"[db] Resizing memories.embedding from {current_type or 'baseline'} to {target_type}...", end=" ")
        rc, _, err = _psql_superuser(plpgsql, dbname=config.db_name)
        if rc != 0:
            print("FAILED")
            err_text = err.strip()
            # plpgsql RAISE surfaces our marker so we can render the operator-
            # facing recovery instructions instead of a raw psql error.
            if "MNEMOS_EMBED_DIM_REFUSE" in err_text:
                # Try to extract the row count from the error.
                import re as _re
                m = _re.search(r"has (\d+) non-null rows", err_text)
                rows_str = m.group(1) if m else "non-zero"
                print(
                    f"[db] ERROR memories.embedding is {current_type or 'unknown'} "
                    f"with {rows_str} non-null rows. Cannot re-size to "
                    f"{target_type} without re-embedding. To migrate: stop "
                    f"this service, then run "
                    f"`psql -d {config.db_name} -c \"BEGIN; "
                    f"UPDATE memories SET embedding=NULL; "
                    f"ALTER TABLE memories ALTER COLUMN embedding TYPE "
                    f"{target_type} USING NULL; COMMIT;\"`, restart the "
                    f"service, and re-embed all memories.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[db] ERROR resizing embedding column: {err_text or '(no detail)'}",
                    file=sys.stderr,
                )
            return False
        print("OK")
        return True
    except Exception as exc:
        print(f"[db] ERROR in _alter_postgres_embedding_dim: {exc}", file=sys.stderr)
        return False


def create_api_key(config: Config) -> str | None:
    """Create an initial API key. Returns the raw key string, or None on failure.

    Connection-driver preference order (codex round-2 finding,
    2026-05-01):

      1. asyncpg (Apache-2.0; default mnemos dep). Honors
         host/port/user/password from the operator's Config —
         works against remote Postgres + auth-enabled installs.
      2. psycopg / psycopg2 (LGPL-3.0; OPTIONAL — operators install
         separately if they want this path). Same connection shape
         as asyncpg but sync API.
      3. psql CLI (operator's external binary; not bundled with
         mnemos). Assumes local sudo passwordless access — last
         resort, often fails on remote / managed Postgres.

    Pre-2026-05-01 the chain was psycopg → psycopg2 → psql, and the
    psycopg drop in v4.2.0a12 left auth-enabled remote installs with
    no working path. asyncpg ships in default mnemos and works for
    every install shape.
    """
    raw_key = "mnemos_" + secrets.token_hex(32)
    import hashlib

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    # 1. asyncpg path (default; works against any Postgres + auth)
    try:
        import asyncio

        import asyncpg as _asyncpg

        async def _create() -> None:
            conn = await _asyncpg.connect(
                host=config.db_host,
                port=int(config.db_port) if config.db_port else None,
                database=config.db_name,
                user=config.db_user,
                password=config.db_password,
            )
            try:
                # Schema per db/migrations_v1_multiuser.sql:
                # api_keys(id, user_id, key_hash, key_prefix, label,
                #          created_at, last_used, revoked)
                await conn.execute(
                    """
                    INSERT INTO api_keys (user_id, key_hash, key_prefix, label)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (key_hash) DO NOTHING
                    """,
                    "default",
                    key_hash,
                    raw_key[:8],
                    "installer-generated",
                )
            finally:
                await conn.close()

        asyncio.run(_create())
        print("[db] API key created via asyncpg.")
        return raw_key
    except Exception as _exc:
        print(f"[db] asyncpg create_api_key failed: {_exc}", file=sys.stderr)

    # 2. psycopg path (LGPL — only fires when operator has it
    # installed separately; not pulled by default mnemos).
    try:
        import psycopg

        conn_str = (
            f"host={config.db_host} port={config.db_port} "
            f"dbname={config.db_name} user={config.db_user} "
            f"password={config.db_password}"
        )
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                # Schema per db/migrations_v1_multiuser.sql:
                # api_keys(id, user_id, key_hash, key_prefix, label, created_at, last_used, revoked)
                # user_id references users(id) — the 'default' root user
                # is seeded by the same migration.
                cur.execute(
                    """
                    INSERT INTO api_keys (user_id, key_hash, key_prefix, label)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key_hash) DO NOTHING
                    """,
                    ("default", key_hash, raw_key[:8], "installer-generated"),
                )
            conn.commit()
        print("[db] API key created via psycopg.")
        return raw_key
    except Exception as _exc:
        print(f"[db] psycopg create_api_key failed: {_exc}", file=sys.stderr)

    # 3. psycopg2 path (LGPL — same posture as psycopg above).
    try:
        import psycopg2

        conn = psycopg2.connect(
            host=config.db_host,
            port=config.db_port,
            dbname=config.db_name,
            user=config.db_user,
            password=config.db_password,
        )
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO api_keys (user_id, key_hash, key_prefix, label)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (key_hash) DO NOTHING
            """,
            ("default", key_hash, raw_key[:8], "installer-generated"),
        )
        conn.commit()
        cur.close()
        conn.close()
        print("[db] API key created via psycopg2.")
        return raw_key
    except Exception as _exc:
        print(f"[db] psycopg2 create_api_key failed: {_exc}", file=sys.stderr)

    # 4. psql CLI fallback — operator's external binary, assumes
    # local sudo passwordless access to a postgres role. Last
    # resort; remote / managed Postgres deployments fall through
    # to None at the bottom of the function.
    import re as _re

    if not _re.fullmatch(r'[0-9a-f]{64}', key_hash):
        print("[db] ERROR: unexpected key_hash format", file=sys.stderr)
        return None
    # raw_key[:8] is the display prefix. We reject anything in it that
    # isn't alphanumeric or `-`/`_` since we're interpolating into a SQL
    # string (the psycopg paths parameterize; this psql-CLI fallback
    # does not).
    key_prefix = raw_key[:8]
    if not _re.fullmatch(r'[A-Za-z0-9_-]{1,8}', key_prefix):
        print("[db] ERROR: unexpected key_prefix format", file=sys.stderr)
        return None
    sql = (
        f"INSERT INTO api_keys (user_id, key_hash, key_prefix, label) "
        f"VALUES ('default', '{key_hash}', '{key_prefix}', 'installer-generated') "
        f"ON CONFLICT (key_hash) DO NOTHING;"
    )
    rc, _, err = _psql_superuser(sql, dbname=config.db_name)
    if rc == 0:
        print("[db] API key created via psql CLI.")
        return raw_key

    # Check if api_keys table even exists — may not be needed for personal profile
    rc2, out2, _ = _psql_superuser(
        "SELECT to_regclass('public.api_keys')", dbname=config.db_name
    )
    if "api_keys" not in out2:
        print("[db] No api_keys table found — skipping key creation (personal profile).")
        return None

    print(f"[db] WARNING: Could not create API key: {err}", file=sys.stderr)
    return None

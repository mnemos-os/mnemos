"""Database setup and migration operations for MNEMOS installer."""

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
    input_text: str = None,
    env: dict = None,
) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    import os as _os
    merged_env = _os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=merged_env,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def _psql_superuser(sql: str, dbname: str = "postgres", timeout: int = 30) -> tuple[int, str, str]:
    """Run SQL as the postgres superuser via sudo."""
    return _run(
        ["sudo", "-u", "postgres", "psql", "-d", dbname, "-c", sql,
         "--no-password", "-A", "-t"],
        timeout=timeout,
    )


def _psql_superuser_file(filepath: str, dbname: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run a SQL file as postgres superuser."""
    return _run(
        ["sudo", "-u", "postgres", "psql", "-d", dbname, "-f", filepath,
         "--no-password"],
        timeout=timeout,
    )


def verify_connection(config: Config) -> bool:
    """Verify we can connect to the target database."""
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
        import asyncpg
        import asyncio

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


def pgvector_installed(config: Config) -> bool:
    """Check if pgvector extension is installed in the target database."""
    env = os.environ.copy()
    env["PGPASSWORD"] = config.db_password
    pg_env = {"PGPASSWORD": config.db_password}
    rc, out, _ = _run(
        [
            "psql",
            "-h", config.db_host,
            "-p", str(config.db_port),
            "-U", config.db_user,
            "-d", config.db_name,
            "-c", "SELECT 1 FROM pg_extension WHERE extname='vector'",
            "-A", "-t",
        ],
        timeout=10,
        env=pg_env,
    )
    return rc == 0 and out.strip() == "1"


def setup_database(config: Config, info) -> bool:
    """Create the database user, database, and extensions. Idempotent."""

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


def run_migrations(config: Config) -> bool:
    """Run SQL migration files in order. Idempotent.

    Keep this list in sync with `install.py::main()`. Both are
    ordered; every new migration must be appended to the end of BOTH
    lists, not inserted in the middle. Order is load-bearing —
    v2 migrations expect v1 tables, v3.1 expects v3, v3.1.2
    expects v3.1.
    """
    repo_path = Path(__file__).parent.parent
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
    ]

    print("[db] Running migrations...")
    success = True

    for mig_path in migration_files:
        if not mig_path.exists():
            print(f"[db] Skipping {mig_path.name} (not found)")
            continue

        print(f"[db] Applying {mig_path.name}...", end=" ")
        rc, out, err = _psql_superuser_file(str(mig_path), config.db_name)
        if rc != 0:
            print("FAILED")
            print(f"[db] ERROR in {mig_path.name}:\n{err}", file=sys.stderr)
            success = False
        else:
            print("OK")

    return success


def create_api_key(config: Config) -> str | None:
    """Create an initial API key. Returns the raw key string, or None on failure."""
    raw_key = "mnemos_" + secrets.token_hex(32)

    # Try via psycopg first
    try:
        import psycopg
        import hashlib

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

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

    # Fallback: try psycopg2
    try:
        import psycopg2
        import hashlib

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
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

    # Fallback: psql CLI — key_hash is a hex digest (safe for interpolation)
    import hashlib
    import re as _re

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
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

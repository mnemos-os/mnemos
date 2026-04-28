#!/usr/bin/env python3
"""MNEMOS interactive installer — configures deployment profile and runs migration."""
import hashlib
import os
import secrets
import subprocess
import sys
import tomllib


# ── Helpers ───────────────────────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    if default:
        answer = input(f"{question} [{default}]: ").strip()
        return answer if answer else default
    return input(f"{question}: ").strip()


def choose(question: str, options: list[str], default: str = None) -> str:
    print(f"\n{question}")
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")
    while True:
        raw = input("Choice [1]: ").strip() or "1"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def write_toml_section(f, name: str, data: dict, indent: int = 0) -> None:
    """Simple TOML writer for flat and one-level-nested dicts."""
    prefix = "  " * indent
    f.write(f"\n[{name}]\n")
    for k, v in data.items():
        if isinstance(v, bool):
            f.write(f"{prefix}{k} = {'true' if v else 'false'}\n")
        elif isinstance(v, int):
            f.write(f"{prefix}{k} = {v}\n")
        elif isinstance(v, str):
            f.write(f'{prefix}{k} = "{v}"\n')


def append_config(path: str, sections: dict) -> None:
    """Append new TOML sections to config.toml (or create if absent)."""
    # Load existing to check which sections already exist
    existing = load_config(path) if os.path.exists(path) else {}
    with open(path, "a") as f:
        for section_name, section_data in sections.items():
            if section_name in existing:
                print(f"  [SKIP] [{section_name}] already in config.toml")
                continue
            write_toml_section(f, section_name, section_data)
            print(f"  [ADD]  [{section_name}] written to config.toml")


def run_migration(pg_user: str, db_name: str, migration_file: str) -> bool:
    """Run the v1 migration SQL as the postgres superuser."""
    cmd = ["sudo", "-u", "postgres", "psql", "-d", db_name, "-f", migration_file]
    print(f"\nRunning: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] Migration failed:\n{result.stderr}")
        return False
    print("[OK] Migration complete")
    return True


def enable_rls(db_name: str) -> bool:
    """Enable Row Level Security on the memories table (team/enterprise only)."""
    sql = (
        "ALTER TABLE memories ENABLE ROW LEVEL SECURITY; "
        "ALTER TABLE memories FORCE ROW LEVEL SECURITY;"
    )
    cmd = ["sudo", "-u", "postgres", "psql", "-d", db_name, "-c", sql]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] RLS enable failed:\n{result.stderr}")
        return False
    print("[OK] Row Level Security enabled")
    return True


def create_root_api_key(db_name: str) -> str | None:
    """Insert a root API key for the default user and return the raw key.

    Uses psycopg parameterized queries to prevent SQL injection.
    Falls back to the hex-safe psql approach if psycopg is not available.
    """
    raw_key = secrets.token_hex(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]

    try:
        import psycopg  # type: ignore
        conn_str = f"dbname={db_name} user=postgres"
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_keys (user_id, key_hash, key_prefix, label) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    ("default", key_hash, key_prefix, "root-install"),
                )
            conn.commit()
        return raw_key
    except ImportError:
        pass

    # Fallback: psql CLI — key_hash and key_prefix are hex-only (no SQL special chars)
    sql = (
        f"INSERT INTO api_keys (user_id, key_hash, key_prefix, label) "
        f"VALUES ('default', '{key_hash}', '{key_prefix}', 'root-install') "
        f"ON CONFLICT DO NOTHING;"
    )
    cmd = ["sudo", "-u", "postgres", "psql", "-d", db_name, "-c", sql]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] Failed to create root API key:\n{result.stderr}")
        return None
    return raw_key


# ── Main flow ─────────────────────────────────────────────────────────────────

def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.toml")
    # Keep this list in sync with `installer/db.py::run_migrations`.
    # Both are ordered; every new migration must be appended to the
    # end of BOTH lists, not inserted in the middle. Order is
    # load-bearing — v2 migrations expect v1 tables, v3.1 expects v3,
    # v3.1.2 expects v3.1.
    migration_files = [
        os.path.join(script_dir, "db", "migrations.sql"),
        os.path.join(script_dir, "db", "migrations_v1_multiuser.sql"),
        os.path.join(script_dir, "db", "migrations_v2_versioning.sql"),
        os.path.join(script_dir, "db", "migrations_v2_sessions.sql"),
        os.path.join(script_dir, "db", "migrations_model_registry.sql"),
        os.path.join(script_dir, "db", "migrations_v3_dag.sql"),
        os.path.join(script_dir, "db", "migrations_v3_graeae_unified.sql"),
        os.path.join(script_dir, "db", "migrations_v3_webhooks.sql"),
        os.path.join(script_dir, "db", "migrations_v3_oauth.sql"),
        os.path.join(script_dir, "db", "migrations_v3_federation.sql"),
        os.path.join(script_dir, "db", "migrations_v3_ownership.sql"),
        os.path.join(script_dir, "db", "migrations_v3_1_compression.sql"),
        os.path.join(script_dir, "db", "migrations_v3_1_versioning_fix.sql"),
        os.path.join(script_dir, "db", "migrations_v3_1_2_kg_tenancy.sql"),
        os.path.join(script_dir, "db", "migrations_v3_1_2_audit_log_columns.sql"),
        os.path.join(script_dir, "db", "migrations_v3_2_user_namespace.sql"),
        os.path.join(script_dir, "db", "migrations_v3_2_entities_namespace.sql"),
        os.path.join(script_dir, "db", "migrations_v3_2_2_version_snapshot_new_values.sql"),
        os.path.join(script_dir, "db", "migrations_v3_3_morpheus.sql"),
        os.path.join(script_dir, "db", "migrations_v3_3_morpheus_namespace.sql"),
        os.path.join(script_dir, "db", "migrations_v3_3_recall_tracking.sql"),
        os.path.join(script_dir, "db", "migrations_charon_trigger_guard.sql"),
        os.path.join(script_dir, "db", "migrations_v3_4_federation_compat.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_trigger_same_memory_parent.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_rls_group_select_unix_bits.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_retry_terminal_state.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_attempt_lease.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_writer_revision.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_status_updated_at.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_superseded_marker.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_attempt_unique.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_succeeded_unique.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_webhook_succeeded_terminal_trigger.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_entities_namespace_unique.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_state_journal_namespace.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_session_compression_ratio_drop.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_session_compression_legacy_drop.sql"),
        os.path.join(script_dir, "db", "migrations_v3_5_sessions_consultations_namespace.sql"),
    ]

    print("=" * 60)
    print("  MNEMOS Installer")
    print("=" * 60)

    # 1. Choose deployment profile
    profile = choose(
        "Select deployment profile:",
        ["personal", "team", "enterprise"],
        default="personal",
    )
    print(f"\nProfile: {profile}")

    # 2. Collect database config
    print("\n--- Database ---")
    db_name = prompt("Database name", "mnemos")
    db_user = prompt("Database user", "mnemos_user")

    # 3. Collect service config
    print("\n--- Service ---")
    listen_port = prompt("Listen port", "5002")

    # Note: GRAEAE URL and Ollama host are configured in config.toml
    # Edit config.toml directly after installation (see config.toml.example)

    # 4. Determine auth/RLS settings
    auth_enabled = profile in ("team", "enterprise")
    rls_enabled = profile in ("team", "enterprise")

    # 5. Build config sections
    new_sections = {
        "deployment": {
            "profile": profile,
        },
        "auth": {
            "enabled": auth_enabled,
            "personal_user_id": "default",
            "default_namespace": "default",
            "mode": "bearer",
        },
        "multiuser": {
            "rls_enabled": rls_enabled,
            "namespaces_enabled": profile != "personal",
            "max_keys_per_user": 10,
        },
    }

    print("\n--- Writing config.toml ---")
    if not os.path.exists(config_path):
        print("  [NOTE] config.toml not found. Copy config.toml.example first:")
        print("         cp config.toml.example config.toml")
        print("         Then re-run this installer.")
        sys.exit(1)
    append_config(config_path, new_sections)

    # 6. Run migrations in order
    print("\n--- Database migrations ---")
    for migration_path in migration_files:
        if not os.path.exists(migration_path):
            print(f"[ERROR] Migration file not found: {migration_path}")
            sys.exit(1)
        if not run_migration(db_user, db_name, migration_path):
            sys.exit(1)

    # 7. Team/Enterprise: enable RLS + create root key
    if rls_enabled:
        print("\n--- Enabling Row Level Security ---")
        if not enable_rls(db_name):
            sys.exit(1)

    if auth_enabled:
        print("\n--- Creating root API key ---")
        raw_key = create_root_api_key(db_name)
        if raw_key:
            print("\n" + "=" * 60)
            print("  ROOT API KEY (save this — it will not be shown again)")
            print("=" * 60)
            print(f"  {raw_key}")
            print("=" * 60)
        else:
            print("[WARN] Could not create root API key; create one manually via admin API")

    # 8. Done
    print(f"""
Installation complete.

Start MNEMOS:
  python api_server.py          # port {listen_port}

Start GRAEAE (optional):
  python graeae/server.py       # port 5001

Health check:
  curl http://localhost:{listen_port}/health
""")


if __name__ == "__main__":
    main()

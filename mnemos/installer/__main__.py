"""
MNEMOS Installer — entry point.

Usage:
    python -m mnemos.installer [--agent] [--wizard] [--unattended] [--upgrade] [--check]

Options:
    --agent       LLM-guided installation (default)
    --wizard      Traditional interactive wizard
    --unattended  Non-interactive; reads config from environment variables
    --upgrade     Re-run migrations only (skip DB/service setup)
    --check       Environment check only, no changes

Environment variables for --unattended:
    MNEMOS_PROFILE, MNEMOS_DB_HOST, MNEMOS_DB_NAME, MNEMOS_DB_USER,
    MNEMOS_DB_PASSWORD, MNEMOS_LISTEN_PORT, MNEMOS_SERVICE_USER
"""

from __future__ import annotations

import argparse
import os
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m mnemos.installer",
        description="MNEMOS Memory System Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--agent",
        action="store_true",
        help="LLM-guided installation (default)",
    )
    mode.add_argument(
        "--wizard",
        action="store_true",
        help="Traditional interactive wizard",
    )
    mode.add_argument(
        "--unattended",
        action="store_true",
        help="Non-interactive; reads config from environment variables",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Re-run migrations only (skip DB/service setup)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Environment check only; no changes made",
    )
    return parser.parse_args()


def _config_from_env() -> "Config":
    """Build a Config from environment variables for unattended installs."""
    from .wizard import Config

    cfg = Config()
    cfg.profile = os.environ.get("MNEMOS_PROFILE", "personal")
    cfg.db_host = os.environ.get("MNEMOS_DB_HOST", "localhost")
    cfg.db_port = int(os.environ.get("MNEMOS_DB_PORT", "5432"))
    cfg.db_name = os.environ.get("MNEMOS_DB_NAME", "mnemos")
    cfg.db_user = os.environ.get("MNEMOS_DB_USER", "mnemos_user")
    cfg.db_password = os.environ.get("MNEMOS_DB_PASSWORD", "")
    cfg.listen_port = int(os.environ.get("MNEMOS_LISTEN_PORT", "5002"))
    cfg.service_user = os.environ.get("MNEMOS_SERVICE_USER", "mnemos")
    cfg.auth_enabled = cfg.profile in ("team", "enterprise")
    cfg.rls_enabled = cfg.profile == "enterprise"
    cfg.create_new_db = os.environ.get("MNEMOS_CREATE_DB", "true").lower() == "true"
    cfg.install_docling = os.environ.get("MNEMOS_INSTALL_DOCLING", "true").lower() == "true"
    cfg.create_service = os.environ.get("MNEMOS_CREATE_SERVICE", "true").lower() == "true"
    cfg.inference_embed_host = os.environ.get(
        "INFERENCE_EMBED_HOST", "http://localhost:11434"
    )

    # Provider keys from env
    providers = ["openai", "anthropic", "xai", "groq", "perplexity", "gemini", "nvidia", "together"]
    for p in providers:
        key = os.environ.get(f"{p.upper()}_API_KEY", "")
        if key:
            cfg.graeae_providers[p] = key

    if not cfg.db_password:
        print(
            "ERROR: MNEMOS_DB_PASSWORD is required for unattended install.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cfg



def _write_config_toml(cfg, repo_path: str) -> None:
    """Write (or update) config.toml with installer-collected values.

    Reads config.toml.example as the template, patches the [database] section
    with actual credentials, and writes to config.toml.  If config.toml already
    exists its [database] block is updated in-place.
    """
    import re
    config_path = os.path.join(repo_path, "config.toml")
    example_path = os.path.join(repo_path, "config.toml.example")

    # Start from example if config.toml doesn't exist yet
    if not os.path.exists(config_path) and os.path.exists(example_path):
        import shutil
        shutil.copy(example_path, config_path)

    if not os.path.exists(config_path):
        # No example either — write a minimal config
        content = (
            "[database]\n"
            f'host = "{cfg.db_host}"\n'
            f"port = {cfg.db_port}\n"
            f'database = "{cfg.db_name}"\n'
            f'user = "{cfg.db_user}"\n'
            f'password = "{cfg.db_password}"\n'
            "\n[api]\n"
            f"port = {cfg.listen_port}\n"
        )
        # Write with restricted permissions (contains DB password)
        import tempfile as _tf
        dir_ = os.path.dirname(config_path) or "."
        fd, tmp_path = _tf.mkstemp(dir=dir_, suffix=".toml.tmp")
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp_path, config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        print(f"[installer] Created {config_path}")
        return

    content = open(config_path).read()

    def _set(section_active, key, value):
        """Replace key = ... inside the active [database] section."""
        nonlocal content
        pattern = rf'((?:^|\n)\[{section_active}\][^\[]*?)({re.escape(key)}\s*=\s*[^\n]*)'
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace("", "\\")
            quoted = f'"{escaped}"'
        else:
            quoted = str(value)
        replacement = rf'\1{key} = {quoted}'
        content, n = re.subn(pattern, replacement, content, flags=re.DOTALL)
        if n == 0:
            # Key absent — append to end of section (simplistic)
            if isinstance(value, str):
                _esc2 = value.replace("\\", "\\\\").replace("", "\\")
                quoted = f'"{_esc2}"'
            else:
                quoted = str(value)
            content = re.sub(
                rf'(\[{section_active}\])',
                rf'\1\n{key} = {quoted}',
                content,
            )

    _set("database", "host", cfg.db_host)
    _set("database", "port", cfg.db_port)
    _set("database", "database", cfg.db_name)
    _set("database", "user", cfg.db_user)
    _set("database", "password", cfg.db_password)
    _set("api", "port", cfg.listen_port)

    import tempfile as _tf
    _dir = os.path.dirname(config_path) or "."
    _fd, _tmp = _tf.mkstemp(dir=_dir, suffix=".toml.tmp")
    try:
        os.chmod(_tmp, 0o600)
        with os.fdopen(_fd, "w") as f:
            f.write(content)
        os.replace(_tmp, config_path)
    except Exception:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise
    print(f"[installer] Updated {config_path}")


def _print_completion(cfg: "Config", api_key: str | None, repo_path: str) -> None:
    """Print the installation completion summary."""
    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{GREEN}=== MNEMOS Installation Complete ==={RESET}\n")
    print(f"  Endpoint:  http://{cfg.db_host if cfg.db_host != 'localhost' else 'localhost'}:{cfg.listen_port}")
    print(f"  Health:    curl http://localhost:{cfg.listen_port}/health")
    if api_key:
        print(f"  API key:   {api_key}")
    if sys.platform != "darwin":
        print("  Logs:      journalctl -u mnemos -f")
    else:
        print("  Logs:      tail -f ~/Library/Logs/mnemos.log")
    print(f"  Config:    {repo_path}/config.toml")
    print()


def main() -> int:
    args = _parse_args()

    # ------------------------------------------------------------------ #
    # Step 1: Always detect environment
    # ------------------------------------------------------------------ #
    from .detect import detect, print_summary

    print("Detecting environment...")
    info = detect()
    print_summary(info)

    # ------------------------------------------------------------------ #
    # Step 2: Python version gate
    # ------------------------------------------------------------------ #
    if not info.python_ok:
        ver = ".".join(str(x) for x in info.python_version)
        print(
            f"ERROR: Python {ver} is too old. MNEMOS requires Python >= 3.11.",
            file=sys.stderr,
        )
        return 1

    # ------------------------------------------------------------------ #
    # Step 3: --check exits here
    # ------------------------------------------------------------------ #
    if args.check:
        print("Environment check complete.")
        return 0

    # Determine repo path (directory containing this package)
    import pathlib
    repo_path = str(pathlib.Path(__file__).resolve().parents[2])

    # ------------------------------------------------------------------ #
    # Step 4: --upgrade = migrations only
    # ------------------------------------------------------------------ #
    if args.upgrade:
        from .db import run_migrations

        # Load existing config from environment or config.toml
        cfg = _load_existing_config(repo_path)
        if cfg is None:
            print(
                "ERROR: --upgrade requires existing config. "
                "Set MNEMOS_DB_* env vars or ensure config.toml exists.",
                file=sys.stderr,
            )
            return 1

        print("Running database migrations...")
        ok = run_migrations(cfg)
        if ok:
            print("Migrations complete.")
            return 0
        else:
            print("Some migrations failed.", file=sys.stderr)
            return 1

    # ------------------------------------------------------------------ #
    # Step 5: Obtain config
    # ------------------------------------------------------------------ #
    cfg = None

    if args.unattended:
        print("Running in unattended mode (reading config from environment)...")
        cfg = _config_from_env()

    elif args.wizard:
        from .wizard import run_wizard
        cfg = run_wizard(info)

    else:
        # Default: --agent (try LLM-guided, fall back to wizard)
        try:
            from .agent import run_agent
            cfg = run_agent(info)
        except (ImportError, ModuleNotFoundError):
            print("[installer] agent module not available — falling back to wizard.")
            from .wizard import run_wizard
            cfg = run_wizard(info)
        except Exception as exc:
            print(f"[installer] Agent error ({exc}) — falling back to wizard.")
            from .wizard import run_wizard
            cfg = run_wizard(info)

    if cfg is None:
        print("ERROR: No configuration obtained.", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------ #
    # Step 6: Create virtual environment
    # ------------------------------------------------------------------ #
    from .venv_setup import create_venv, install_docling, install_requirements

    print("\n[installer] Setting up virtual environment...")
    try:
        venv_path = create_venv(repo_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------ #
    # Step 7: Install requirements
    # ------------------------------------------------------------------ #
    print("\n[installer] Installing Python dependencies...")
    ok = install_requirements(venv_path)
    if not ok:
        print("WARNING: Some dependencies failed to install.", file=sys.stderr)

    if cfg.install_docling:
        print("\n[installer] Installing docling...")
        install_docling(venv_path)

    # ------------------------------------------------------------------ #
    # Step 8: Setup database
    # ------------------------------------------------------------------ #
    from .db import create_api_key, run_migrations, setup_database, verify_connection

    if cfg.create_new_db:
        print("\n[installer] Setting up database...")
        ok = setup_database(cfg, info)
        if not ok:
            print("ERROR: Database setup failed.", file=sys.stderr)
            return 1

    print("\n[installer] Running migrations...")
    ok = run_migrations(cfg)
    if not ok:
        print("WARNING: Some migrations failed.", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # Step 8b: Write config.toml
    # ------------------------------------------------------------------ #
    print("\n[installer] Writing config.toml...")
    _write_config_toml(cfg, repo_path)

    # ------------------------------------------------------------------ #
    # Step 9: Create API key
    # ------------------------------------------------------------------ #
    api_key = None
    if cfg.auth_enabled:
        print("\n[installer] Creating API key...")
        api_key = create_api_key(cfg)

    # ------------------------------------------------------------------ #
    # Step 10: Install service (optional)
    # ------------------------------------------------------------------ #
    if cfg.create_service:
        from .service import (
            create_service_user,
            enable_service,
            install_launchd,
            install_systemd,
            start_service,
        )

        print("\n[installer] Setting up system service...")

        if cfg.service_user == "mnemos":
            create_service_user(cfg.service_user)

        service_name = "mnemos"
        if sys.platform == "darwin":
            ok = install_launchd(cfg, repo_path)
            if ok:
                if not enable_service(f"ai.{service_name}"):
                    print("[service] WARNING: service enable failed.", file=sys.stderr)
                if not start_service(f"ai.{service_name}"):
                    print("[service] WARNING: service start failed.", file=sys.stderr)
        else:
            if info.systemd:
                ok = install_systemd(cfg, repo_path)
                if ok:
                    if not enable_service(service_name):
                        print("[service] WARNING: service enable failed.", file=sys.stderr)
                    if not start_service(service_name):
                        print("[service] WARNING: service start failed.", file=sys.stderr)
            else:
                print("[service] No supported init system — service not installed.")

    # ------------------------------------------------------------------ #
    # Step 11: Verify connection
    # ------------------------------------------------------------------ #
    print("\n[installer] Verifying database connection...")
    if verify_connection(cfg):
        print("[installer] Database connection: OK")
    else:
        print("[installer] WARNING: Could not verify database connection.")

    # ------------------------------------------------------------------ #
    # Final summary
    # ------------------------------------------------------------------ #
    _print_completion(cfg, api_key, repo_path)
    return 0


def _load_existing_config(repo_path: str):
    """Try to load existing config from environment variables or config.toml."""
    import os

    from .wizard import Config

    # Try env vars
    if os.environ.get("MNEMOS_DB_PASSWORD"):
        return _config_from_env()

    # Try config.toml
    config_path = os.path.join(repo_path, "config.toml")
    if os.path.exists(config_path):
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                return None

        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)

        cfg = Config()
        db = data.get("database", {})
        cfg.db_host = db.get("host", "localhost")
        cfg.db_port = db.get("port", 5432)
        # Key is "database" (matching what _write_config_toml writes), not "name"
        cfg.db_name = db.get("database", db.get("name", "mnemos"))
        cfg.db_user = db.get("user", "mnemos_user")
        cfg.db_password = db.get("password", os.environ.get("MNEMOS_DB_PASSWORD", ""))
        # Key is under [api], not [server]
        api = data.get("api", data.get("server", {}))
        cfg.listen_port = api.get("port", 5002)
        return cfg

    return None


if __name__ == "__main__":
    sys.exit(main())

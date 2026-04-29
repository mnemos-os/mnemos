"""Interactive Q&A wizard for MNEMOS mnemos.installer."""

from __future__ import annotations

import getpass
import os
import secrets
import string
import sys
from dataclasses import dataclass, field

from .detect import SystemInfo, check_port_free


@dataclass
class Config:
    profile: str = "edge"           # 'server', 'edge', 'dev'
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "mnemos"
    db_user: str = "mnemos_user"
    db_password: str = ""
    sqlite_path: str = "~/.mnemos/mnemos.db"
    listen_port: int = 5002
    service_user: str = "mnemos"
    auth_enabled: bool = False       # False for personal
    rls_enabled: bool = False        # False for personal
    graeae_providers: dict = field(default_factory=dict)
    inference_embed_host: str = "http://localhost:11434"
    install_docling: bool = True
    create_service: bool = True
    create_new_db: bool = True       # True = create DB, False = use existing


_PROVIDERS = [
    "openai",
    "anthropic",
    "xai",
    "groq",
    "perplexity",
    "gemini",
    "nvidia",
    "together",
]


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _prompt(question: str, default: str = "", secret: bool = False) -> str:
    """Prompt the user; return stripped input or default if blank."""
    if default:
        prompt_str = f"  {question} [default: {default}]: "
    else:
        prompt_str = f"  {question}: "
    try:
        if secret:
            value = getpass.getpass(prompt_str)
        else:
            value = input(prompt_str)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return value.strip() or default


def _prompt_bool(question: str, default: bool = True) -> bool:
    """Prompt for yes/no. Returns bool."""
    default_str = "Y/n" if default else "y/N"
    while True:
        raw = _prompt(f"{question} ({default_str})", default="")
        if raw == "":
            return default
        if raw.lower() in ("y", "yes"):
            return True
        if raw.lower() in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _prompt_int(question: str, default: int, min_val: int = 1, max_val: int = 65535) -> int:
    """Prompt for an integer in [min_val, max_val]."""
    while True:
        raw = _prompt(question, default=str(default))
        try:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  Value must be between {min_val} and {max_val}.")
        except ValueError:
            print("  Please enter a valid integer.")


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def _profile_uses_sqlite(profile: str) -> bool:
    return profile in {"edge", "dev"}


def run_wizard(info: SystemInfo, existing_config: dict = None, selected_profile: str | None = None) -> Config:
    """Run the interactive installation wizard and return a Config."""
    cfg = Config()

    # Pre-populate from existing config if upgrading
    if existing_config:
        for k, v in existing_config.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    current_user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))

    print("\n" + "=" * 50)
    print("  MNEMOS Installation Wizard")
    print("=" * 50)

    # ------------------------------------------------------------------ #
    # 1. Profile
    # ------------------------------------------------------------------ #
    _section("Deployment Profile")
    print("  Profiles:")
    print("    server — Postgres + Redis + multi-worker production deployment")
    print("    edge   — SQLite + single-worker laptop/Pi/edge appliance")
    print("    dev    — SQLite + debug logging for local development")

    if selected_profile:
        cfg.profile = selected_profile
    else:
        while True:
            raw = _prompt("Select profile", default="edge")
            if raw == "personal":
                raw = "edge"
            if raw in ("server", "edge", "dev"):
                cfg.profile = raw
                break
            print("  Choose: server, edge, or dev. Legacy personal maps to edge.")

    cfg.auth_enabled = False
    cfg.rls_enabled = False

    # ------------------------------------------------------------------ #
    # 2. Database
    # ------------------------------------------------------------------ #
    _section("Database Configuration")

    if _profile_uses_sqlite(cfg.profile):
        cfg.create_new_db = True
        cfg.sqlite_path = _prompt("SQLite database path", default=cfg.sqlite_path)
    else:
        cfg.create_new_db = _prompt_bool("Create a new PostgreSQL database?", default=True)

        cfg.db_host = _prompt("Database host", default="localhost")
        cfg.db_port = _prompt_int("Database port", default=5432)
        cfg.db_name = _prompt("Database name", default="mnemos")
        cfg.db_user = _prompt("Database user", default="mnemos_user")

        if cfg.create_new_db:
            offer_generate = _prompt_bool("Generate a random database password?", default=True)
            if offer_generate:
                cfg.db_password = _generate_password()
                print(f"  Generated password: {cfg.db_password}")
                print("  (Save this — it will be written to /etc/mnemos/mnemos.env)")
            else:
                while True:
                    pw = getpass.getpass("  Database password: ")
                    pw2 = getpass.getpass("  Confirm password: ")
                    if pw == pw2 and pw:
                        cfg.db_password = pw
                        break
                    print("  Passwords do not match or are empty. Try again.")
        else:
            cfg.db_password = getpass.getpass("  Database password: ")

    # ------------------------------------------------------------------ #
    # 3. Listen port
    # ------------------------------------------------------------------ #
    _section("API Server")

    while True:
        port = _prompt_int("Listen port", default=5002)
        if check_port_free(port):
            cfg.listen_port = port
            break
        print(f"  Port {port} is already in use. Choose a different port.")

    # ------------------------------------------------------------------ #
    # 4. Service user
    # ------------------------------------------------------------------ #
    _section("Service User")
    print("  Default: dedicated 'mnemos' system user (recommended)")
    print(f"  Alternative: run as current user '{current_user}'")

    use_dedicated = _prompt_bool("Create dedicated 'mnemos' service user?", default=True)
    if use_dedicated:
        cfg.service_user = "mnemos"
    else:
        cfg.service_user = current_user

    # ------------------------------------------------------------------ #
    # 5. GRAEAE providers
    # ------------------------------------------------------------------ #
    _section("GRAEAE Provider API Keys (optional)")

    configure_providers = False
    if cfg.profile in {"edge", "dev"}:
        configure_providers = _prompt_bool(
            "Configure LLM provider API keys for GRAEAE reasoning?", default=False
        )
    else:
        configure_providers = True

    if configure_providers:
        print("  Leave blank to skip a provider.\n")
        for provider in _PROVIDERS:
            env_var = f"{provider.upper()}_API_KEY"
            env_val = os.environ.get(env_var, "")
            if env_val:
                print(f"  {provider}: (found in environment ${env_var})")
                cfg.graeae_providers[provider] = env_val
            else:
                key = getpass.getpass(f"  API key for {provider} (blank to skip): ")
                if key.strip():
                    cfg.graeae_providers[provider] = key.strip()
    else:
        print("  Skipping provider configuration.")

    # ------------------------------------------------------------------ #
    # 6. Embedding inference host
    # ------------------------------------------------------------------ #
    _section("Embeddings")
    cfg.inference_embed_host = _prompt(
        "Embedding inference host", default="http://localhost:11434"
    )

    # ------------------------------------------------------------------ #
    # 7. Docling
    # ------------------------------------------------------------------ #
    _section("Optional: Document Import (docling)")
    print("  docling enables importing PDFs, DOCX, and other documents into MNEMOS.")
    print("  It requires additional system libraries and ~2 GB of space.")
    cfg.install_docling = _prompt_bool("Install docling?", default=True)

    # ------------------------------------------------------------------ #
    # 8. Service installation
    # ------------------------------------------------------------------ #
    _section("System Service")
    cfg.create_service = _prompt_bool(
        "Install MNEMOS as a system service (auto-start on boot)?", default=True
    )

    # ------------------------------------------------------------------ #
    # 9. Confirmation
    # ------------------------------------------------------------------ #
    _section("Confirm Configuration")
    print(f"  Profile:         {cfg.profile}")
    if _profile_uses_sqlite(cfg.profile):
        print(f"  Database:        sqlite:///{cfg.sqlite_path}")
    else:
        print(f"  Database:        postgresql://{cfg.db_user}@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")
        print(f"  Create new DB:   {cfg.create_new_db}")
    print(f"  Listen port:     {cfg.listen_port}")
    print(f"  Service user:    {cfg.service_user}")
    print(f"  Auth enabled:    {cfg.auth_enabled}")
    print(f"  GRAEAE providers: {list(cfg.graeae_providers.keys()) or 'none'}")
    print(f"  Embed host:      {cfg.inference_embed_host}")
    print(f"  Install docling: {cfg.install_docling}")
    print(f"  Create service:  {cfg.create_service}")
    print()

    confirmed = _prompt_bool("Proceed with this configuration?", default=True)
    if not confirmed:
        print("\nInstallation cancelled.")
        sys.exit(0)

    return cfg

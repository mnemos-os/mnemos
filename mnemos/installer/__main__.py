"""
MNEMOS Installer — entry point.

Usage:
    python -m mnemos.installer [--agent] [--wizard] [--unattended] [--upgrade] [--check]
                              [--profile server|edge|dev]

Options:
    --agent       LLM-guided installation (default)
    --wizard      Traditional interactive wizard
    --unattended  Non-interactive; reads config from environment variables
    --upgrade     Re-run migrations only (skip DB/service setup)
    --check       Environment check only, no changes

Environment variables for --unattended:
    MNEMOS_PROFILE, MNEMOS_DB_HOST, MNEMOS_DB_NAME, MNEMOS_DB_USER,
    MNEMOS_DB_PASSWORD, MNEMOS_SQLITE_PATH, MNEMOS_LISTEN_PORT,
    MNEMOS_SERVICE_USER
"""

from __future__ import annotations

import argparse
import os
import sys


_PROFILE_ALIASES = {"personal": "edge"}
_VALID_PROFILES = {"server", "edge", "dev"}


def _canonical_profile(raw_profile: str | None) -> str:
    profile = (raw_profile or "personal").strip().lower()
    profile = _PROFILE_ALIASES.get(profile, profile)
    if profile not in _VALID_PROFILES:
        valid = ", ".join(sorted(_VALID_PROFILES))
        raise argparse.ArgumentTypeError(
            f"unsupported profile {raw_profile!r}; expected one of: {valid}. "
            "Legacy profile 'personal' maps to 'edge'."
        )
    return profile


def _profile_uses_sqlite(profile: str) -> bool:
    return profile in {"edge", "dev"}


def _read_existing_internal_audit_token(content: str | None) -> str | None:
    """Parse ``[server].internal_audit_token`` from a TOML string.

    Uses ``tomllib`` (stdlib, Python 3.11+) so quoting/comment edge
    cases (single quotes, IPv6 URL literals, comments containing
    brackets) don't trip up the lookup. Returns ``None`` on parse
    failure, missing section, or empty value — caller treats that as
    "no existing token".
    """
    if not content:
        return None
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover — py<3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(content)
    except Exception:
        # Don't fail closed on parse error — if config.toml is
        # malformed, the rest of _write_config_toml's regex-based
        # patcher will already surface that to the operator. Falling
        # back to fresh-generate keeps the autogen useful.
        return None
    server = data.get("server")
    if not isinstance(server, dict):
        return None
    existing = server.get("internal_audit_token")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return None


def _resolve_internal_audit_token(existing_content: str | None = None) -> str:
    """Return the value to persist for [server].internal_audit_token.

    Priority:
    1. ``MNEMOS_INTERNAL_AUDIT_TOKEN`` env var (if set + non-empty).
    2. Existing ``[server].internal_audit_token`` in the resolved
       runtime config (honors ``MNEMOS_CONFIG_PATH`` so we read the
       same file the service reads — without this, an installer with
       MNEMOS_CONFIG_PATH=/etc/mnemos/config.toml and a stale
       repo_path/config.toml would generate a fresh token while the
       service kept reading the existing one, causing token skew
       between API and bridges).
    3. Existing token in the supplied ``existing_content`` string
       (the in-memory copy ``_write_config_toml`` is patching, used
       for the case where the runtime path equals repo_path/config.toml
       so we don't double-read the same file).
    4. Freshly generated 256-bit hex token (``secrets.token_hex(32)``).

    Round-3 residual #1 follow-up to #146: shipping a generated token
    by default flips ``/v1/internal/mcp_audit`` from legacy mode (any
    authenticated caller) to service-only mode (caller must present the
    token via ``X-Mnemos-Audit-Token``). Operators can still override
    via env or by hand-editing config.toml.
    """
    import secrets

    env_token = os.environ.get("MNEMOS_INTERNAL_AUDIT_TOKEN", "").strip()
    if env_token:
        return env_token

    # Codex-flagged HIGH: the runtime resolves config via
    # MNEMOS_CONFIG_PATH; if that points at a different file than
    # repo_path/config.toml, reading only the in-memory `existing_content`
    # would miss an already-installed token there.
    runtime_path = _resolve_runtime_config_path()
    if runtime_path and os.path.exists(runtime_path):
        try:
            with open(runtime_path) as fh:
                runtime_content = fh.read()
        except OSError:
            runtime_content = None
        runtime_token = _read_existing_internal_audit_token(runtime_content)
        if runtime_token:
            return runtime_token

    in_memory_token = _read_existing_internal_audit_token(existing_content)
    if in_memory_token:
        return in_memory_token

    return secrets.token_hex(32)


def _has_dsn_config(repo_path: str | None = None) -> bool:
    """True iff a DSN/url-based DB config is in scope.

    Runtime supports DATABASE_URL / MNEMOS_DATABASE_URL / PG_URL (and
    the *_DSN variants), but the installer's migration runner is
    not DSN-aware — it shells out to `sudo -u postgres psql -d <db>`
    using config.db_host/db_port/db_name fields. If the operator has
    a DSN config, the runtime uses the DSN target while --upgrade
    silently migrates the localhost defaults instead. Refuse the
    upgrade in that case so operator notices.

    Sources checked: env vars (any of the documented names) and
    [database].url / [database].dsn in config.toml.
    """
    import os

    dsn_env_vars = (
        "MNEMOS_DATABASE_URL",
        "DATABASE_URL",
        "PG_URL",
        "MNEMOS_DATABASE_DSN",
        "DATABASE_DSN",
        "PG_DSN",
    )
    for var in dsn_env_vars:
        if os.environ.get(var):
            return True

    # Honor MNEMOS_CONFIG_PATH first (round-22 HIGH).
    config_path = _resolve_runtime_config_path(repo_path)
    if config_path and os.path.exists(config_path):
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(config_path, "rb") as fh:
                data = tomllib.load(fh)
            db = data.get("database", {}) or {}
            if db.get("url") or db.get("dsn"):
                return True
        except Exception:
            # Don't refuse upgrade if config.toml is unreadable —
            # other code paths surface that error more clearly.
            return False

    return False


def _resolve_repo_path() -> str | None:
    """Find the installed mnemos repo path (where config.toml lives)."""
    import os
    # Same shape as the upgrade path uses: __file__'s grandparent's
    # grandparent (mnemos/installer/__main__.py → repo root).
    try:
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
        )
    except Exception:
        return None


def _resolve_runtime_config_path(repo_path: str | None = None) -> str | None:
    """Find the config.toml the runtime service would actually load.

    Order:
    1. MNEMOS_CONFIG_PATH env var (if set)
    2. repo_path/config.toml (the installed service location)
    3. /etc/mnemos/config.toml

    Returns the first existing path, or None if none exist.

    Round-24 HIGH: deliberately DROPS the runtime's `cwd/config.toml`
    candidate. Runtime checks cwd because the service is running with
    `WorkingDirectory=repo_path`, so cwd/config.toml resolves to the
    same file as repo_path/config.toml in production. But the
    installer runs in the OPERATOR'S shell — if the operator is in
    /tmp or any other directory containing a stray config.toml, the
    runtime cwd lookup would shadow the actual installed service
    config. For --upgrade purposes, we resolve to the file the
    *running service* sees, not the operator's cwd.

    Operators who need to upgrade an alternate config must set
    MNEMOS_CONFIG_PATH explicitly.

    Round-22 HIGH: hardcoded repo_path/config.toml ignored
    MNEMOS_CONFIG_PATH, so a production deployment with
    MNEMOS_CONFIG_PATH=/etc/mnemos/config.toml + a stale repo
    config.toml would have --upgrade load+patch the wrong file.
    """
    import os
    from pathlib import Path

    candidates: list[str] = []
    configured = os.environ.get("MNEMOS_CONFIG_PATH", "").strip()
    if configured:
        candidates.append(str(Path(configured).expanduser()))

    if repo_path is None:
        repo_path = _resolve_repo_path()
    if repo_path:
        candidates.append(os.path.join(repo_path, "config.toml"))

    candidates.append("/etc/mnemos/config.toml")

    seen: set[str] = set()
    for path in candidates:
        try:
            normalized = os.path.realpath(path) if os.path.exists(path) else path
        except Exception:
            normalized = path
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(path):
            return path
    return None


def _resolve_runtime_backend(cfg, repo_path: str | None = None) -> str:
    """Resolve which DB backend the *runtime* will use.

    Returns 'postgres' or 'sqlite'. This is the canonical signal for
    --upgrade dispatch; profile is a deployment-shape signal that can
    diverge (operator-set MNEMOS_PROFILE=edge with [database].backend=
    postgres in config.toml). Mirrors the runtime resolution order in
    mnemos.core.config so installer and runtime agree.

    Order (highest priority first, mirrors runtime selection):
    1. Env: MNEMOS_PERSISTENCE_BACKEND / PERSISTENCE_BACKEND / PG_BACKEND
    2. DSN/url env signals (PG_URL/MNEMOS_DATABASE_URL/etc.) → postgres
    3. config.toml: [database].backend (case-insensitive, "pg"="postgres")
    4. config.toml: [database].url / [database].dsn → postgres
    5. Explicit Postgres connection signals (env or TOML host/port/
       database/user with non-default values) → postgres (round-21)
    6. profile-derived default (sqlite for edge/dev, postgres otherwise)

    Round-21: extended past env/TOML backend keys to mirror the runtime
    rule that explicit Postgres connection fields force postgres
    selection. Without this, profile=edge/dev with PG_HOST set but no
    explicit backend silently routed to sqlite migrations during
    --upgrade while the running service kept selecting postgres.
    """
    import os

    # Read TOML once if present. Honor MNEMOS_CONFIG_PATH first
    # (round-22 HIGH) — the runtime config loader checks that path
    # before falling back to repo/cwd/etc.
    toml_data: dict | None = None
    config_path = _resolve_runtime_config_path(repo_path)
    if config_path and os.path.exists(config_path):
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(config_path, "rb") as fh:
                toml_data = tomllib.load(fh)
        except Exception:
            # Unreadable config.toml — fall through.
            toml_data = None

    raw_db_toml = (toml_data or {}).get("database", {}) or {}

    # Sanitize db_toml the same way runtime does (round-27 MEDIUM).
    # mnemos.core.config._build_settings drops empty-string values
    # before passing to _DatabaseSettings(**db_section), so empty
    # placeholders fall through to env/defaults at runtime too.
    _DB_SANITIZE_KEYS = {
        "backend", "dsn", "url", "host", "port", "database",
        "user", "password", "sqlite_path",
    }
    db_toml = {
        k: v for k, v in raw_db_toml.items()
        if k not in _DB_SANITIZE_KEYS or not (isinstance(v, str) and v == "")
    }

    # Round-44 HIGH: BACKEND is a SPECIAL case.
    # _DatabaseSettings.backend uses `validation_alias=AliasChoices(
    # 'MNEMOS_PERSISTENCE_BACKEND', 'PERSISTENCE_BACKEND', 'PG_BACKEND')`.
    # In pydantic-settings 2.10.1, `validation_alias` makes the env
    # source override init kwargs. Empirically verified: with
    # init backend='sqlite' + PG_BACKEND=postgres, runtime picks
    # 'postgres'.
    #
    # Connection fields (host/port/database/user) DO NOT have
    # validation_alias — they only have env_prefix='PG_'. For those
    # fields, init kwargs WIN over env (round-43 was correct).
    #
    # So the priority is:
    # 1. Backend env (if present) wins over TOML backend.
    # 2. TOML backend (if non-empty) is the next signal.
    # 3. TOML url/dsn (non-empty) → postgres.
    # 4. DSN/url env (only when TOML url/dsn empty).
    # 5. TOML host/port/database/user (presence) → postgres.
    # 6. PG_* connection env (only when TOML connection fields absent).
    # 7. Profile-derived default.

    # 1. Backend env first (matches validation_alias precedence).
    backend_env_explicit = False
    backend_env: str | None = None
    backend_env_alias: str | None = None
    for alias in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
        if alias in os.environ:
            backend_env_explicit = True
            backend_env = os.environ[alias]
            backend_env_alias = alias
            break

    if backend_env_explicit:
        normalized = (backend_env or "").strip().lower()
        if normalized in ("pg", "postgres", "postgresql"):
            return "postgres"
        if normalized in ("sqlite", "sqlite3"):
            return "sqlite"
        if normalized != "auto":
            # Round-34/37 MEDIUM: fail closed on unrecognized or
            # explicit-empty backend env value.
            raise ValueError(
                f"Unsupported persistence backend {backend_env!r} "
                f"(via {backend_env_alias}); expected one of: "
                f"postgres, sqlite, auto. Set the env var to one of "
                f"those values, or unset to fall through to "
                f"profile/connection-field inference."
            )
        # 'auto' → env shadows TOML backend, fall through to
        # DSN/connection/profile inference (matches runtime —
        # empirically: PG_BACKEND='auto' + init backend='sqlite'
        # makes _DatabaseSettings.backend = 'auto', which lifecycle
        # then resolves via DSN/conn/profile, NOT via the TOML).

    # 2. TOML [database].backend — ONLY when env is unset (round-45
    #    HIGH: env 'auto' shadows TOML backend, just like an
    #    explicit env value). Accept runtime aliases (sqlite3 →
    #    sqlite, pg/postgresql → postgres) per
    #    lifecycle._normalize_backend_name.
    if not backend_env_explicit:
        # Round-46 MEDIUM: track presence in raw_db_toml (before
        # the empty-string sanitization). Whitespace-only values
        # like "   " survive sanitization but lifecycle raises on
        # them after stripping. Match that fail-closed behavior.
        backend_toml_present = "backend" in raw_db_toml
        backend_toml_raw = raw_db_toml.get("backend") if backend_toml_present else None
        backend_toml = (backend_toml_raw or "").strip().lower()
        if backend_toml in ("pg", "postgres", "postgresql"):
            return "postgres"
        if backend_toml in ("sqlite", "sqlite3"):
            return "sqlite"
        if backend_toml and backend_toml != "auto":
            # Fail closed on unrecognized explicit TOML backend
            # (round-34 MEDIUM, matches
            # lifecycle._normalize_backend_name).
            raise ValueError(
                f"Unsupported [database].backend = {backend_toml!r} "
                f"in config.toml; expected one of: postgres, sqlite, "
                f"auto. Fix config.toml or unset the field to fall "
                f"through to env/profile inference."
            )
        # Round-46 MEDIUM: whitespace-only value (stripped to empty)
        # but key was present in raw TOML → fail closed (matches
        # runtime which raises on _normalize_backend_name('') ).
        # The "" sanitization above only drops exact empty strings,
        # not whitespace-only values, so they reach this branch as
        # `backend_toml == ""` after stripping — distinguish via
        # `backend_toml_present` and the raw_db_toml entry surviving
        # the sanitize filter (whitespace strings aren't `== ""`).
        if (
            backend_toml_present
            and isinstance(backend_toml_raw, str)
            and backend_toml_raw != ""
            and backend_toml == ""
        ):
            raise ValueError(
                f"Unsupported [database].backend = {backend_toml_raw!r} "
                f"in config.toml (resolves to empty after strip); "
                f"expected one of: postgres, sqlite, auto. Fix "
                f"config.toml or unset the field."
            )

    # 3. DSN/url — non-empty TOML wins over env.
    if (db_toml.get("url") or db_toml.get("dsn")):
        return "postgres"
    dsn_env = any(
        os.environ.get(k)
        for k in (
            "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
            "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
        )
    )
    if dsn_env:
        return "postgres"

    # 4. Connection-field signals — non-empty TOML wins over env
    #    (matches runtime init-kwargs > env order). Only host/port/
    #    database/user count as postgres-distinguishing
    #    (round-26 / matches lifecycle._has_explicit_postgres_
    #    connection_config which excludes password). Only PG_* env
    #    aliases count — runtime _DatabaseSettings uses env_prefix
    #    "PG_" exclusively for these fields.
    #
    # First check sanitized TOML (non-empty wins).
    has_pg_toml_signal_first = any(
        field in db_toml for field in ("host", "port", "database", "user")
    )
    if has_pg_toml_signal_first:
        return "postgres"
    # Then env (any present PG_HOST/PG_PORT/PG_DATABASE/PG_USER).
    pg_conn_env = any(
        os.environ.get(k)
        for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER")
    )
    if pg_conn_env:
        return "postgres"

    # 5. Profile-derived default (no other signals).
    return "sqlite" if _profile_uses_sqlite(cfg.profile) else "postgres"


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
    parser.add_argument(
        "--profile",
        type=_canonical_profile,
        choices=sorted(_VALID_PROFILES),
        help="Deployment profile: server, edge, or dev. Legacy personal maps to edge.",
    )
    return parser.parse_args()


def _looks_like_default_postgres(db: dict) -> bool:
    """Return True iff `db` only carries the bare _DatabaseSettings defaults.

    Used to distinguish a config.toml that was authored for sqlite (and
    happens to still have host="localhost", port=5432, etc. as the
    inherited defaults) from one that was actively configured for
    postgres. The defaults alone are insufficient signal — operators
    don't typically declare them on a sqlite config.
    """
    return (
        db.get("host", "localhost") == "localhost"
        and db.get("port", 5432) == 5432
        and not db.get("password")
        and db.get("user", "mnemos_user") == "mnemos_user"
        and db.get("database", "mnemos") == "mnemos"
    )


def _patch_config_toml_embedding_dim(config_path: str, new_dim: int) -> bool:
    """Surgically update only [database].embedding_dim in config.toml.

    Used by --upgrade so an embedding-dim model swap doesn't accidentally
    rewrite profile-derived defaults like backend, rate_limit storage,
    graeae mode, logging level, or compression workers — all of which
    `_write_config_toml()` would clobber from cfg.profile-defaults if
    called for an embedding-only change.

    Round-23: takes the resolved config_path directly (was repo_path
    + hardcoded "config.toml"). The caller honors MNEMOS_CONFIG_PATH
    via _resolve_runtime_config_path so the patched file is the same
    file the runtime will read.

    Returns True on success, False on any failure (read parse, write,
    atomic replace). The caller treats False as a fatal upgrade error.
    """
    import re

    try:
        with open(config_path, "r") as fh:
            text = fh.read()
    except Exception as exc:
        print(f"[installer] ERROR could not read {config_path}: {exc}", file=sys.stderr)
        return False

    # First pass: validate that the file is parseable TOML and learn what
    # tomllib sees. If it parses with a [database] table but our regex
    # doesn't find a header span we can safely patch, refuse to write —
    # appending a duplicate [database] table would corrupt the file.
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        parsed = tomllib.loads(text)
    except Exception as exc:
        print(
            f"[installer] ERROR config.toml is not parseable TOML: {exc}. "
            f"Refusing to patch a malformed file.",
            file=sys.stderr,
        )
        return False
    parsed_has_database = isinstance(parsed.get("database"), dict)

    # Header-line regex tolerates leading whitespace and trailing inline
    # comments — both are valid TOML. Match must be a proper section
    # header, not (e.g.) `[database]` inside a string value.
    db_section_re = re.compile(
        r"^[ \t]*\[database\][ \t]*(?:#[^\n]*)?$", re.MULTILINE
    )
    # Boundary: ANY new section header ends the [database] body, including
    # array-of-tables `[[...]]`. The previous `(?!\[)` rejected `[[` and
    # let the body over-extend across providers/etc; embedding_re.sub
    # would then rewrite UNRELATED embedding_dim lines in those tables.
    next_section_re = re.compile(r"^[ \t]*\[", re.MULTILINE)
    db_match = db_section_re.search(text)

    if parsed_has_database and not db_match:
        # tomllib sees a [database] table but our regex can't safely span
        # it — refuse rather than append a duplicate. Likely shapes that
        # hit this: an array-of-tables `[[database]]`, or a nested key
        # like `[server.database]`. Safer to fail early than corrupt the
        # file post-ALTER.
        print(
            f"[installer] ERROR config.toml has a [database] table but the "
            f"installer cannot map it to a safe source span. Edit "
            f"config.toml manually to set [database].embedding_dim = "
            f"{int(new_dim)}, then re-run.",
            file=sys.stderr,
        )
        return False

    if not db_match:
        # tomllib parsed the file with no [database] table — fresh shape,
        # safe to append.
        new_text = text.rstrip() + f"\n\n[database]\nembedding_dim = {int(new_dim)}\n"
    else:
        section_start = db_match.end()
        # Find the next section header to bound the [database] block.
        next_match = next_section_re.search(text, section_start)
        section_end = next_match.start() if next_match else len(text)
        section_body = text[section_start:section_end]

        # Replace existing embedding_dim line, or append before the next section.
        embedding_re = re.compile(r"^[ \t]*embedding_dim\s*=\s*[^\n]*$", re.MULTILINE)
        if embedding_re.search(section_body):
            new_section_body = embedding_re.sub(
                f"embedding_dim = {int(new_dim)}", section_body
            )
        else:
            # Append at the end of the [database] block (before next section).
            trail = section_body.rstrip("\n")
            new_section_body = f"{trail}\nembedding_dim = {int(new_dim)}\n"
            # Preserve any trailing blank lines that originally separated
            # sections.
            if section_body.endswith("\n\n"):
                new_section_body += "\n"

        new_text = text[:section_start] + new_section_body + text[section_end:]

    # Final validation: the patched text must still be parseable TOML
    # AND have [database].embedding_dim at the requested value.
    try:
        new_parsed = tomllib.loads(new_text)
    except Exception as exc:
        print(
            f"[installer] ERROR patched config.toml is not parseable TOML: "
            f"{exc}. Refusing to install a malformed file.",
            file=sys.stderr,
        )
        return False
    if not isinstance(new_parsed.get("database"), dict):
        print(
            f"[installer] ERROR patched config.toml has no [database] table.",
            file=sys.stderr,
        )
        return False
    if int(new_parsed["database"].get("embedding_dim", -1)) != int(new_dim):
        print(
            f"[installer] ERROR patched config.toml's "
            f"[database].embedding_dim is "
            f"{new_parsed['database'].get('embedding_dim')!r}, expected "
            f"{int(new_dim)}.",
            file=sys.stderr,
        )
        return False

    # Stage the patched content in the system temp dir (NOT in
    # dirname(config.toml) — the config could live in a privileged
    # location). Then go through `install -m -o -g` preserving the
    # existing owner/group/mode, with sudo fallback for privileged
    # destinations. The previous os.replace + chmod 0600 path could
    # turn a service-readable root:mnemos 0640 config into root:root
    # 0600, locking the service out of its own config after the DB
    # had already been ALTERed.
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".toml.tmp")
    except Exception as exc:
        print(
            f"[installer] ERROR could not create temp file for config patch: {exc}",
            file=sys.stderr,
        )
        return False
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(f"[installer] ERROR could not stage config patch: {exc}", file=sys.stderr)
        return False

    # Read existing uid/gid/mode for preservation.
    try:
        st = os.stat(config_path)
        target_mode = st.st_mode & 0o777
        target_uid = st.st_uid
        target_gid = st.st_gid
    except Exception:
        # Reasonable defaults if we somehow can't stat (shouldn't happen
        # — we just read the file at the top of this function).
        target_mode = 0o640
        target_uid = 0
        target_gid = 0

    from .db import _run
    install_args = [
        "install",
        "-m", oct(target_mode)[2:].zfill(3),
        "-o", str(target_uid),
        "-g", str(target_gid),
        tmp_path, config_path,
    ]
    rc, _out, err = _run(install_args)
    if rc != 0:
        rc, _out, err = _run(["sudo"] + install_args)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    if rc != 0:
        print(
            f"[installer] ERROR install of config.toml failed: "
            f"{err.strip() or '(no detail)'}",
            file=sys.stderr,
        )
        return False
    return True


def _patch_config_toml_internal_audit_token(config_path: str) -> bool:
    """Surgically autogen [server].internal_audit_token if missing.

    Used by --upgrade to bring v5.3.4-era installs (where #148 added
    the env var but operators rarely set it) up to the #150 default-on
    posture without rewriting profile-derived defaults the operator
    may have tuned. Mirrors `_patch_config_toml_embedding_dim` shape:
    parse with tomllib, surgically insert into the [server] block,
    atomic ``install -m`` write preserving owner/group/mode.

    Behavior:
      * If [server].internal_audit_token is already a non-empty string,
        leave it alone and return True (no-op).
      * If [server] table exists but the key is missing/empty, generate
        ``secrets.token_hex(32)`` and append the line at the end of the
        section. Return True on success.
      * If [server] table is missing entirely, append a new section
        with the autogen token. Return True on success.
      * Returns False on parse failure, malformed file, or atomic-
        write failure. Caller treats False as a soft warning rather
        than a fatal upgrade error — without the token the endpoint
        operates in legacy mode, so the upgrade is still functional;
        the operator can hand-edit later.
    """
    import re
    import secrets

    try:
        with open(config_path) as fh:
            text = fh.read()
    except Exception as exc:
        print(
            f"[installer] WARNING could not read {config_path} for audit "
            f"token patch: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover — py<3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        parsed = tomllib.loads(text)
    except Exception as exc:
        print(
            f"[installer] WARNING config.toml at {config_path} is not "
            f"parseable TOML: {exc}. Skipping audit-token patch.",
            file=sys.stderr,
        )
        return False

    server_table = parsed.get("server", {}) if isinstance(parsed, dict) else {}
    if isinstance(server_table, dict):
        existing = server_table.get("internal_audit_token")
        if isinstance(existing, str) and existing.strip():
            # Already populated — nothing to do. Honors operator-set
            # tokens (env or hand-edit) AND tokens previously written
            # by a fresh install of this version.
            return True

    # Honor MNEMOS_INTERNAL_AUDIT_TOKEN env if the operator is
    # rotating across the upgrade boundary.
    env_token = os.environ.get("MNEMOS_INTERNAL_AUDIT_TOKEN", "").strip()
    new_token = env_token or secrets.token_hex(32)
    quoted = f'"{new_token}"'

    # Surgical patch into [server] section. Mirrors the embedding_dim
    # helper but the target is internal_audit_token.
    server_section_re = re.compile(
        r"^[ \t]*\[server\][ \t]*(?:#[^\n]*)?$", re.MULTILINE
    )
    next_section_re = re.compile(r"^[ \t]*\[", re.MULTILINE)
    server_match = server_section_re.search(text)

    if not server_match:
        # No [server] section — append one with the token.
        new_text = text.rstrip() + f"\n\n[server]\ninternal_audit_token = {quoted}\n"
    else:
        section_start = server_match.end()
        next_match = next_section_re.search(text, section_start)
        section_end = next_match.start() if next_match else len(text)
        section_body = text[section_start:section_end]

        # Replace empty/commented-style key OR append at end of block.
        # Empty (key = "") line: replace. Allow optional trailing
        # inline comment ("# placeholder") — codex round-1 HIGH on
        # #151: without the `(?:#[^\n]*)?` allowance, valid TOML like
        # `internal_audit_token = "" # placeholder` would be parsed
        # by tomllib as empty (so the populated-check skipped this
        # line) but the regex wouldn't match it for replacement, so
        # the patcher appended a SECOND live key, the duplicate-key
        # validation failed, and the upgrade soft-warned — leaving
        # /v1/internal/mcp_audit in legacy mode despite an apparent
        # placeholder ready to fill in.
        empty_re = re.compile(
            r"^[ \t]*internal_audit_token\s*=\s*[\"']\s*[\"'][ \t]*"
            r"(?:#[^\n]*)?$",
            re.MULTILINE,
        )
        if empty_re.search(section_body):
            new_section_body = empty_re.sub(
                f"internal_audit_token = {quoted}", section_body
            )
        else:
            trail = section_body.rstrip("\n")
            new_section_body = f"{trail}\ninternal_audit_token = {quoted}\n"
            if section_body.endswith("\n\n"):
                new_section_body += "\n"

        new_text = text[:section_start] + new_section_body + text[section_end:]

    # Final validation: must still parse + token must be present + non-empty.
    try:
        new_parsed = tomllib.loads(new_text)
    except Exception as exc:
        print(
            f"[installer] WARNING patched config.toml failed TOML parse after "
            f"audit-token insertion: {exc}. Skipping write.",
            file=sys.stderr,
        )
        return False
    new_server = new_parsed.get("server", {}) if isinstance(new_parsed, dict) else {}
    if not (
        isinstance(new_server, dict)
        and isinstance(new_server.get("internal_audit_token"), str)
        and new_server["internal_audit_token"].strip()
    ):
        print(
            "[installer] WARNING audit-token patch did not produce a live "
            "[server].internal_audit_token in parsed result; skipping write.",
            file=sys.stderr,
        )
        return False

    # Atomic install -m preserving owner/group/mode (mirrors embedding-dim helper).
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".toml.tmp")
    except Exception as exc:
        print(
            f"[installer] WARNING could not create temp file for audit-token "
            f"patch: {exc}",
            file=sys.stderr,
        )
        return False
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(
            f"[installer] WARNING could not stage audit-token patch: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        st = os.stat(config_path)
        target_mode = st.st_mode & 0o777
        target_uid = st.st_uid
        target_gid = st.st_gid
    except Exception:
        target_mode = 0o600
        target_uid = 0
        target_gid = 0

    from .db import _run
    install_args = [
        "install",
        "-m", oct(target_mode)[2:].zfill(3),
        "-o", str(target_uid),
        "-g", str(target_gid),
        tmp_path, config_path,
    ]
    rc, _out, err = _run(install_args)
    if rc != 0:
        rc, _out, err = _run(["sudo"] + install_args)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    if rc != 0:
        print(
            f"[installer] WARNING install of audit-token-patched config.toml "
            f"failed: {err.strip() or '(no detail)'}",
            file=sys.stderr,
        )
        return False
    return True


def _patch_service_env_embedding_dim(env_path: str, new_dim: int) -> bool:
    """Surgically update MNEMOS_EMBEDDING_DIM in the systemd EnvironmentFile.

    Used by --upgrade so we don't accidentally drop provider API keys or
    operator-managed lines. The full _write_env_file() rewrites from
    cfg.graeae_providers which _load_existing_config() doesn't populate
    on --upgrade — calling it would silently erase OPENAI_API_KEY,
    ANTHROPIC_API_KEY, GEMINI_API_KEY, etc. Surgical update preserves
    everything else.

    The systemd EnvironmentFile is typically owned by root:mnemos with
    0640. We preserve mode + ownership when possible. Returns False on
    any failure (read, parse, write, atomic replace).
    """
    import os
    import re

    if not os.path.exists(env_path):
        # Service env file not present — nothing to patch. Caller will
        # treat this as a hard failure since we expect it to exist when
        # cfg.create_service is True.
        return False

    try:
        with open(env_path, "r") as fh:
            text = fh.read()
    except PermissionError:
        # Try sudo cat as a fallback for root-owned files.
        from .db import _run
        rc, out, _err = _run(["sudo", "cat", env_path])
        if rc != 0:
            print(
                f"[installer] ERROR could not read {env_path}: permission "
                f"denied and sudo cat failed too.",
                file=sys.stderr,
            )
            return False
        text = out
    except Exception as exc:
        print(f"[installer] ERROR could not read {env_path}: {exc}", file=sys.stderr)
        return False

    # Look for an existing MNEMOS_EMBEDDING_DIM=N line; replace, or append.
    line_re = re.compile(r"^MNEMOS_EMBEDDING_DIM=[^\n]*$", re.MULTILINE)
    new_line = f"MNEMOS_EMBEDDING_DIM={int(new_dim)}"
    if line_re.search(text):
        new_text = line_re.sub(new_line, text)
    else:
        # Append; ensure trailing newline shape is sensible.
        new_text = text.rstrip("\n") + "\n" + new_line + "\n"

    # Stage the patched content in a user-writable tempfile (system temp,
    # NOT dirname(env_path) — the production /etc/mnemos directory is
    # root-owned and tempfile.mkstemp would PermissionError there before
    # any sudo fallback could run).
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mnemos.env.tmp")
    except Exception as exc:
        print(
            f"[installer] ERROR could not create temp file for env patch: {exc}",
            file=sys.stderr,
        )
        return False
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(f"[installer] ERROR could not write env stage: {exc}", file=sys.stderr)
        return False

    # Always preserve uid/gid/mode from the existing file. The previous
    # round used os.replace + os.chmod for the user-owned case, but that
    # pattern is wrong when the installer runs as root against a
    # root:mnemos 0640 file — os.replace would install temp as root:root,
    # locking the service group out. It also breaks on hosts where /tmp
    # is a separate filesystem (EXDEV from os.replace).
    #
    # Cleaner: always go through `install -m -o -g`, using sudo only
    # when the existing file isn't writable as us.
    try:
        st = os.stat(env_path)
        target_mode = st.st_mode & 0o777
        target_uid = st.st_uid
        target_gid = st.st_gid
    except Exception:
        # Defaults for systemd EnvironmentFile shape.
        target_mode = 0o640
        target_uid = 0
        target_gid = 0

    from .db import _run
    install_args = [
        "install",
        "-m", oct(target_mode)[2:].zfill(3),
        "-o", str(target_uid),
        "-g", str(target_gid),
        tmp_path, env_path,
    ]
    # Try without sudo first (works when the operator owns the file or
    # is root). Fall back to sudo install when permission denied.
    rc, _out, err = _run(install_args)
    if rc != 0:
        # Retry under sudo for the standard root-owned case.
        rc, _out, err = _run(["sudo"] + install_args)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    if rc != 0:
        print(
            f"[installer] ERROR install of env file failed: "
            f"{err.strip() or '(no detail)'}. The DB and config.toml may "
            f"already reflect MNEMOS_EMBEDDING_DIM={int(new_dim)} but "
            f"{env_path} was NOT updated.",
            file=sys.stderr,
        )
        return False
    return True


def _verify_config_toml_embedding_dim(config_path: str, expected_dim: int) -> bool:
    """Read back config.toml and confirm [database].embedding_dim matches.

    Defensive against partial writes / filesystem drift after a schema-
    altering migration. Returns False if the file doesn't exist, can't be
    parsed, or carries a different dim. The caller treats False as a fatal
    upgrade error since the DB may already be at the new vector(<dim>).

    Round-23: takes the resolved config_path directly (was repo_path
    + hardcoded "config.toml"). Caller honors MNEMOS_CONFIG_PATH so
    the verified file is the same file the runtime will read.
    """
    import os

    if not os.path.exists(config_path):
        return False
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return False
    db = data.get("database", {})
    try:
        return int(db.get("embedding_dim", -1)) == int(expected_dim)
    except (TypeError, ValueError):
        return False


def _apply_embedding_dim_from_env(cfg) -> None:
    """Override cfg.embedding_dim from MNEMOS_EMBEDDING_DIM (or PG_EMBEDDING_DIM).

    Centralized so unattended / wizard / agent installer modes all honor
    the env var. Wizard and agent collect most config interactively but
    don't (yet) prompt for embedding dim — falling back to the env var
    keeps the cix Sky1 NPU substrate's MNEMOS_EMBEDDING_DIM=512 path
    working without burying the question in the wizard flow.

    No-op if the env var isn't set or is invalid; cfg.embedding_dim
    keeps its default (768 from the Config dataclass) on failure.
    """
    # Round-39 MEDIUM: Pydantic AliasChoices semantics — first present
    # alias wins (regardless of value, including empty string). Truthy
    # `or` chain previously skipped MNEMOS_EMBEDDING_DIM='' and let
    # PG_EMBEDDING_DIM take over, while runtime would have raised
    # ValidationError on the empty value. Empty/malformed must fail
    # closed.
    raw: str | None = None
    chosen_alias: str | None = None
    for alias in ("MNEMOS_EMBEDDING_DIM", "PG_EMBEDDING_DIM"):
        if alias in os.environ:
            raw = os.environ[alias]
            chosen_alias = alias
            break
    if raw is None:
        return  # neither alias set — keep default
    try:
        cfg.embedding_dim = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid embedding dimension {raw!r} via {chosen_alias}: "
            f"{exc}. Set MNEMOS_EMBEDDING_DIM/PG_EMBEDDING_DIM to a "
            f"positive integer matching your embedding model "
            f"(default 768). Refusing to dispatch migrations under a "
            f"value runtime would reject."
        )


def _config_from_env_runtime_parity() -> "Config":
    """Build a Config from env strictly mirroring runtime selection.

    Used by the --upgrade no-config fallback (round-25 HIGH). Runtime
    _DatabaseSettings uses env_prefix='PG_' exclusively for
    host/port/database/user — MNEMOS_DB_* env aliases have NO effect
    on runtime selection. So if the upgrade no-config loader treats
    MNEMOS_DB_HOST/NAME/USER as authoritative, it can migrate one DB
    while runtime starts on another (or on defaults).

    Difference from _config_from_env (the fresh-install loader):
    - DB host/port/database/user: PG_* only, NOT MNEMOS_DB_*
    - Profile inference: only PG_*-shape signals (host/port/db/user
      explicit, dsn/url, or PG_PASSWORD) trigger 'server'; MNEMOS_DB_*
      alone does not
    - Password: still accepts MNEMOS_DB_PASSWORD as documented alias
      (the installer's env writer maps both names to PG_PASSWORD in
      the systemd env file)
    """
    from .wizard import Config

    cfg = Config()
    # Profile inference: only postgres-distinguishing signals trigger
    # 'server'. Round-26 HIGH: runtime
    # lifecycle._has_explicit_postgres_connection_config explicitly
    # IGNORES password — it only counts host/port/database/user. So
    # PG_PASSWORD-only env (e.g. cred rotation in a sqlite/edge
    # deployment) must NOT force server profile, otherwise the
    # installer dispatches to postgres while runtime stays on sqlite.
    # Round-32 MEDIUM: honor MNEMOS_PROFILE_OVERRIDE (highest-priority
    # source per runtime _profile_from_sources). Without this, an
    # operator running --upgrade with MNEMOS_PROFILE_OVERRIDE=server +
    # no config.toml would have the runtime-parity loader resolve to
    # edge while runtime resolved to server, dispatching --upgrade to
    # sqlite migrations while the service started on postgres.
    explicit_profile = (
        os.environ.get("MNEMOS_PROFILE_OVERRIDE")
        or os.environ.get("MNEMOS_PROFILE")
    )
    # Round-42 HIGH: presence-based PG signal check, matching round-41.
    # Treat PG_HOST/PG_DATABASE/PG_USER as a postgres signal whenever
    # they are PRESENT in env (even empty) — runtime keeps the
    # explicit empty values as postgres-distinguishing fields.
    has_pg_env_signal = (
        any(
            k in os.environ
            for k in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER")
        )
        or any(
            os.environ.get(k)
            for k in (
                "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
                "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
            )
        )
    )
    if explicit_profile:
        cfg.profile = _canonical_profile(explicit_profile)
    elif has_pg_env_signal:
        cfg.profile = "server"
    else:
        cfg.profile = _canonical_profile("personal")

    # DB connection fields: PG_* only (matches runtime). Round-42
    # HIGH: explicit empty PG_HOST/PG_DATABASE/PG_USER fails closed
    # — matches the round-41 _resolve_db_field_strict contract for
    # the config.toml loader. Without this, no-config + PG_HOST=''
    # + PG_PASSWORD=secret would load cfg.db_host='' and the
    # resolver would silently dispatch sqlite while runtime selected
    # postgres.
    def _strict_pg_env_field(env_key: str, default: str, field_name: str) -> str:
        if env_key in os.environ:
            v = os.environ[env_key]
            if v == "":
                raise ValueError(
                    f"Invalid {field_name}: env {env_key}='' is "
                    f"explicit empty. Runtime would reject this. "
                    f"Set {env_key} to a non-empty value or unset it."
                )
            # Round-47/48: reject whitespace-only and padded values.
            if v.strip() == "":
                raise ValueError(
                    f"Invalid {field_name}: env {env_key}={v!r} is "
                    f"whitespace-only. Runtime would pass this through "
                    f"unchanged and fail. Set {env_key} to a non-empty "
                    f"value or unset it."
                )
            if v != v.strip():
                raise ValueError(
                    f"Invalid {field_name}: env {env_key}={v!r} has "
                    f"leading/trailing whitespace. Runtime passes the "
                    f"raw value to the connection — trim the whitespace "
                    f"or fix the typo."
                )
            return v
        return default

    cfg.db_host = _strict_pg_env_field("PG_HOST", "localhost", "Postgres host")

    # Round-42 HIGH: PG_PORT presence-based parsing matches round-40.
    if "PG_PORT" in os.environ:
        raw_port = os.environ["PG_PORT"]
        try:
            cfg.db_port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid Postgres port {raw_port!r}: {exc}. Set "
                f"PG_PORT to a valid integer or unset to default."
            )
    else:
        cfg.db_port = 5432

    cfg.db_name = _strict_pg_env_field("PG_DATABASE", "mnemos", "Postgres database name")
    cfg.db_user = _strict_pg_env_field("PG_USER", "mnemos_user", "Postgres user")
    # Password: documented exception — both names map to PG_PASSWORD
    # in the production env file, so accepting either is parity-
    # preserving. Empty allowed (peer auth / sudo psql shapes).
    cfg.db_password = os.environ.get("MNEMOS_DB_PASSWORD") or os.environ.get(
        "PG_PASSWORD", ""
    )

    cfg.sqlite_path = os.environ.get("MNEMOS_SQLITE_PATH", "~/.mnemos/mnemos.db")
    _apply_embedding_dim_from_env(cfg)
    cfg.listen_port = int(os.environ.get("MNEMOS_LISTEN_PORT", "5002"))
    cfg.service_user = os.environ.get("MNEMOS_SERVICE_USER", "mnemos")
    cfg.auth_enabled = False
    cfg.rls_enabled = False
    # --upgrade never creates the DB; the service is already provisioned.
    cfg.create_new_db = False
    cfg.create_service = False

    # Provider keys from env (same as install path).
    providers = ["openai", "anthropic", "xai", "groq", "perplexity", "gemini", "nvidia", "together"]
    for p in providers:
        key = os.environ.get(f"{p.upper()}_API_KEY", "")
        if key:
            cfg.graeae_providers[p] = key

    return cfg


def _config_from_env() -> "Config":
    """Build a Config from environment variables for unattended installs.

    Accepts both the MNEMOS_DB_* shape (the installer's preferred unattended-
    install convention) AND the PG_* shape (what `service._write_env_file()`
    actually emits to systemd's EnvironmentFile, and what runtime config
    accepts). Without the PG_* fallbacks, a container shape that mirrors
    the production env file would hit an --upgrade with cfg defaults
    (localhost/mnemos/mnemos_user) and target the wrong DB.

    NOTE: this loader is for FRESH installs. The --upgrade no-config
    path uses _config_from_env_runtime_parity() instead, which is
    PG_*-strict to match what the running service reads.
    """
    from .wizard import Config

    cfg = Config()
    # Profile inference: if MNEMOS_PROFILE isn't set explicitly but the
    # env shape carries PG_*/MNEMOS_DB_* postgres signals (host or
    # password — the production unattended shape), assume "server"
    # rather than the legacy "personal->edge" default. Without this,
    # an env-only --upgrade with PG_PASSWORD set would resolve to
    # profile=edge and (a) take the SQLite migration path, (b)
    # bypass the Postgres remote-host rejection guard which is gated
    # on `not _profile_uses_sqlite(config.profile)`. Codex round-17
    # HIGH finding.
    explicit_profile = os.environ.get("MNEMOS_PROFILE")
    has_pg_env_signal = any(
        os.environ.get(k)
        for k in (
            "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
            "MNEMOS_DB_HOST", "MNEMOS_DB_PORT", "MNEMOS_DB_NAME",
            "MNEMOS_DB_USER", "MNEMOS_DB_PASSWORD",
        )
    )
    if explicit_profile:
        cfg.profile = _canonical_profile(explicit_profile)
    elif has_pg_env_signal:
        cfg.profile = "server"
    else:
        cfg.profile = _canonical_profile("personal")
    cfg.db_host = os.environ.get("MNEMOS_DB_HOST") or os.environ.get("PG_HOST", "localhost")
    cfg.db_port = int(
        os.environ.get("MNEMOS_DB_PORT") or os.environ.get("PG_PORT", "5432")
    )
    cfg.db_name = os.environ.get("MNEMOS_DB_NAME") or os.environ.get("PG_DATABASE", "mnemos")
    cfg.db_user = os.environ.get("MNEMOS_DB_USER") or os.environ.get("PG_USER", "mnemos_user")
    cfg.db_password = os.environ.get("MNEMOS_DB_PASSWORD") or os.environ.get(
        "PG_PASSWORD", ""
    )
    cfg.sqlite_path = os.environ.get("MNEMOS_SQLITE_PATH", "~/.mnemos/mnemos.db")
    _apply_embedding_dim_from_env(cfg)
    cfg.listen_port = int(os.environ.get("MNEMOS_LISTEN_PORT", "5002"))
    cfg.service_user = os.environ.get("MNEMOS_SERVICE_USER", "mnemos")
    cfg.auth_enabled = False
    cfg.rls_enabled = False
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

    if not _profile_uses_sqlite(cfg.profile) and not cfg.db_password:
        print(
            "ERROR: MNEMOS_DB_PASSWORD is required for unattended install.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cfg



def _resolve_config_write_target(repo_path: str) -> str:
    """Pick the config.toml the installer should write.

    Honors ``MNEMOS_CONFIG_PATH`` when set + non-empty (matches runtime
    resolution semantics) so an installer running with
    ``MNEMOS_CONFIG_PATH=/etc/mnemos/config.toml`` patches the file the
    service actually reads instead of dumping the autogen audit token
    into a stale ``repo_path/config.toml`` the service will never load.
    Falls back to ``repo_path/config.toml`` when the env var is unset.

    Codex round-3 HIGH on #150: the read-side resolver already honored
    MNEMOS_CONFIG_PATH; without matching write-side honoring, the
    install-time autogen wrote the credential to a file the runtime
    didn't read, leaving ``/v1/internal/mcp_audit`` in legacy mode for
    operators using a relocated config — the exact deployment shape
    the slice was trying to support.
    """
    from pathlib import Path

    configured = os.environ.get("MNEMOS_CONFIG_PATH", "").strip()
    if configured:
        return str(Path(configured).expanduser())
    return os.path.join(repo_path, "config.toml")


def _write_config_toml(cfg, repo_path: str) -> None:
    """Write (or update) config.toml with installer-collected values.

    Reads config.toml.example as the template, patches the [database] section
    with actual credentials, and writes to config.toml.  If config.toml already
    exists its [database] block is updated in-place.

    Honors ``MNEMOS_CONFIG_PATH`` for the write target so a relocated
    runtime config gets patched in place (see ``_resolve_config_write_target``).
    """
    import re
    config_path = _resolve_config_write_target(repo_path)
    example_path = os.path.join(repo_path, "config.toml.example")

    # Start from example if config.toml doesn't exist yet
    if not os.path.exists(config_path) and os.path.exists(example_path):
        import shutil
        shutil.copy(example_path, config_path)

    profile_defaults = {
        "server": {
            "backend": "postgres",
            "rate_limit_storage": "redis://localhost:6379/1",
            "graeae_mode_default": "auto",
            "log_level": "INFO",
            "compression_workers": 4,
        },
        "edge": {
            "backend": "sqlite",
            "rate_limit_storage": "memory://",
            "graeae_mode_default": "single",
            "log_level": "INFO",
            "compression_workers": 1,
        },
        "dev": {
            "backend": "sqlite",
            "rate_limit_storage": "memory://",
            "graeae_mode_default": "auto",
            "log_level": "DEBUG",
            "compression_workers": 1,
        },
    }[cfg.profile]

    if not os.path.exists(config_path):
        # No example either — write a minimal config
        content = _render_minimal_config(cfg, profile_defaults)
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

    def _toml_value(value):
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _set(section_active, key, value):
        """Replace or append key = ... inside a TOML section.

        Codex round-3 HIGH on #150: the previous regex used
        ``[^\\[]*?`` for the section span, which mis-detects ANY ``[``
        (e.g. in an IPv6 URL value ``"http://[::1]:5002"`` or a
        comment containing brackets) as a section boundary. The fix
        anchors section boundaries to line starts: ``(?:^|\\n)\\[``
        marks a section header line, and the lookahead
        ``(?:(?!\\n\\[).)*?`` allows any character as long as it's
        not a newline-prefixed ``[`` (i.e. mid-line brackets are
        fine; only line-anchored next-section headers terminate the
        span). This affects ALL fields written by the installer, not
        just internal_audit_token.

        Codex round-4 HIGH: the *key* match also has to be line-
        anchored. Without it, a commented-out ``# key = "..."`` line
        mid-section satisfies the regex (lazy ``.*?`` consumes ``# ``,
        then the key matches the rest of the comment), the rewrite
        edits inside the comment, ``re.subn`` returns ``n=1``, and
        the append path never runs. The patched config parses as
        valid TOML but has NO actual key — so e.g.
        ``/v1/internal/mcp_audit`` stays in legacy mode after a
        “successful” install.

        Codex round-5 HIGH: a bare ``(?<=\\n)`` lookbehind regresses
        valid INDENTED TOML keys (TOML allows leading whitespace).
        ``[server]\\n  internal_audit_token = "old"`` would not match
        (char before key is space, not ``\\n``); the append path
        would then add an unindented duplicate, and tomllib would
        reject the result with "Cannot overwrite a value." The
        correct anchor consumes ``\\n[ \\t]*`` (newline + optional
        indent), which preserves the indent in group 1 for the
        rewrite while still rejecting comment lines (where
        ``\\n[ \\t]*`` is followed by ``#``, not the key).
        """
        nonlocal content
        quoted = _toml_value(value)
        # Line-anchored section header + content up to (but not
        # including) the next line-start `[`. The key MUST be at the
        # start of a line (after `\n` + optional indent) so
        # commented-out lines don't satisfy the regex AND indented
        # TOML keys are preserved with their indent. The header
        # match deliberately does NOT consume the trailing newline —
        # that way the lazy span can be empty and `\n[ \t]*key` can
        # still match the very first key right after the header.
        pattern = (
            rf'((?:^|\n)\[{section_active}\][^\n]*'
            rf'(?:(?!\n\[).)*?'
            rf'\n[ \t]*)'
            rf'({re.escape(key)}\s*=\s*[^\n]*)'
        )
        content, n = re.subn(
            pattern,
            lambda match: f"{match.group(1)}{key} = {quoted}",
            content,
            flags=re.DOTALL,
        )
        if n == 0:
            # Section header must appear at line start (defensive: don't
            # treat `something[server]something` mid-line as a section).
            section_header_pattern = rf'(?:^|\n)\[{section_active}\]'
            if not re.search(section_header_pattern, content):
                if not content.endswith("\n"):
                    content += "\n"
                content += f"\n[{section_active}]\n{key} = {quoted}\n"
                return
            # Append the key+value at the end of the section: insert
            # before the next line-start `[` (or at the end of file).
            append_pattern = (
                rf'((?:^|\n)\[{section_active}\][^\n]*\n'
                rf'(?:(?!\n\[).)*)'
            )
            content = re.sub(
                append_pattern,
                lambda match: f"{match.group(1).rstrip()}\n{key} = {quoted}",
                content,
                count=1,
                flags=re.DOTALL,
            )

    _set("server", "profile", cfg.profile)
    _set("server", "port", cfg.listen_port)
    _set("server", "internal_audit_token", _resolve_internal_audit_token(content))
    _set("deployment", "profile", cfg.profile)
    _set("api", "port", cfg.listen_port)
    _set("database", "backend", profile_defaults["backend"])
    if _profile_uses_sqlite(cfg.profile):
        _set("database", "sqlite_path", cfg.sqlite_path)
    else:
        _set("database", "host", cfg.db_host)
        _set("database", "port", cfg.db_port)
        _set("database", "database", cfg.db_name)
        _set("database", "user", cfg.db_user)
        _set("database", "password", cfg.db_password)
    _set("database", "embedding_dim", cfg.embedding_dim)
    _set("rate_limit", "storage_uri", profile_defaults["rate_limit_storage"])
    _set("graeae", "mode_default", profile_defaults["graeae_mode_default"])
    _set("logging", "level", profile_defaults["log_level"])
    _set("compression", "workers", profile_defaults["compression_workers"])
    if cfg.profile == "dev":
        _set("runtime", "loose_timeouts", True)

    # Codex round-3 HIGH on #150: post-patch invariant — the result
    # must be parseable TOML. Without this, a regex slip on a future
    # field could land malformed content, and the operator would only
    # discover it when the service fails to start. Fail loudly here
    # with the original content preserved.
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover — py<3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        parsed = tomllib.loads(content)
    except Exception as exc:
        raise RuntimeError(
            f"[installer] ERROR: patched config.toml failed TOML parse: {exc}. "
            f"Refusing to write {config_path}; the existing file is unchanged. "
            f"This is a bug in the installer's _set() patcher — please report."
        ) from exc

    # Codex round-4 HIGH on #150: ensure the audit token actually
    # landed as a key (not as a comment edit). If a previous regex
    # slip left only a `# internal_audit_token = "..."` comment, the
    # parsed [server] table would have no key — the service would
    # see the field as the default empty string and
    # /v1/internal/mcp_audit would stay in legacy mode despite a
    # "successful" install.
    server_table = parsed.get("server", {}) if isinstance(parsed, dict) else {}
    if not isinstance(server_table, dict) or not server_table.get(
        "internal_audit_token"
    ):
        raise RuntimeError(
            f"[installer] ERROR: patched config.toml has no live "
            f"[server].internal_audit_token (only a comment, perhaps?). "
            f"Refusing to write {config_path}; the existing file is unchanged. "
            f"This is a bug in the installer's _set() patcher — please report."
        )

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


def _render_minimal_config(cfg, profile_defaults: dict) -> str:
    sqlite = _profile_uses_sqlite(cfg.profile)
    audit_token = _resolve_internal_audit_token(None)
    lines = [
        "[server]",
        f'profile = "{cfg.profile}"',
        f"port = {cfg.listen_port}",
        f'internal_audit_token = "{audit_token}"',
        "",
        "[deployment]",
        f'profile = "{cfg.profile}"',
        "",
        "[database]",
        f'backend = "{profile_defaults["backend"]}"',
    ]
    if sqlite:
        lines.append(f'sqlite_path = "{cfg.sqlite_path}"')
    else:
        lines.extend(
            [
                f'host = "{cfg.db_host}"',
                f"port = {cfg.db_port}",
                f'database = "{cfg.db_name}"',
                f'user = "{cfg.db_user}"',
                f'password = "{cfg.db_password}"',
            ]
        )
    lines.append(f"embedding_dim = {cfg.embedding_dim}")
    lines.extend(
        [
            "",
            "[api]",
            f"port = {cfg.listen_port}",
            "",
            "[rate_limit]",
            f'storage_uri = "{profile_defaults["rate_limit_storage"]}"',
            "",
            "[graeae]",
            f'mode_default = "{profile_defaults["graeae_mode_default"]}"',
            "",
            "[logging]",
            f'level = "{profile_defaults["log_level"]}"',
            "",
            "[compression]",
            f"workers = {profile_defaults['compression_workers']}",
        ]
    )
    if cfg.profile == "dev":
        lines.extend(["", "[runtime]", "loose_timeouts = true"])
    return "\n".join(lines) + "\n"


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
    if args.profile:
        # CLI --profile is an operator-explicit override. Round-31
        # HIGH: setting only MNEMOS_PROFILE put it BELOW TOML
        # [server].profile in the resolution order, so a stale
        # `profile = "edge"` in an old config.toml shadowed the CLI
        # flag during --upgrade and routed dispatch to sqlite while
        # the operator clearly asked for server. Set
        # MNEMOS_PROFILE_OVERRIDE (highest-priority source in
        # runtime _profile_from_sources + installer
        # _load_existing_config) so CLI ALWAYS wins. Also keep
        # MNEMOS_PROFILE for backward compat with code paths that
        # only consult that name.
        os.environ["MNEMOS_PROFILE_OVERRIDE"] = args.profile
        os.environ["MNEMOS_PROFILE"] = args.profile

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
        from .db import run_migrations, setup_sqlite_database

        # Load existing config from environment or config.toml
        cfg = _load_existing_config(repo_path)
        if cfg is None:
            print(
                "ERROR: --upgrade requires existing config. "
                "Set MNEMOS_DB_* env vars or ensure config.toml exists.",
                file=sys.stderr,
            )
            return 1

        # Round-19 codex finding: dispatch by backend so SQLite configs
        # don't reach run_migrations() (postgres-only). Without this,
        # an edge/dev SQLite config with default db_host=localhost +
        # db_port=5432 passes the round-18 unconditional locality
        # guards and then run_migrations applies postgres SQL files
        # against any local mnemos database that happens to exist.
        # The round-18 guards close the wrong-cluster mutation case
        # for postgres-shaped configs; this dispatch closes the
        # SQLite-config-routes-to-postgres-runner case.
        #
        # We also reject DSN/url-based configs explicitly: runtime
        # supports DATABASE_URL/[database].url, but the migration
        # runner is not DSN-aware. Failing closed is safer than
        # silently migrating localhost defaults.
        if _has_dsn_config(repo_path):
            print(
                "ERROR: --upgrade does not support DSN/url-based "
                "configs (DATABASE_URL, MNEMOS_DATABASE_URL, or "
                "[database].url). The migration runner is not "
                "DSN-aware and would silently migrate localhost "
                "defaults instead of the DSN target. Run --upgrade "
                "on the host that owns the DB with explicit "
                "MNEMOS_DB_HOST/PG_HOST + companions, or use a "
                "DSN-aware migration tool.",
                file=sys.stderr,
            )
            return 1

        # Round-20: dispatch by RUNTIME BACKEND, not by profile.
        # Profile is a deployment-shape signal that can diverge from the
        # actual DB target — an operator can set MNEMOS_PROFILE=dev (or
        # have legacy "personal") while [database].backend=postgres in
        # config.toml. Profile-based dispatch sent that to
        # setup_sqlite_database while the running service kept using
        # postgres + skipped migrations.
        runtime_backend = _resolve_runtime_backend(cfg, repo_path)
        print(f"Running database migrations (backend={runtime_backend})...")
        if runtime_backend == "sqlite":
            # SQLite migrations are applied lazily inside SqliteBackend.open(),
            # which setup_sqlite_database invokes. Idempotent — same code path
            # as the install case, safe to re-run on --upgrade.
            ok = setup_sqlite_database(cfg)
        else:
            ok = run_migrations(cfg)
        if not ok:
            print("Some migrations failed.", file=sys.stderr)
            return 1

        # Persist the (possibly env-overridden) embedding_dim back to
        # config.toml + the managed service env file. The DB schema may
        # already be at vector(<new dim>) after run_migrations() returned;
        # if we can't update config to match, the operator's automation
        # would treat the upgrade as complete while runtime would still
        # send old-dim vectors at the new-dim column. Make persistence
        # FATAL — fail closed if we can't durably record what the schema
        # now looks like.
        #
        # IMPORTANT: do NOT call _write_config_toml() on --upgrade. That
        # helper rewrites profile-derived defaults (backend, rate_limit
        # storage, graeae mode, logging, compression workers) and would
        # clobber any production settings the operator has tuned in
        # config.toml. Use the surgical _patch_config_toml_embedding_dim()
        # which touches only [database].embedding_dim.
        # Round-23: post-migration persistence MUST patch the same
        # file the runtime will read. Without this, --upgrade alters
        # the DB schema then patches the stale repo config while the
        # service continues reading the production MNEMOS_CONFIG_PATH
        # config with the old embedding_dim → runtime/schema dim
        # mismatch.
        config_toml_path = (
            _resolve_runtime_config_path(repo_path)
            or os.path.join(repo_path, "config.toml")
        )
        if os.path.exists(config_toml_path):
            if not _patch_config_toml_embedding_dim(config_toml_path, cfg.embedding_dim):
                print(
                    f"[installer] ERROR failed to update [database].embedding_dim "
                    f"in {config_toml_path}. DB schema may already be at "
                    f"vector({cfg.embedding_dim}) but config.toml was NOT updated. "
                    f"Edit {config_toml_path} manually to set "
                    f"[database].embedding_dim = {cfg.embedding_dim}, then restart "
                    f"the service. Refusing to report success.",
                    file=sys.stderr,
                )
                return 1
            # Verify the value actually round-tripped — defensive against
            # a silent partial-write or filesystem drift.
            if not _verify_config_toml_embedding_dim(config_toml_path, cfg.embedding_dim):
                print(
                    f"[installer] ERROR config.toml at {config_toml_path} does "
                    f"not reflect [database].embedding_dim = {cfg.embedding_dim} "
                    f"after the post-migration write. Inspect the file and update "
                    f"manually before reporting upgrade success.",
                    file=sys.stderr,
                )
                return 1
            print(f"[installer] Refreshed [database].embedding_dim in {config_toml_path}.")

            # #151: bring legacy v5.3.4-era installs up to the #150
            # default-on audit-token posture. v5.3.4 added the env
            # var (#148) but operators rarely set it manually, so a
            # tokenless config.toml left /v1/internal/mcp_audit in
            # legacy mode after an --upgrade. Surgical patch inserts
            # `internal_audit_token = <hex>` if missing/empty;
            # otherwise a no-op (preserves operator-set or
            # previously-installed values). Failure is a soft
            # warning, not a fatal upgrade error — the upgrade itself
            # already succeeded, and operators can hand-edit later.
            if not _patch_config_toml_internal_audit_token(config_toml_path):
                print(
                    f"[installer] WARNING could not autogen "
                    f"[server].internal_audit_token in {config_toml_path}. "
                    f"/v1/internal/mcp_audit will operate in legacy "
                    f"mode (any authenticated caller can POST audit "
                    f"rows). Hand-edit the file to add a 256-bit hex "
                    f"token if you want the lockdown engaged.",
                    file=sys.stderr,
                )
        else:
            # Env-only / container-shaped install with no config.toml on
            # disk. Verify the env var that drove this upgrade is still in
            # scope so the operator's restart picks up the same dim.
            #
            # Round-27 MEDIUM: an absent MNEMOS_EMBEDDING_DIM is
            # acceptable when cfg.embedding_dim equals the runtime
            # default (768). Previously we required env presence even
            # for the default, so a successful migration on a no-config
            # default-dim install would exit 1 just because the
            # operator hadn't redundantly set MNEMOS_EMBEDDING_DIM=768.
            # That left automation seeing a failure after DB changes
            # had already run.
            env_dim_raw = os.environ.get("MNEMOS_EMBEDDING_DIM") or os.environ.get(
                "PG_EMBEDDING_DIM"
            )
            try:
                env_dim = int(env_dim_raw) if env_dim_raw else None
            except (TypeError, ValueError):
                env_dim = None

            # Default dim (768) is fine without an explicit env var —
            # runtime falls back to the same default.
            DEFAULT_EMBEDDING_DIM = 768
            implicit_default_ok = (
                env_dim is None and cfg.embedding_dim == DEFAULT_EMBEDDING_DIM
            )
            if env_dim is not None and env_dim != cfg.embedding_dim:
                print(
                    f"[installer] ERROR no config.toml at {config_toml_path} and "
                    f"MNEMOS_EMBEDDING_DIM in environment ({env_dim_raw!r}) "
                    f"does not match the upgrade target ({cfg.embedding_dim}). "
                    f"DB schema may already be at vector({cfg.embedding_dim}). "
                    f"Persist MNEMOS_EMBEDDING_DIM={cfg.embedding_dim} in your "
                    f"container env / launcher and re-run. Refusing to report "
                    f"success.",
                    file=sys.stderr,
                )
                return 1
            if (not implicit_default_ok) and env_dim is None:
                # Non-default dim with no env var to durably record it —
                # operator must set it explicitly so service restart sees
                # the right dim.
                print(
                    f"[installer] ERROR no config.toml at {config_toml_path} and "
                    f"non-default embedding_dim={cfg.embedding_dim} requires an "
                    f"explicit env var. Set "
                    f"MNEMOS_EMBEDDING_DIM={cfg.embedding_dim} in your "
                    f"container env / launcher and re-run. Refusing to report "
                    f"success.",
                    file=sys.stderr,
                )
                return 1
            if implicit_default_ok:
                print(
                    f"[installer] No config.toml; relying on default "
                    f"embedding_dim={DEFAULT_EMBEDDING_DIM} for runtime "
                    f"(no env override needed)."
                )
            else:
                print(
                    f"[installer] No config.toml; relying on env "
                    f"MNEMOS_EMBEDDING_DIM={cfg.embedding_dim} for runtime."
                )
        # Only patch the systemd EnvironmentFile if it actually exists.
        # Config-based --upgrade can't tell the difference between a
        # systemd-managed install, a no-service install, or a non-Linux
        # install — config.toml doesn't persist cfg.create_service. Some
        # operators set MNEMOS_NO_SERVICE_ENV=1 to skip even this check
        # for known no-service deployments.
        env_path = "/etc/mnemos/mnemos.env"
        skip_env = os.environ.get("MNEMOS_NO_SERVICE_ENV", "").strip().lower() in {
            "1", "true", "yes",
        }
        if skip_env:
            print(
                "[installer] Skipping systemd env file patch "
                "(MNEMOS_NO_SERVICE_ENV set)."
            )
        elif not os.path.exists(env_path):
            print(
                f"[installer] No systemd env file at {env_path}; skipping "
                f"env patch. (Likely a no-service or non-Linux install. If "
                f"this is unexpected, set MNEMOS_EMBEDDING_DIM=" + str(cfg.embedding_dim) + " "
                f"manually in your service launcher before restart.)"
            )
        else:
            # Surgical patch — preserves existing OPENAI_API_KEY,
            # ANTHROPIC_API_KEY, etc. that _load_existing_config() doesn't
            # repopulate. The full _write_env_file() would rebuild from
            # cfg.graeae_providers={} and silently erase those keys.
            env_ok = _patch_service_env_embedding_dim(env_path, cfg.embedding_dim)
            if not env_ok:
                print(
                    f"[installer] ERROR could not patch {env_path}. DB at "
                    f"vector({cfg.embedding_dim}) but the systemd env file "
                    f"was NOT updated. Set "
                    f"MNEMOS_EMBEDDING_DIM={cfg.embedding_dim} in that file "
                    f"manually before restarting the service. Refusing to "
                    f"report success.",
                    file=sys.stderr,
                )
                return 1
            print(f"[installer] Patched MNEMOS_EMBEDDING_DIM in {env_path}.")

        print("Migrations complete.")
        return 0

    # ------------------------------------------------------------------ #
    # Step 5: Obtain config
    # ------------------------------------------------------------------ #
    cfg = None

    if args.unattended:
        print("Running in unattended mode (reading config from environment)...")
        cfg = _config_from_env()
        if args.profile:
            cfg.profile = args.profile

    elif args.wizard:
        from .wizard import run_wizard
        cfg = run_wizard(info, selected_profile=args.profile)
        _apply_embedding_dim_from_env(cfg)

    else:
        # Default: --agent (try LLM-guided, fall back to wizard)
        try:
            from .agent import run_agent
            cfg = run_agent(info)
            if args.profile:
                cfg.profile = args.profile
            _apply_embedding_dim_from_env(cfg)
        except (ImportError, ModuleNotFoundError):
            print("[installer] agent module not available — falling back to wizard.")
            from .wizard import run_wizard
            cfg = run_wizard(info, selected_profile=args.profile)
            _apply_embedding_dim_from_env(cfg)
        except Exception as exc:
            print(f"[installer] Agent error ({exc}) — falling back to wizard.")
            from .wizard import run_wizard
            cfg = run_wizard(info, selected_profile=args.profile)
            _apply_embedding_dim_from_env(cfg)

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
    from .db import create_api_key, run_migrations, setup_database, setup_sqlite_database, verify_connection

    if _profile_uses_sqlite(cfg.profile):
        print("\n[installer] Initializing SQLite database...")
        ok = setup_sqlite_database(cfg)
        if not ok:
            print("ERROR: SQLite setup failed.", file=sys.stderr)
            return 1
    elif cfg.create_new_db:
        print("\n[installer] Setting up database...")
        ok = setup_database(cfg, info)
        if not ok:
            print("ERROR: Database setup failed.", file=sys.stderr)
            return 1

    if not _profile_uses_sqlite(cfg.profile):
        print("\n[installer] Running migrations...")
        ok = run_migrations(cfg)
        if not ok:
            # Migration failures (including embed-dim ALTER refusals on a
            # populated mismatched DB) MUST abort the install — otherwise
            # the installer goes on to write config.toml + service env
            # files at the configured embedding_dim, leaving runtime state
            # mismatched with the (still-old) DB schema.
            print(
                "ERROR: Postgres migrations failed (or refused to proceed).",
                file=sys.stderr,
            )
            print(
                "Refer to the [db] error above for the exact migration / "
                "recovery commands. Once the DB is in the expected state, "
                "re-run the installer.",
                file=sys.stderr,
            )
            return 1

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
    """Load existing config for --upgrade.

    Precedence (config-first, env overlays explicit values only):
      1. config.toml provides the baseline if it exists.
      2. Specific env vars override fields the operator explicitly set:
         MNEMOS_DB_PASSWORD (rotated out-of-band) and MNEMOS_EMBEDDING_DIM
         (operator-driven model swap). Other env vars are ignored — the
         persisted config.toml is the source of truth for stable settings.
      3. If config.toml doesn't exist, fall back to the env-only path
         (`_config_from_env()`). That's the fresh-install/unattended shape.

    Why config-first: a deployment that keeps the DB password in env but
    stores embedding_dim=512 in config.toml must NOT lose that 512 just
    because MNEMOS_DB_PASSWORD is set. Env-first short-circuited the
    config.toml read and silently downgraded embedding_dim to 768 on
    --upgrade, breaking the postgres ALTER guard.
    """
    import os

    from .wizard import Config

    # Honor MNEMOS_CONFIG_PATH first (round-22 HIGH). The runtime
    # config loader checks that path before falling back to repo/cwd/
    # /etc/mnemos. Hardcoding repo_path/config.toml here mutated a
    # stale repo config while the running service kept reading the
    # MNEMOS_CONFIG_PATH target.
    resolved = _resolve_runtime_config_path(repo_path)
    config_path = resolved if resolved else os.path.join(repo_path, "config.toml")
    if not os.path.exists(config_path):
        # No persisted config — use env-only with RUNTIME PARITY.
        # Round-25 HIGH: prior fallback called _config_from_env()
        # which accepts MNEMOS_DB_* as authoritative. Runtime ignores
        # those aliases — _DatabaseSettings uses env_prefix='PG_' only
        # for host/port/database/user. So a container --upgrade with
        # stale MNEMOS_DB_NAME could migrate one DB while the service
        # starts on another. The runtime-parity loader uses PG_* only.
        #
        # Round-33 MEDIUM: trigger the loader on ANY runtime-supported
        # upgrade signal, not just password. Runtime selects postgres
        # from profile override/env, explicit backend env, PG_HOST/
        # PORT/DATABASE/USER, DSN/url envs — even without a password
        # (peer auth, sudo psql in setup_database, etc.). Requiring
        # PG_PASSWORD made round-32's override parity fix unreachable
        # for those passwordless shapes.
        runtime_upgrade_signals = (
            # Profile signals (any non-empty value triggers).
            "MNEMOS_PROFILE_OVERRIDE", "MNEMOS_PROFILE",
            # Backend env aliases.
            "MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND",
            # DSN/url.
            "MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL",
            "MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN",
            # PG_* connection fields (runtime-recognized aliases).
            "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD",
            # MNEMOS_DB_PASSWORD (installer alias for PG_PASSWORD).
            "MNEMOS_DB_PASSWORD",
        )
        if any(os.environ.get(k) for k in runtime_upgrade_signals):
            return _config_from_env_runtime_parity()
        return None

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
    server = data.get("server", {})
    deployment = data.get("deployment", {})
    db = data.get("database", {})
    # Profile resolution. The legacy default ("personal" -> "edge") used to
    # apply unconditionally when no [server]/[deployment] profile was set.
    # That silently rewrote a postgres-backed config to sqlite on the next
    # _write_config_toml call (since edge/dev profiles imply sqlite).
    # Infer from [database].backend / explicit postgres fields when no
    # profile is set so a config that's been running on postgres stays on
    # postgres through --upgrade.
    # Profile resolution mirrors runtime _profile_from_sources +
    # _ServerSettings.profile (validation_alias=MNEMOS_PROFILE):
    #   1. MNEMOS_PROFILE_OVERRIDE env (runtime override)
    #   2. [server].profile TOML
    #   3. [deployment].profile TOML
    #   4. MNEMOS_PROFILE env (runtime _ServerSettings default)
    #   5. backend/field inference (installer-specific)
    #   6. legacy "personal" → "edge"
    #
    # Round-30 HIGH: previously skipped step 4. A profile-less config
    # with `--profile server` (which sets MNEMOS_PROFILE=server) or a
    # bare MNEMOS_PROFILE=server in the upgrade env had cfg.profile
    # resolve to edge, dispatching to setup_sqlite_database while
    # runtime correctly selected server/postgres.
    profile_override = os.environ.get("MNEMOS_PROFILE_OVERRIDE", "").strip()
    raw_profile = (
        profile_override
        or server.get("profile")
        or deployment.get("profile")
    )
    if not raw_profile:
        env_profile = os.environ.get("MNEMOS_PROFILE", "").strip()
        if env_profile:
            raw_profile = env_profile
    if raw_profile:
        cfg.profile = _canonical_profile(raw_profile)
    else:
        # Round-36 HIGH: when an explicit env backend is set
        # (including PG_BACKEND=auto), the TOML [database].backend
        # value is shadowed at runtime — _resolve_runtime_backend
        # already skips it. We must NOT use TOML backend to infer
        # cfg.profile here either, otherwise PG_BACKEND=auto +
        # profile-less TOML backend=postgres would set
        # cfg.profile=server and the resolver's profile fallback
        # (after env shadowing) would return postgres while runtime
        # selected edge/sqlite via auto+default fallback.
        # Round-44/45: backend uses validation_alias so env wins over
        # TOML. When env is unset → use TOML. When env has explicit
        # postgres/sqlite value → use env. When env is 'auto' or
        # empty → shadow TOML and fall through to no-signal (round-45:
        # 'auto' fully replaces TOML in pydantic-settings, so we must
        # NOT fall back to TOML).
        env_backend_value: str | None = None
        env_backend_present = False
        for alias in ("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"):
            if alias in os.environ:
                env_backend_present = True
                env_backend_value = os.environ[alias].strip().lower()
                break
        if env_backend_present:
            # Env shadows TOML — even when 'auto' or empty.
            if env_backend_value in ("postgres", "postgresql", "pg",
                                     "sqlite", "sqlite3"):
                backend = env_backend_value
            else:
                # 'auto' or '' → no backend signal from this layer.
                backend = ""
        else:
            backend = (db.get("backend") or "").strip().lower()
        # Round-28 HIGH: `password` is NOT a postgres-signal field per
        # runtime _has_explicit_postgres_connection_config. Counting it
        # here let a [database] section with only a password (rotated
        # via env) infer profile=server, which then routed --upgrade
        # to run_migrations against local default postgres while
        # runtime stayed on edge/sqlite.
        has_pg_fields = any(
            db.get(field) for field in ("host", "port", "database", "user")
        ) and not _looks_like_default_postgres(db)
        # Round-28 MEDIUM: accept the `sqlite3` alias, matching
        # runtime's _normalize_backend_name normalization.
        if backend in ("postgres", "postgresql", "pg") or has_pg_fields:
            cfg.profile = "server"
        elif backend in ("sqlite", "sqlite3"):
            cfg.profile = _canonical_profile("personal")  # edge
        else:
            cfg.profile = _canonical_profile("personal")  # legacy default → edge
    cfg.sqlite_path = db.get("sqlite_path", "~/.mnemos/mnemos.db")
    # Empty-string TOML fields fall through to env (mirrors the runtime
    # contract in _build_settings: documented production shape is
    # `field = ""` with the actual value supplied via PG_* env). Without
    # this parity, a config with `database = ""` + PG_DATABASE set would
    # have the runtime correctly using PG_DATABASE while --upgrade
    # targeted an empty cfg.db_name.
    def _config_or_env(toml_value, env_keys: tuple[str, ...], default: str) -> str:
        # Round-38 MEDIUM: distinguish "absent" (None / empty string)
        # from "explicit zero/falsy" (e.g. port = 0). Truthy `if
        # toml_value:` previously turned an explicit port=0 into the
        # default 5432, letting --upgrade run migrations against the
        # default-port cluster while runtime kept the explicit 0
        # value (and would refuse to start). Use is-None/empty-string
        # check so explicit zero values pass through and fail the
        # downstream port guard.
        if toml_value is not None and not (isinstance(toml_value, str) and toml_value == ""):
            return str(toml_value)
        for key in env_keys:
            v = os.environ.get(key)
            # Same shape: only empty string falls through to default.
            if v is not None and v != "":
                return v
        return default

    # Round-24 HIGH: DB connection field overlays must mirror runtime.
    # Runtime _DatabaseSettings uses env_prefix='PG_' exclusively for
    # host/port/database/user — MNEMOS_DB_* env vars have no effect on
    # runtime selection. If the upgrade overlay accepts MNEMOS_DB_*,
    # operators can run --upgrade with MNEMOS_DB_HOST=10.0.0.5 against
    # a config.toml with empty host, target a different DB during
    # migration, then the service starts on the original DB (PG_*-
    # backed or default).
    #
    # Password is the documented exception: production env file shape
    # is `password = ""` in config.toml + PG_PASSWORD in the systemd
    # env file. MNEMOS_DB_PASSWORD remains accepted as the installer
    # convention for unattended installs (both flow into PG_PASSWORD
    # via _config_from_env), so operators rotating creds via either
    # name still work.
    def _resolve_db_field_strict(
        toml_value, env_keys: tuple[str, ...], default: str, field_name: str,
    ) -> str:
        """Resolve a DB connection field mirroring runtime priority.

        Runtime _DatabaseSettings receives sanitized TOML as init
        kwargs; pydantic-settings init kwargs WIN over env. So a
        non-empty TOML value beats the same-field PG_* env alias.

        Round-43 HIGH: corrected priority. Previous round-41 version
        checked env first; that diverged from runtime, letting a
        stale PG_HOST in operator's shell retarget --upgrade away
        from the persisted TOML host.

        Round-47 HIGH: also reject whitespace-only TOML values —
        they pass through runtime as explicit broken targets, but
        the locality guard's strip() turned them into local. Fail
        closed at the loader so the operator gets a clear error.

        Order:
        1. Non-empty TOML value wins (whitespace-only fails closed).
        2. Otherwise, PG_* env: presence wins (round-41), explicit
           empty/whitespace fails closed.
        3. Otherwise, default.
        """
        if toml_value is not None and not (isinstance(toml_value, str) and toml_value == ""):
            v = str(toml_value)
            # Round-47/48: reject whitespace-only AND padded values
            # (anything where `v != v.strip()`). Runtime passes the
            # raw value through; padded localhost like " localhost "
            # is an explicit broken target, not local.
            if isinstance(toml_value, str):
                if v.strip() == "":
                    raise ValueError(
                        f"Invalid {field_name}: TOML value {toml_value!r} "
                        f"is whitespace-only. Runtime would pass this "
                        f"through to the connection unchanged and fail. "
                        f"Set the field to a non-empty value or remove "
                        f"it to fall through to env/default."
                    )
                if v != v.strip():
                    raise ValueError(
                        f"Invalid {field_name}: TOML value {toml_value!r} "
                        f"has leading/trailing whitespace. Runtime "
                        f"passes the raw value to the connection — "
                        f"trim the whitespace or fix the typo."
                    )
            return v
        for key in env_keys:
            if key in os.environ:
                v = os.environ[key]
                if v == "":
                    raise ValueError(
                        f"Invalid {field_name}: env {key}='' is "
                        f"explicit empty. Runtime would reject this "
                        f"as a validation error. Set {key} to a "
                        f"non-empty value or unset it to fall through "
                        f"to config.toml/default. Refusing to dispatch "
                        f"migrations under a value runtime would reject."
                    )
                # Round-47: reject whitespace-only env values.
                if v.strip() == "":
                    raise ValueError(
                        f"Invalid {field_name}: env {key}={v!r} is "
                        f"whitespace-only. Runtime would pass this "
                        f"through unchanged and fail. Set {key} to a "
                        f"non-empty value or unset it."
                    )
                # Round-48: reject padded env values (e.g. " localhost ").
                if v != v.strip():
                    raise ValueError(
                        f"Invalid {field_name}: env {key}={v!r} has "
                        f"leading/trailing whitespace. Runtime passes "
                        f"the raw value to the connection — trim the "
                        f"whitespace or fix the typo."
                    )
                return v
        return default

    cfg.db_host = _resolve_db_field_strict(
        db.get("host"), ("PG_HOST",), "localhost", "Postgres host"
    )
    # Round-43 HIGH: TOML wins over env (matches runtime init-kwargs
    # > env order). Non-empty TOML port wins; only when TOML port is
    # missing/empty does PG_PORT take over. Round-40 fail-closed
    # contract still applies: explicit empty PG_PORT or malformed
    # value raises ValueError.
    raw_port: str | None = None
    toml_port = db.get("port")
    if toml_port is not None and not (
        isinstance(toml_port, str) and toml_port == ""
    ):
        raw_port = str(toml_port)
    elif "PG_PORT" in os.environ:
        # No explicit TOML port — env wins (presence-based, empty fails).
        raw_port = os.environ["PG_PORT"]
    else:
        raw_port = "5432"
    try:
        cfg.db_port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid Postgres port {raw_port!r}: {exc}. Set "
            f"[database].port in config.toml or PG_PORT env to a "
            f"valid integer (typically 5432). Refusing to dispatch "
            f"migrations under a port runtime would reject."
        )
    # Key is "database" (matches runtime _DatabaseSettings.database).
    # Round-29 HIGH: dropped the legacy `db.get("name")` fallback —
    # runtime ignores extra TOML keys, so a config with `name = "old_db"`
    # plus empty/missing `database` would have --upgrade target old_db
    # while runtime connects to PG_DATABASE/default. Schema/embedding
    # ALTERs would land on the wrong DB.
    cfg.db_name = _resolve_db_field_strict(
        db.get("database"),
        ("PG_DATABASE",),
        "mnemos",
        "Postgres database name",
    )
    cfg.db_user = _resolve_db_field_strict(
        db.get("user"), ("PG_USER",), "mnemos_user", "Postgres user"
    )
    # Password overlay accepts both MNEMOS_DB_PASSWORD installer convention
    # and PG_PASSWORD (documented production shape with `password = ""`).
    # Both set the same runtime password (MNEMOS_DB_PASSWORD is mapped
    # to PG_PASSWORD by the installer's env writer), so accepting both
    # here is parity-preserving.
    cfg.db_password = _config_or_env(
        db.get("password"), ("MNEMOS_DB_PASSWORD", "PG_PASSWORD"), ""
    )
    # embedding_dim must round-trip through --upgrade. Without this, an
    # existing 512-D install would lose its dim on `--upgrade`, default
    # back to 768, skip _alter_postgres_embedding_dim entirely, and
    # leave config.toml + DB schema mismatched.
    #
    # Round-40 MEDIUM: distinguish absent (default to 768) from present-
    # but-malformed (fail closed). Runtime _DatabaseSettings declares
    # embedding_dim:int and would reject "not-a-number" or "" — the
    # installer must match instead of silently rewriting to 768.
    if "embedding_dim" in db:
        raw_dim = db["embedding_dim"]
        try:
            cfg.embedding_dim = int(raw_dim)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid [database].embedding_dim = {raw_dim!r} in "
                f"config.toml: {exc}. Must be a positive integer "
                f"matching your embedding model (default 768). "
                f"Refusing to dispatch migrations under a value "
                f"runtime would reject."
            )
    else:
        cfg.embedding_dim = 768
    # Env overlay for embedding_dim — operators sometimes drive a model
    # swap via env on `--upgrade`. Apply on top of the config.toml value.
    _apply_embedding_dim_from_env(cfg)
    # Key is under [api], not [server]
    api = data.get("api", data.get("server", {}))
    cfg.listen_port = api.get("port", 5002)
    return cfg


if __name__ == "__main__":
    sys.exit(main())

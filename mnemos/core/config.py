"""
MNEMOS Configuration Module
Centralized settings for PostgreSQL, GRAEAE, and embeddings.

All values are overridden by env vars (documented in .env.example) or config.toml.
Only PG_CONFIG and GRAEAE_CONFIG are imported by application code.
"""

import os

# ============================================================================
# TOML Configuration (config.toml overrides env-var defaults where present)
# ============================================================================

try:
    import tomllib as _tomllib  # noqa: E402
except ModuleNotFoundError:
    import tomli as _tomllib  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _load_toml() -> dict:
    """Load config.toml if present, return empty dict otherwise."""
    toml_path = _Path(__file__).resolve().parents[2] / 'config.toml'
    if toml_path.exists():
        with open(toml_path, 'rb') as _f:
            return _tomllib.load(_f)
    return {}


_TOML = _load_toml()

# ============================================================================
# PostgreSQL Configuration
# Env vars: PG_HOST, PG_PORT, PG_DATABASE, PG_USER, PG_PASSWORD,
#           PG_POOL_MIN, PG_POOL_MAX
# ============================================================================

_db_toml = _TOML.get('database', {})
PG_CONFIG = {
    'host':         os.getenv('PG_HOST',     str(_db_toml.get('host',     'localhost'))),
    'port':         int(os.getenv('PG_PORT', str(_db_toml.get('port',     5432)))),
    'database':     os.getenv('PG_DATABASE', str(_db_toml.get('database', 'mnemos'))),
    'user':         os.getenv('PG_USER',     str(_db_toml.get('user',     'mnemos_user'))),
    'password':     os.getenv('PG_PASSWORD', str(_db_toml.get('password', ''))),  # No default — service will fail loudly if PG_PASSWORD is not set
    'pool_min_size': int(os.getenv('PG_POOL_MIN', str(_db_toml.get('pool_min_size', 5)))),
    'pool_max_size': int(os.getenv('PG_POOL_MAX', str(_db_toml.get('pool_max_size', 20)))),
}

# ============================================================================
# GRAEAE Configuration — provider registry and engine settings
# Sourced from config.toml [graeae]; imported by graeae/engine.py
# ============================================================================

GRAEAE_CONFIG: dict = _TOML.get('graeae', {})

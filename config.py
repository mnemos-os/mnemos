"""
MNEMOS Configuration Module
Centralized settings for PostgreSQL, GRAEAE, and embeddings.

All values are overridden by env vars (documented in .env.example) or config.toml.
Only PG_CONFIG, GRAEAE_CONFIG, and OLLAMA_EMBED_URL are imported by application code.
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
    toml_path = _Path(__file__).parent / 'config.toml'
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
# Embeddings Configuration
#
# Canonical env vars: INFERENCE_EMBED_HOST, INFERENCE_EMBED_MODEL,
#   INFERENCE_EMBED_TIMEOUT.
#
# Backward-compat fallbacks: OLLAMA_EMBED_HOST, OLLAMA_EMBED_MODEL,
#   OLLAMA_EMBED_TIMEOUT (deprecated; removal scheduled for v4.0).
#
# The endpoint is backend-agnostic — `api/lifecycle.py::_get_embedding`
# auto-detects the wire shape and works against llama-server, Ollama,
# vLLM, NVIDIA NIM embedding containers, or any OpenAI-compat
# /v1/embeddings endpoint. The fleet today runs llama-server, not
# Ollama; the legacy prefix was a historical artifact.
#
# These exported names (`OLLAMA_EMBED_HOST`, `OLLAMA_EMBED_URL`) are
# kept identifier-stable for any external script or test that imports
# them. Internally api/lifecycle.py prefers the INFERENCE_EMBED_*
# names. Module-level identifiers will be renamed in v4.0 alongside
# the env-var deprecation.
# ============================================================================

OLLAMA_EMBED_HOST = (
    os.getenv('INFERENCE_EMBED_HOST')
    or os.getenv('OLLAMA_EMBED_HOST')
    or 'http://localhost:11434'
)
OLLAMA_EMBED_URL = f'{OLLAMA_EMBED_HOST}/api/embeddings'

# ============================================================================
# GRAEAE Configuration — provider registry and engine settings
# Sourced from config.toml [graeae]; imported by graeae/engine.py
# ============================================================================

GRAEAE_CONFIG: dict = _TOML.get('graeae', {})

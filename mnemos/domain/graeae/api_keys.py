"""API key loader for GRAEAE providers.

Resolution order (first hit wins per provider):

  1. Standard per-provider environment variables — the widely-accepted
     convention every LLM SDK uses:
        OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
        XAI_API_KEY, GROQ_API_KEY, PERPLEXITY_API_KEY,
        TOGETHER_API_KEY, NVIDIA_API_KEY
  2. Canonical MNEMOS key file (first readable path below):
       $MNEMOS_KEYS_PATH                   — explicit override
       ~/.config/mnemos/api_keys.json      — preferred standard location

File format (MNEMOS-native, self-contained — do not symlink to
third-party service key files):

  {
    "llm_providers": {
      "openai":         { "api_key": "sk-..." },
      "anthropic":      { "api_key": "sk-ant-..." },
      "google_gemini":  { "api_key": "AIza..." },
      "xai":            { "api_key": "xai-..." },
      "groq":           { "api_key": "gsk_..." },
      "perplexity":     { "api_key": "pplx-..." },
      "together_ai":    { "api_key": "tog_..." }
    }
  }

Environment variables win over the file when both are set, so an
operator can override a single provider's key per-process without
rewriting the shared file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from mnemos.core.config import get_settings

logger = logging.getLogger(__name__)

# Canonical names in the key file may differ from GRAEAE provider names.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude-opus": "anthropic",
    "claude":      "anthropic",
    "gemini":      "google_gemini",
}

# Per-provider environment variable fallback. Variables here match the
# conventions used by each vendor's official SDK so an operator can
# drop MNEMOS into an environment where keys are already exported.
_PROVIDER_ENV_VARS: dict[str, str] = {
    "openai":        "OPENAI_API_KEY",
    "anthropic":     "ANTHROPIC_API_KEY",
    "google_gemini": "GEMINI_API_KEY",
    "xai":           "XAI_API_KEY",
    "groq":          "GROQ_API_KEY",
    "perplexity":    "PERPLEXITY_API_KEY",
    "together_ai":   "TOGETHER_API_KEY",
    "nvidia":        "NVIDIA_API_KEY",
}


def _search_paths() -> list[Path]:
    settings = get_settings().providers
    paths: list[Path] = []
    if settings.keys_path is not None:
        paths.append(settings.keys_path.expanduser())
    paths.append(settings.api_keys_file.expanduser())
    return paths


def _find_key_file() -> Path | None:
    for path in _search_paths():
        if path.exists():
            return path
    return None


def load_provider_registry() -> dict:
    """Load the MNEMOS Provider Registry File.

    Returns the `llm_providers` mapping (`{provider_name:
    {api_key: ...}}`) or {} if no file is found / unparseable /
    missing the expected wrapper. Unrecognized top-level shapes are
    rejected loudly so operators know their file is wrong rather
    than silently discovering empty keys at the first request.
    """
    key_file = _find_key_file()
    if key_file is None:
        logger.info(
            "[GRAEAE] no Provider Registry File found in search paths (%s); "
            "falling back to per-provider environment variables "
            "(OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)",
            [str(path) for path in _search_paths()],
        )
        return {}
    try:
        with open(key_file) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(
            f"[GRAEAE] failed to parse Provider Registry File {key_file}: {e}"
        )
        return {}

    if not isinstance(data, dict) or "llm_providers" not in data:
        logger.warning(
            "[GRAEAE] Provider Registry File %s has no 'llm_providers' "
            "wrapper — expected shape is "
            '{"llm_providers": {"<name>": {"api_key": "..."}, ...}}. '
            "Falling back to environment variables.",
            key_file,
        )
        return {}

    nested = data.get("llm_providers")
    if not isinstance(nested, dict):
        logger.warning(
            "[GRAEAE] Provider Registry File %s: llm_providers is not an "
            "object — ignoring", key_file,
        )
        return {}

    logger.debug(
        "[GRAEAE] loaded Provider Registry File from %s (%d providers)",
        key_file, len(nested),
    )
    return nested


# Loaded once at module import; refresh by calling load_provider_registry() again if needed.
_LLM_PROVIDERS: dict = load_provider_registry()


def get_key(provider: str) -> str:
    """Return the api_key for a provider.

    Resolution: environment variable first (per-provider SDK
    convention), then the Provider Registry File. An operator can
    override a single provider's key by exporting its env var
    without rewriting the shared file.
    """
    canonical = _PROVIDER_ALIASES.get(provider, provider)

    env_var = _PROVIDER_ENV_VARS.get(canonical)
    if env_var:
        env_val = get_settings().providers.api_key_for(canonical).strip()
        if env_val:
            return env_val

    return _LLM_PROVIDERS.get(canonical, {}).get("api_key", "")

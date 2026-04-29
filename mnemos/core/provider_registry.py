"""Provider registry constants shared below the domain layer."""
from __future__ import annotations

# Maps each GRAEAE provider name to:
#   registry_provider - name used by provider_sync.py when upserting into
#                       model_registry (may differ from the GRAEAE name,
#                       e.g. claude -> anthropic).
#   prefer            - ordered list of ILIKE patterns to pick a current
#                       flagship when arena_score is absent.
GRAEAE_REGISTRY_MAP: dict[str, dict] = {
    "together": {
        "registry_provider": "together",
        "prefer": ["Qwen3-235B", "Llama-3.3-70B", "Llama-3.1-70B"],
    },
    "groq": {
        "registry_provider": "groq",
        "prefer": ["llama-3.3-70b-versatile", "llama-3.3", "llama-3.1"],
    },
    "openai": {
        "registry_provider": "openai",
        "prefer": ["gpt-5", "gpt-4o", "gpt-4"],
    },
    "claude": {
        "registry_provider": "anthropic",
        "prefer": ["claude-opus", "claude-sonnet"],
    },
    "perplexity": {
        "registry_provider": "perplexity",
        "prefer": ["sonar-pro", "sonar"],
    },
    "xai": {
        "registry_provider": "xai",
        "prefer": ["grok-4", "grok-3", "grok"],
    },
    "nvidia": {
        "registry_provider": "nvidia",
        "prefer": ["llama-3.3-70b-instruct", "llama-3.1-70b-instruct", "nemotron-70b"],
    },
    "gemini": {
        "registry_provider": "gemini",
        "prefer": ["gemini-3", "gemini-2.5", "gemini-2"],
    },
}

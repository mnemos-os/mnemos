from __future__ import annotations

"""
Arena.ai-driven model registry auto-updater for GRAEAE and OpenClaw.

For each provider, fetches the current Arena.ai text leaderboard and identifies
the highest-ranked model from that provider's family. If it differs from what
is currently configured, updates:
  - GRAEAE:   config.toml  [graeae.providers.<name>] model + url fields
  - OpenClaw: openclaw.json models.providers.<name>.models[] (adds new; never removes)

Arena model name → API model ID normalization is provider-specific. Providers
where normalization is ambiguous are listed as CANDIDATES in the log but not
auto-applied (normalizer returns None).

Run via: scripts/update_model_registry.py
"""

import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# ── HuggingFace Datasets Server (same source as elo_sync) ─────────────────
_HF_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=lmarena-ai%2Fleaderboard-dataset"
    "&config=text"
    "&split=latest"
    "&offset=0&limit=300"
)

# ── Per-provider Arena family config ──────────────────────────────────────
# prefix:     substring that identifies this provider's models in Arena
# skip_terms: Arena model name substrings that indicate cheap/fast variants
#             (those belong in OpenClaw's operational default, not GRAEAE)
# normalize:  converts Arena model name → provider API model ID.
#             Return None if normalization is ambiguous → candidate-only (no auto-apply)
# url_fn:     for providers where the URL embeds the model name (e.g. Gemini)

def _xai_norm(name: str) -> str:
    """grok-4.20-beta1 → grok-4.2 ; grok-4.20-beta-0309-reasoning → grok-4.2"""
    n = re.sub(r'(\d+)\.(\d)0+\b', r'\1.\2', name)   # 4.20 → 4.2
    n = re.sub(r'-(beta\d*|beta-[0-9-]+|reasoning|non-reasoning|thinking|multi-agent[^-]*)', '', n)
    return n.rstrip('-')

def _openai_norm(name: str) -> Optional[str]:
    """gpt-5.4-high → gpt-5.4  (strip -high, -mini-high, but not -mini itself)"""
    n = re.sub(r'-(high|chat-latest|20\d{6})$', '', name)
    return n

def _gemini_norm(name: str) -> str:
    """gemini-3.1-pro-preview stays as-is — Arena name == API name."""
    return name

def _gemini_url(model: str) -> str:
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

def _claude_norm(name: str) -> str:
    """claude-opus-4-6-thinking → claude-opus-4-6"""
    return re.sub(r'-(thinking(-\d+k)?)$', '', name)

# Together API IDs use vendor-prefixed capitalization and provider-specific
# suffixes (-tput, -Turbo, -FP8) that can't be derived algorithmically.
# Add entries here when new Together-hosted models appear in the Arena rankings.
# Key: lowercase substring of Arena model name (first match wins)
# Value: exact Together API model ID
_TOGETHER_API_MAP: dict[str, str] = {
    "qwen3-235b-a22b-instruct-2507":    "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    "qwen3-235b-a22b-thinking-2507":    "Qwen/Qwen3-235B-A22B-Thinking-2507",
    "qwen3-235b":                        "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    "llama-4-maverick":                  "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
    "llama-4-scout":                     "meta-llama/Llama-4-Scout-17B-16E-Instruct",
    "llama-3.3-70b":                     "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "deepseek-v3":                       "deepseek-ai/DeepSeek-V3",
    "deepseek-r1":                       "deepseek-ai/DeepSeek-R1",
    "mistral-large":                     "mistralai/Mistral-Large-Instruct-2411",
}

def _together_norm(name: str) -> Optional[str]:
    """Map Arena model name → Together API model ID via explicit lookup table.

    Together uses vendor-prefixed, capitalized IDs with provider-specific suffixes
    (-tput, -Turbo, -FP8) that can't be derived algorithmically. First matching
    entry in _TOGETHER_API_MAP wins. Returns None if model is unknown → candidate only.
    """
    name_lower = name.lower()
    for key, api_id in _TOGETHER_API_MAP.items():
        if key in name_lower:
            return api_id
    return None


_PROVIDER_FAMILIES: dict[str, dict] = {
    # key = GRAEAE provider name
    "xai": {
        "prefix": "grok-",
        "skip_terms": ["-fast", "-instant"],  # fast = cheap operational
        "normalize": _xai_norm,
        "url_fn": None,
        "openclaw_provider": "xai",
        "openclaw_api": "openai-completions",
    },
    "openai": {
        "prefix": "gpt-5",
        "skip_terms": ["-mini", "-nano", "-4o"],
        "normalize": _openai_norm,
        "url_fn": None,
        "openclaw_provider": "openai",
        "openclaw_api": "openai-completions",
    },
    "gemini": {
        "prefix": "gemini-3",
        "skip_terms": ["-flash", "-lite"],
        "normalize": _gemini_norm,
        "url_fn": _gemini_url,
        "openclaw_provider": "google",
        "openclaw_api": "google-generative-ai",
    },
    "claude_opus": {
        "prefix": "claude-opus-4",
        "skip_terms": ["-sonnet", "-haiku"],
        "normalize": _claude_norm,
        "url_fn": None,
        "openclaw_provider": None,  # not in OpenClaw
        "openclaw_api": None,
    },
    "together": {
        "prefix": "qwen3-235",
        "skip_terms": ["-thinking", "-no-thinking"],  # prefer instruct variant
        "normalize": _together_norm,
        "url_fn": None,
        "openclaw_provider": "together",
        "openclaw_api": "openai-completions",
    },
    # nvidia, groq, perplexity: models not in Arena top-100 → weights only, no model update
}


# ── Fetch + parse Arena rows ───────────────────────────────────────────────

def _fetch_arena_rows(timeout: int = 30) -> list[dict]:
    try:
        resp = httpx.get(_HF_ROWS_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        return [entry.get("row", entry) for entry in rows]
    except Exception as exc:
        logger.warning(f"[REGISTRY] arena fetch failed: {exc}")
        return []


def _best_per_family(rows: list[dict]) -> dict[str, tuple[str, float]]:
    """Return {provider_key: (arena_model_name, score)} for the top model per family."""
    best: dict[str, tuple[str, float]] = {}
    for row in rows:
        name = (row.get("model_name") or row.get("model") or "").lower().strip()
        score = float(row.get("rating") or row.get("score") or 0)
        if not name or not score:
            continue
        for provider, fam in _PROVIDER_FAMILIES.items():
            prefix = fam["prefix"]
            skip = fam["skip_terms"]
            if name.startswith(prefix) and not any(s in name for s in skip):
                current_best = best.get(provider)
                if current_best is None or score > current_best[1]:
                    best[provider] = (name, score)
    return best


# ── GRAEAE config.toml update ─────────────────────────────────────────────

def update_graeae_config(
    config_path: Path,
    dry_run: bool = False,
) -> list[str]:
    """Update model names in config.toml from Arena rankings. Returns change log."""
    rows = _fetch_arena_rows()
    if not rows:
        logger.warning("[REGISTRY] no Arena data — GRAEAE config unchanged")
        return []

    best = _best_per_family(rows)
    changes: list[str] = []

    text = config_path.read_text()

    for provider, (arena_name, score) in best.items():
        fam = _PROVIDER_FAMILIES[provider]
        normalize: Callable = fam["normalize"]
        api_id = normalize(arena_name)

        if api_id is None:
            logger.info(
                f"[REGISTRY] {provider:15s} CANDIDATE: Arena top={arena_name!r} "
                f"(score={score:.0f}) — normalization ambiguous, skipping auto-apply"
            )
            continue

        # Find current model in config.toml (simple text search)
        import re as _re
        section_re = _re.compile(
            rf'(\[graeae\.providers\.{provider}\][^\[]*?model\s*=\s*")([^"]+)(")',
            _re.DOTALL,
        )
        m = section_re.search(text)
        if not m:
            logger.debug(f"[REGISTRY] {provider}: not found in config.toml — skipping")
            continue

        current_model = m.group(2)
        if current_model == api_id:
            logger.info(f"[REGISTRY] {provider:15s} up-to-date ({current_model!r})")
            continue

        msg = f"{provider}: {current_model!r} → {api_id!r}  (Arena score {score:.0f})"
        changes.append(msg)
        logger.info(f"[REGISTRY] UPDATE {msg}")

        if not dry_run:
            text = section_re.sub(rf'\g<1>{api_id}\g<3>', text)

            # If provider has URL that embeds model name (Gemini), update URL too
            url_fn = fam.get("url_fn")
            if url_fn:
                url_re = _re.compile(
                    rf'(\[graeae\.providers\.{provider}\][^\[]*?url\s*=\s*")([^"]+)(")',
                    _re.DOTALL,
                )
                new_url = url_fn(api_id)
                text = url_re.sub(rf'\g<1>{new_url}\g<3>', text)
                logger.info(f"[REGISTRY]   URL updated → {new_url!r}")

    if changes and not dry_run:
        config_path.write_text(text)
        logger.info(f"[REGISTRY] config.toml updated ({len(changes)} changes)")

    return changes


# ── Model family detection ────────────────────────────────────────────────

def _model_family(model_id: str) -> str:
    """Extract the major version family from a model ID for update-vs-add decisions.

    Same family = dot-version update → replace existing entry.
    New family   = genuinely new model line → add alongside existing.

    Examples:
      grok-4.2          → grok-4
      grok-4.1-fast     → grok-4
      gpt-5.4           → gpt-5
      gpt-5.3-chat-latest → gpt-5
      gemini-3.1-pro-preview → gemini-3
      gemini-3-flash    → gemini-3
      claude-opus-4-6   → claude-opus-4
      qwen3-235b-a22b   → qwen3-235b
      llama-4-maverick  → llama-4
    """
    name = model_id.lower().strip()
    # Match: word-chars + dash + single digit optionally followed by .digit
    # Captures the "name-MAJOR" part before any further version/variant info
    m = re.match(r'^((?:[a-z][a-z0-9]*-)*[a-z][a-z0-9]*-\d+)[\.\-]', name)
    if m:
        return m.group(1)
    # Fallback: everything up to first dot
    return name.split('.')[0]


def _same_family(a: str, b: str) -> bool:
    """True if two model IDs share the same major-version family."""
    return _model_family(a) == _model_family(b)


# ── OpenClaw openclaw.json update ─────────────────────────────────────────

def update_openclaw_models(
    openclaw_path: Path,
    dry_run: bool = False,
    default_context_window: int = 1000000,
    default_max_tokens: int = 32768,
) -> list[str]:
    """Sync Arena top-models into openclaw.json per provider.

    Update rules:
      • Same model family (dot-version bump, same price tier) → REPLACE old entry.
        Cost, contextWindow, maxTokens are inherited from the replaced entry so
        the operator doesn't have to re-enter pricing for minor version bumps.
      • New model family (entirely new architecture/line)     → ADD alongside existing.
      • Model already present (exact or near-exact match)     → no-op.
      • Never deletes entries that are not superseded.
    """
    rows = _fetch_arena_rows()
    if not rows:
        logger.warning("[REGISTRY] no Arena data — openclaw.json unchanged")
        return []

    best = _best_per_family(rows)
    config = json.loads(openclaw_path.read_text())
    providers = config.get("models", {}).get("providers", {})
    changes: list[str] = []

    for graeae_provider, (arena_name, score) in best.items():
        fam = _PROVIDER_FAMILIES[graeae_provider]
        oc_provider = fam.get("openclaw_provider")
        oc_api = fam.get("openclaw_api")
        normalize = fam["normalize"]

        if oc_provider is None or oc_provider not in providers:
            continue

        api_id = normalize(arena_name)
        if api_id is None:
            api_id = arena_name  # use raw Arena name; operator may need to verify cost

        existing_models: list[dict] = providers[oc_provider].setdefault("models", [])
        existing_ids = [entry.get("id", "") for entry in existing_models]

        # ── Already present (exact or substring match) ─────────────────────
        if any(
            api_id.lower() == eid.lower()
            or api_id.lower() in eid.lower()
            or eid.lower() in api_id.lower()
            for eid in existing_ids
        ):
            logger.info(f"[REGISTRY] openclaw/{oc_provider}: {api_id!r} already present — skip")
            continue

        # ── Find same-family entry (dot-version update) ────────────────────
        same_fam_idx: Optional[int] = None
        for idx, entry in enumerate(existing_models):
            if _same_family(entry.get("id", ""), api_id):
                same_fam_idx = idx
                break

        new_model: dict = {
            "id": api_id,
            "name": arena_name,
            "api": oc_api,
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": default_context_window,
            "maxTokens": default_max_tokens,
        }

        if same_fam_idx is not None:
            old_entry = existing_models[same_fam_idx]
            old_id = old_entry.get("id", "?")
            # Inherit cost + limits from old entry so pricing doesn't go blank
            new_model["cost"] = old_entry.get("cost", new_model["cost"])
            new_model["contextWindow"] = old_entry.get("contextWindow", default_context_window)
            new_model["maxTokens"] = old_entry.get("maxTokens", default_max_tokens)

            msg = f"openclaw/{oc_provider}: REPLACE {old_id!r} → {api_id!r}  (Arena score {score:.0f})"
            changes.append(msg)
            logger.info(f"[REGISTRY] {msg}")

            if not dry_run:
                existing_models[same_fam_idx] = new_model

        else:
            msg = f"openclaw/{oc_provider}: ADD {api_id!r}  (new model family, Arena score {score:.0f})"
            changes.append(msg)
            logger.info(f"[REGISTRY] {msg}")

            if not dry_run:
                existing_models.append(new_model)

    if changes and not dry_run:
        openclaw_path.write_text(json.dumps(config, indent=2))
        logger.info(f"[REGISTRY] openclaw.json updated ({len(changes)} changes)")

    return changes

from __future__ import annotations

"""
Arena.ai (formerly LMArena) Elo score sync for GRAEAE provider weighting.

Data source
-----------
Official dataset:  lmarena-ai/leaderboard-dataset  (HuggingFace)
Config used:       text   (human-preference text arena)
Split:             latest (most recent snapshot, ~8k rows)

We use the HuggingFace Datasets Server HTTP API — plain JSON, no extra
libraries, no authentication required for public datasets.

Weight normalization
--------------------
Arena scores are typically in the range ~900–1400 Elo.
We map them to GRAEAE base weights in [0.50, 1.00]:

    weight = 0.50 + 0.50 * (score - score_p10) / (score_p90 - score_p10)

Using p10/p90 rather than min/max prevents a single extreme outlier from
compressing all other providers into a narrow band.

Provider → Arena model mapping
-------------------------------
Declared in _ELO_MODEL_MAP below — substring match (case-insensitive) against
the Arena model_name field. First match for each provider wins; update when
provider models change in config.toml.

Storage
-------
Weights are persisted to GRAEAE_ELO_REGISTRY (default: /var/lib/mnemos/graeae_elo_weights.json).
On engine startup, cached weights are used so Arena is not hit on every restart.
A separate `scripts/refresh_elo_weights.py` is intended for a quarterly systemd timer.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── HuggingFace Datasets Server ───────────────────────────────────────────
_HF_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=lmarena-ai%2Fleaderboard-dataset"
    "&config=text"
    "&split=latest"
    "&offset=0&limit=300"
)

# ── Provider → Arena model name substring (case-insensitive, first match) ─
# Keys are GRAEAE provider names; values are substrings of how the model
# appears in the Arena leaderboard — always the MODEL NAME, never the
# inference host (e.g. "gpt-oss-120b" not "groq", "llama-3.3-70b" not "groq").
# Providers whose models are not in Arena's top-100 snapshot will not match
# and will silently retain their config.toml base weight.
# Update these when provider models change in config.toml [graeae.providers].
_ELO_MODEL_MAP: dict[str, str] = {
    "perplexity":  "sonar-pro",
    "groq":        "gpt-oss-120b",        # openai/gpt-oss-120b served via Groq
    "claude_opus": "claude-opus-4-6",
    "xai":         "grok-4.20-0309-reasoning",
    "openai":      "gpt-5.4",
    "gemini":      "gemini-3.1-pro",
    "nvidia":      "llama-4-maverick",     # meta/llama-4-maverick-17b-128e-instruct via NIM
    "together":    "qwen3-235b",          # Qwen3-235B-A22B served via Together
}

# ── On-disk weight registry ───────────────────────────────────────────────
_REGISTRY_PATH = Path(
    os.getenv("GRAEAE_ELO_REGISTRY", "/var/lib/mnemos/graeae_elo_weights.json")
)


# ── Normalisation ─────────────────────────────────────────────────────────

def _normalize_weights(scores: dict[str, float]) -> dict[str, float]:
    """Map raw Elo scores → GRAEAE weights in [0.50, 1.00] using p10/p90 anchoring."""
    if not scores:
        return {}
    vals = sorted(scores.values())
    n = len(vals)
    p10 = vals[max(0, int(n * 0.10))]
    p90 = vals[min(n - 1, int(n * 0.90))]
    span = p90 - p10 or 1.0

    return {
        name: round(max(0.50, min(1.00, 0.50 + 0.50 * (s - p10) / span)), 4)
        for name, s in scores.items()
    }


# ── Fetch ─────────────────────────────────────────────────────────────────

def fetch_elo_weights(timeout: int = 30) -> Optional[dict[str, float]]:
    """Fetch the Arena.ai text leaderboard and return provider → weight map.

    Returns None on any failure so the caller can fall back to config.toml weights.
    """
    try:
        resp = httpx.get(_HF_ROWS_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"[ELO] arena fetch failed: {exc}")
        return None

    rows = data.get("rows", [])
    if not rows:
        logger.warning("[ELO] HuggingFace returned empty rows")
        return None

    # Build normalised-name → rating lookup
    arena_scores: dict[str, float] = {}
    for entry in rows:
        row = entry.get("row", entry)
        name = (row.get("model_name") or row.get("model") or "").lower().strip()
        rating = row.get("rating") or row.get("score") or 0
        if name and rating:
            arena_scores[name] = float(rating)

    if not arena_scores:
        logger.warning("[ELO] could not parse any model scores from response")
        return None

    vals = list(arena_scores.values())
    logger.info(
        f"[ELO] fetched {len(arena_scores)} models from arena.ai, "
        f"score range {min(vals):.0f}–{max(vals):.0f}"
    )

    # Match providers to Arena model names
    matched_scores: dict[str, float] = {}
    for provider, search_term in _ELO_MODEL_MAP.items():
        term = search_term.lower()
        candidates = {n: s for n, s in arena_scores.items() if term in n}
        if candidates:
            best = max(candidates, key=candidates.__getitem__)
            matched_scores[provider] = candidates[best]
            logger.info(
                f"[ELO]   {provider:15s} ← arena:{best!r}  score={candidates[best]:.0f}"
            )
        else:
            logger.debug(f"[ELO]   {provider}: no arena match for {search_term!r} — skipping")

    if not matched_scores:
        return None

    weights = _normalize_weights(matched_scores)
    logger.info(f"[ELO] normalised weights: {weights}")
    return weights


# ── Cache ─────────────────────────────────────────────────────────────────

def load_cached_weights() -> Optional[dict[str, float]]:
    """Load weights persisted by a previous sync run."""
    if not _REGISTRY_PATH.exists():
        return None
    try:
        data = json.loads(_REGISTRY_PATH.read_text())
        weights = data.get("weights")
        updated = data.get("updated_at", "unknown")
        if weights:
            logger.info(f"[ELO] loaded cached weights from {updated} ({len(weights)} providers)")
        return weights
    except Exception as exc:
        logger.warning(f"[ELO] failed to load cached weights: {exc}")
        return None


def save_weights(weights: dict[str, float]) -> None:
    """Persist weights to the on-disk registry."""
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(json.dumps({
            "weights": weights,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": "arena.ai / lmarena-ai/leaderboard-dataset (text/latest)",
        }, indent=2))
        logger.info(f"[ELO] weights saved to {_REGISTRY_PATH}")
    except Exception as exc:
        logger.warning(f"[ELO] failed to save weights: {exc}")


def get_elo_weights(force_refresh: bool = False) -> Optional[dict[str, float]]:
    """Return Elo-derived provider weights.

    Resolution order:
      1. Cached registry (unless force_refresh=True)
      2. Live fetch from arena.ai via HuggingFace Datasets API
      3. None → caller uses config.toml weights

    Call with force_refresh=True from the quarterly refresh script.
    """
    if not force_refresh:
        cached = load_cached_weights()
        if cached:
            return cached

    weights = fetch_elo_weights()
    if weights:
        save_weights(weights)
    return weights

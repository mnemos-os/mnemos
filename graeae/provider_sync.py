from __future__ import annotations
"""
MNEMOS Model Registry — Provider API Sync

Queries each LLM provider's /models (or equivalent) endpoint daily and
upserts results into the model_registry PostgreSQL table.

Providers and their API patterns:
  openai    → GET /v1/models (OpenAI-compatible)
  groq      → GET /v1/models (OpenAI-compatible)
  xai       → GET /v1/models (OpenAI-compatible)
  together  → GET /v1/models (OpenAI-compatible, type=chat filter)
  nvidia    → GET /v1/models (OpenAI-compatible, filter nim/ prefix)
  gemini    → GET /v1beta/models (Google Generative AI — paginated)
  anthropic → static list (Anthropic does not expose a public /models endpoint)

Arena.ai rankings are written separately by update_model_registry.py / elo_sync.py.
This module only sets arena_score/arena_rank if passed in via update_arena_scores().

Usage (standalone, for testing):
  python3 -m graeae.provider_sync --provider openai --dry-run
  python3 -m graeae.provider_sync --all
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Key store ──────────────────────────────────────────────────────────────────
_KEY_FILE = Path(
    os.getenv("API_KEYS_FILE", Path.home() / ".config" / "mnemos" / "api_keys.json")
)

def _load_key(provider: str) -> Optional[str]:
    """Load API key from the Provider Registry File."""
    try:
        data = json.loads(_KEY_FILE.read_text())
        return data.get("llm_providers", {}).get(provider, {}).get("api_key")
    except Exception:
        return None


# ── Model family extraction (mirrors model_registry.py) ───────────────────────
import re as _re

def _model_family(model_id: str) -> str:
    """Extract major-version family for dedup / replace-vs-add logic."""
    name = model_id.lower().strip()
    m = _re.match(r'^((?:[a-z][a-z0-9]*-)*[a-z][a-z0-9]*-\d+)[\.\-]', name)
    if m:
        return m.group(1)
    return name.split('.')[0]


# ── Per-provider fetch functions ───────────────────────────────────────────────

async def _fetch_openai_compatible(
    base_url: str,
    api_key: str,
    provider: str,
    model_filter: Optional[callable] = None,
    timeout: int = 20,
) -> list[dict]:
    """Fetch models from an OpenAI-compatible /v1/models endpoint."""
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/v1/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[SYNC:{provider}] fetch failed: {exc}")
        return []

    models = []
    items = data if isinstance(data, list) else data.get("data", [])
    for item in items:
        mid = item.get("id", "")
        if not mid:
            continue
        if model_filter and not model_filter(mid, item):
            continue
        models.append({
            "provider":           provider,
            "model_id":           mid,
            "display_name":       item.get("name") or item.get("display_name") or mid,
            "family":             _model_family(mid),
            "context_window":     item.get("context_window") or item.get("context_length"),
            "max_output_tokens":  item.get("max_output_tokens") or item.get("max_tokens"),
            "capabilities":       _infer_capabilities(mid, item),
            "available":          True,
            "raw":                item,
        })
    return models


async def _fetch_openai(timeout: int = 20) -> list[dict]:
    key = _load_key("openai")
    if not key:
        logger.warning("[SYNC:openai] no API key — skipping")
        return []

    def _filter(mid: str, _item: dict) -> bool:
        # Only flagship/reasoning models — skip whisper, dall-e, tts, embeddings
        if any(x in mid for x in ["whisper", "dall-e", "tts-", "embedding", "text-", "babbage", "davinci"]):
            return False
        return True

    return await _fetch_openai_compatible("https://api.openai.com", key, "openai", _filter, timeout)


async def _fetch_xai(timeout: int = 20) -> list[dict]:
    key = _load_key("xai")
    if not key:
        logger.warning("[SYNC:xai] no API key — skipping")
        return []

    def _filter(mid: str, _item: dict) -> bool:
        return mid.startswith("grok-")

    return await _fetch_openai_compatible("https://api.x.ai", key, "xai", _filter, timeout)


async def _fetch_groq(timeout: int = 20) -> list[dict]:
    key = _load_key("groq")
    if not key:
        logger.warning("[SYNC:groq] no API key — skipping")
        return []

    def _filter(mid: str, item: dict) -> bool:
        # Groq hosts many models — keep only active chat models
        return item.get("active", True) is not False

    return await _fetch_openai_compatible("https://api.groq.com/openai", key, "groq", _filter, timeout)


async def _fetch_together(timeout: int = 60) -> list[dict]:
    key = _load_key("together_ai")
    if not key:
        logger.warning("[SYNC:together] no API key — skipping")
        return []

    def _filter(mid: str, item: dict) -> bool:
        # Together lists image, embedding, rerank models — keep only chat/language
        return item.get("type", "chat") in ("chat", "language")

    return await _fetch_openai_compatible("https://api.together.xyz", key, "together", _filter, timeout)


async def _fetch_nvidia(timeout: int = 20) -> list[dict]:
    key = _load_key("nvidia")
    if not key:
        logger.warning("[SYNC:nvidia] no API key — skipping")
        return []

    def _filter(mid: str, item: dict) -> bool:
        # NVIDIA NIM hosts many providers; we want nvidia/* models only
        return mid.startswith("nvidia/") or mid.startswith("meta/") or "/" not in mid

    return await _fetch_openai_compatible(
        "https://integrate.api.nvidia.com", key, "nvidia", _filter, timeout
    )


async def _fetch_gemini(timeout: int = 20) -> list[dict]:
    key = _load_key("google_gemini")
    if not key:
        logger.warning("[SYNC:gemini] no API key — skipping")
        return []

    models = []
    page_token = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                params: dict = {"key": key, "pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token

                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                for item in data.get("models", []):
                    mid = item.get("name", "")
                    # Strip "models/" prefix → bare model ID used in API calls
                    mid = mid.removeprefix("models/")
                    if not mid:
                        continue
                    # Only keep generateContent-capable models
                    methods = item.get("supportedGenerationMethods", [])
                    if "generateContent" not in methods:
                        continue
                    models.append({
                        "provider":           "gemini",
                        "model_id":           mid,
                        "display_name":       item.get("displayName") or mid,
                        "family":             _model_family(mid),
                        "context_window":     item.get("inputTokenLimit"),
                        "max_output_tokens":  item.get("outputTokenLimit"),
                        "capabilities":       _infer_capabilities(mid, item),
                        "available":          True,
                        "raw":                item,
                    })

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

    except Exception as exc:
        logger.warning(f"[SYNC:gemini] fetch failed: {exc}")

    return models


def _fetch_anthropic_static() -> list[dict]:
    """Anthropic does not expose a public /models endpoint; use a static list.

    Updated manually here when new Claude models ship.
    Capabilities and pricing are hardcoded from public Anthropic docs.
    """
    return [
        {
            "provider": "anthropic",
            "model_id": "claude-opus-4-7",
            "display_name": "Claude Opus 4.7",
            "family": "claude-opus-4",
            "context_window": 200000,
            "max_output_tokens": 32768,
            "capabilities": ["chat", "code", "reasoning", "vision"],
            "input_cost_per_mtok":  15.00,
            "output_cost_per_mtok": 75.00,
            "cache_read_per_mtok":   1.50,
            "cache_write_per_mtok":  3.75,
            "available": True,
            "raw": {"source": "static", "docs": "https://docs.anthropic.com/en/docs/about-claude/models"},
        },
        {
            "provider": "anthropic",
            "model_id": "claude-opus-4-6",
            "display_name": "Claude Opus 4.6",
            "family": "claude-opus-4",
            "context_window": 200000,
            "max_output_tokens": 32768,
            "capabilities": ["chat", "code", "reasoning", "vision"],
            "input_cost_per_mtok":  15.00,
            "output_cost_per_mtok": 75.00,
            "cache_read_per_mtok":   1.50,
            "cache_write_per_mtok":  3.75,
            "available": True,
            "raw": {"source": "static", "docs": "https://docs.anthropic.com/en/docs/about-claude/models"},
        },
        {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "display_name": "Claude Sonnet 4.6",
            "family": "claude-sonnet-4",
            "context_window": 200000,
            "max_output_tokens": 16384,
            "capabilities": ["chat", "code", "reasoning", "vision"],
            "input_cost_per_mtok":  3.00,
            "output_cost_per_mtok": 15.00,
            "cache_read_per_mtok":   0.30,
            "cache_write_per_mtok":  3.75,
            "available": True,
            "raw": {"source": "static", "docs": "https://docs.anthropic.com/en/docs/about-claude/models"},
        },
        {
            "provider": "anthropic",
            "model_id": "claude-haiku-4-5-20251001",
            "display_name": "Claude Haiku 4.5",
            "family": "claude-haiku-4",
            "context_window": 200000,
            "max_output_tokens": 8192,
            "capabilities": ["chat", "code", "vision"],
            "input_cost_per_mtok":  0.80,
            "output_cost_per_mtok": 4.00,
            "cache_read_per_mtok":  0.08,
            "cache_write_per_mtok": 1.00,
            "available": True,
            "raw": {"source": "static", "docs": "https://docs.anthropic.com/en/docs/about-claude/models"},
        },
    ]


def _fetch_perplexity_static() -> list[dict]:
    """Perplexity's /models endpoint requires a subscription; use a static list."""
    return [
        {
            "provider": "perplexity",
            "model_id": "sonar-pro",
            "display_name": "Sonar Pro",
            "family": "sonar",
            "context_window": 200000,
            "max_output_tokens": 8000,
            "capabilities": ["chat", "web_search"],
            "input_cost_per_mtok":  3.00,
            "output_cost_per_mtok": 15.00,
            "available": True,
            "raw": {"source": "static"},
        },
        {
            "provider": "perplexity",
            "model_id": "sonar",
            "display_name": "Sonar",
            "family": "sonar",
            "context_window": 128000,
            "max_output_tokens": 8000,
            "capabilities": ["chat", "web_search"],
            "input_cost_per_mtok":  1.00,
            "output_cost_per_mtok": 1.00,
            "available": True,
            "raw": {"source": "static"},
        },
        {
            "provider": "perplexity",
            "model_id": "sonar-reasoning-pro",
            "display_name": "Sonar Reasoning Pro",
            "family": "sonar-reasoning",
            "context_window": 128000,
            "max_output_tokens": 8000,
            "capabilities": ["chat", "web_search", "reasoning"],
            "input_cost_per_mtok":  2.00,
            "output_cost_per_mtok": 8.00,
            "available": True,
            "raw": {"source": "static"},
        },
    ]


# ── Capabilities inference ─────────────────────────────────────────────────────

def _infer_capabilities(model_id: str, item: dict) -> list[str]:
    """Infer model capabilities from ID and API response metadata."""
    caps = ["chat"]
    mid = model_id.lower()

    if any(x in mid for x in ["vision", "vl", "4o", "gemini", "claude", "grok"]):
        caps.append("vision")
    if any(x in mid for x in ["code", "coder", "codestral"]):
        caps.append("code")
    if any(x in mid for x in ["r1", "o3", "o4", "think", "reason", "qwq", "deepseek-r"]):
        caps.append("reasoning")
    if any(x in mid for x in ["sonar", "search", "online", "perplexity"]):
        caps.append("web_search")

    # Provider-specific metadata fields
    if item.get("supportedGenerationMethods"):  # Gemini
        if "generateContent" in item["supportedGenerationMethods"]:
            if "gemini" in mid:
                caps.append("vision")

    return sorted(set(caps))


# ── DB upsert ──────────────────────────────────────────────────────────────────

async def upsert_models(pool, models: list[dict], dry_run: bool = False) -> tuple[int, int, int]:
    """Upsert model list into model_registry.

    Returns (added, updated, deprecated) counts.
    """
    if not models:
        return 0, 0, 0

    added = updated = deprecated = 0
    now = datetime.utcnow()

    async with pool.acquire() as conn:
        async with conn.transaction():
            for m in models:
                if dry_run:
                    logger.info(f"[SYNC] DRY-RUN upsert: {m['provider']}/{m['model_id']}")
                    continue

                existing = await conn.fetchrow(
                    "SELECT id, available FROM model_registry WHERE provider=$1 AND model_id=$2",
                    m["provider"], m["model_id"],
                )

                raw_json = json.dumps(m.get("raw", {}))
                caps = m.get("capabilities", [])

                if existing is None:
                    await conn.execute(
                        """INSERT INTO model_registry
                            (provider, model_id, display_name, family,
                             context_window, max_output_tokens, capabilities,
                             input_cost_per_mtok, output_cost_per_mtok,
                             cache_read_per_mtok, cache_write_per_mtok,
                             available, last_seen, last_synced, raw)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE,$12,$12,$13)
                           ON CONFLICT (provider, model_id) DO NOTHING""",
                        m["provider"], m["model_id"],
                        m.get("display_name") or m["model_id"],
                        m.get("family") or _model_family(m["model_id"]),
                        m.get("context_window"), m.get("max_output_tokens"),
                        caps,
                        m.get("input_cost_per_mtok", 0), m.get("output_cost_per_mtok", 0),
                        m.get("cache_read_per_mtok", 0), m.get("cache_write_per_mtok", 0),
                        now, raw_json,
                    )
                    added += 1
                    logger.info(f"[SYNC] ADD {m['provider']}/{m['model_id']}")
                else:
                    await conn.execute(
                        """UPDATE model_registry SET
                            display_name=$3, family=$4,
                            context_window=COALESCE($5, context_window),
                            max_output_tokens=COALESCE($6, max_output_tokens),
                            capabilities=$7,
                            available=TRUE, last_seen=$8, last_synced=$8, raw=$9
                           WHERE provider=$1 AND model_id=$2""",
                        m["provider"], m["model_id"],
                        m.get("display_name") or m["model_id"],
                        m.get("family") or _model_family(m["model_id"]),
                        m.get("context_window"), m.get("max_output_tokens"),
                        caps, now, raw_json,
                    )
                    updated += 1

            if not dry_run:
                # Mark models not seen in this sync as unavailable
                provider = models[0]["provider"] if models else None
                if provider:
                    seen_ids = [m["model_id"] for m in models]
                    result = await conn.execute(
                        """UPDATE model_registry SET available=FALSE
                           WHERE provider=$1 AND model_id != ALL($2::text[])
                             AND available=TRUE""",
                        provider, seen_ids,
                    )
                    # asyncpg returns "UPDATE N" string
                    try:
                        deprecated = int(result.split()[-1])
                    except (ValueError, AttributeError):
                        deprecated = 0
                    if deprecated:
                        logger.info(f"[SYNC] DEPRECATED {deprecated} models from {provider}")

    return added, updated, deprecated


async def log_sync(pool, provider: str, models_found: int, added: int,
                   updated: int, deprecated: int, error: Optional[str],
                   duration_ms: int) -> None:
    """Write a row to model_registry_sync_log."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_registry_sync_log
                    (provider, models_found, models_added, models_updated,
                     models_deprecated, error, duration_ms)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                provider, models_found, added, updated, deprecated, error, duration_ms,
            )
    except Exception as exc:
        logger.warning(f"[SYNC] failed to write sync log for {provider}: {exc}")


# ── Orchestrator ───────────────────────────────────────────────────────────────

_LIVE_PROVIDERS = {
    "openai":    _fetch_openai,
    "xai":       _fetch_xai,
    "groq":      _fetch_groq,
    "together":  _fetch_together,
    "nvidia":    _fetch_nvidia,
    "gemini":    _fetch_gemini,
}

_STATIC_PROVIDERS = {
    "anthropic":  _fetch_anthropic_static,
    "perplexity": _fetch_perplexity_static,
}


async def sync_provider(
    pool,
    provider: str,
    dry_run: bool = False,
) -> dict:
    """Sync a single provider. Returns summary dict."""
    t0 = time.monotonic()
    error: Optional[str] = None
    models: list[dict] = []

    try:
        if provider in _LIVE_PROVIDERS:
            models = await _LIVE_PROVIDERS[provider]()
        elif provider in _STATIC_PROVIDERS:
            models = _STATIC_PROVIDERS[provider]()
        else:
            raise ValueError(f"Unknown provider: {provider!r}")

        logger.info(f"[SYNC:{provider}] fetched {len(models)} models")

        added = updated = deprecated = 0
        if pool is not None:
            added, updated, deprecated = await upsert_models(pool, models, dry_run=dry_run)
        else:
            logger.warning(f"[SYNC:{provider}] no DB pool — skipping upsert (dry-run mode)")
    except Exception as exc:
        error = str(exc)
        logger.error(f"[SYNC:{provider}] error: {exc}", exc_info=True)
        added = updated = deprecated = 0

    duration_ms = int((time.monotonic() - t0) * 1000)

    if pool is not None and not dry_run:
        await log_sync(pool, provider, len(models), added, updated, deprecated, error, duration_ms)

    return {
        "provider":         provider,
        "models_found":     len(models),
        "models_added":     added if not dry_run else 0,
        "models_updated":   updated if not dry_run else 0,
        "models_deprecated":deprecated if not dry_run else 0,
        "error":            error,
        "duration_ms":      duration_ms,
        "dry_run":          dry_run,
    }


async def sync_all_providers(
    pool,
    dry_run: bool = False,
    providers: Optional[list[str]] = None,
) -> list[dict]:
    """Sync all (or specified) providers concurrently. Returns list of summary dicts."""
    all_prov = list(_LIVE_PROVIDERS) + list(_STATIC_PROVIDERS)
    targets = providers if providers else all_prov
    unknown = [p for p in targets if p not in all_prov]
    if unknown:
        raise ValueError(f"Unknown providers: {unknown}")

    results = await asyncio.gather(
        *[sync_provider(pool, p, dry_run=dry_run) for p in targets],
        return_exceptions=True,
    )
    out = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"[SYNC] unexpected exception in gather: {r}")
        else:
            out.append(r)
    return out


# ── Arena score update (called by update_model_registry.py) ───────────────────

async def update_arena_scores(
    pool,
    scores: dict[str, tuple[str, float, int]],  # {provider: (model_id, score, rank)}
) -> None:
    """Write Arena Elo scores + GRAEAE weights back to model_registry rows.

    scores: {provider: (api_model_id, arena_score, arena_rank)}
    graeae_weight is derived inline from the score using p10/p90 normalization.

    Matching strategy (in order):
      1. Exact match on model_id (e.g. openai/gpt-5.4 → gpt-5.4 in DB)
      2. Family prefix match (e.g. xai/grok-4.2 → family "grok-4" matches
         grok-4.20-0309-reasoning, grok-4.20-0309-non-reasoning, etc.)
    This handles providers where the Arena normalizer produces an alias that
    doesn't appear verbatim in the provider's /models response.
    """
    if not pool or not scores:
        return

    raw_scores = {prov: s for prov, (_, s, _) in scores.items()}
    vals = sorted(raw_scores.values())
    n = len(vals)
    p10 = vals[max(0, int(n * 0.10))]
    p90 = vals[min(n - 1, int(n * 0.90))]
    span = p90 - p10 or 1.0

    async with pool.acquire() as conn:
        for prov, (model_id, arena_score, arena_rank) in scores.items():
            weight = round(max(0.50, min(1.00, 0.50 + 0.50 * (arena_score - p10) / span)), 4)

            # 1. Exact match
            result = await conn.execute(
                """UPDATE model_registry
                   SET arena_score=$3, arena_rank=$4, graeae_weight=$5
                   WHERE provider=$1 AND model_id=$2""",
                prov, model_id, arena_score, arena_rank, weight,
            )
            rows_updated = int(result.split()[-1]) if result else 0

            # 2. Family prefix match as fallback
            if rows_updated == 0:
                family = _model_family(model_id)
                result = await conn.execute(
                    """UPDATE model_registry
                       SET arena_score=$3, arena_rank=$4, graeae_weight=$5
                       WHERE provider=$1 AND family=$2""",
                    prov, family, arena_score, arena_rank, weight,
                )
                rows_updated = int(result.split()[-1]) if result else 0
                if rows_updated:
                    logger.info(
                        f"[SYNC] arena scores applied to family {prov}/{family!r} "
                        f"({rows_updated} rows) score={arena_score:.0f} weight={weight}"
                    )
                else:
                    logger.debug(
                        f"[SYNC] arena: no rows matched for {prov}/{model_id!r} "
                        f"(family={family!r}) — provider models not yet synced?"
                    )
                continue

            logger.info(
                f"[SYNC] arena scores updated: {prov}/{model_id} "
                f"score={arena_score:.0f} rank={arena_rank} weight={weight}"
            )


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Sync provider models into MNEMOS registry")
    parser.add_argument("--provider", help="Single provider to sync (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    args = parser.parse_args()

    async def _run() -> None:
        pool = None
        if not args.dry_run:
            from api import lifecycle as _lc
            await _lc.startup()
            pool = _lc._pool

        if args.provider:
            result = await sync_provider(pool, args.provider, dry_run=args.dry_run)
            results = [result]
        else:
            results = await sync_all_providers(pool, dry_run=args.dry_run)

        print("\n=== Provider Sync Results ===")
        for r in results:
            status = "ERROR" if r.get("error") else "OK"
            print(
                f"  [{status}] {r['provider']:12s}  "
                f"found={r['models_found']:3d}  "
                f"added={r['models_added']:3d}  "
                f"updated={r['models_updated']:3d}  "
                f"deprecated={r['models_deprecated']:3d}  "
                f"{r['duration_ms']}ms"
            )
            if r.get("error"):
                print(f"           ERROR: {r['error']}")

        if pool:
            await _lc.shutdown()

    asyncio.run(_run())

#!/usr/bin/env python3
"""
MNEMOS Model Registry — Daily Provider Sync

Queries each LLM provider's model list API and upserts results into the
model_registry PostgreSQL table.  Intended to run daily via systemd timer
so MNEMOS is always the authoritative model registry.

Then runs Arena.ai ranking sync so arena_score / graeae_weight are current.

Usage:
  python3 scripts/sync_provider_models.py              # sync all providers
  python3 scripts/sync_provider_models.py --dry-run    # preview only
  python3 scripts/sync_provider_models.py --provider openai xai
  python3 scripts/sync_provider_models.py --arena-only # refresh scores only
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sync_provider_models")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily sync of LLM provider model lists into MNEMOS registry"
    )
    parser.add_argument(
        "--provider", nargs="+", metavar="PROVIDER",
        help="One or more provider names to sync (default: all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and display changes without writing to DB",
    )
    parser.add_argument(
        "--arena-only", action="store_true",
        help="Skip provider sync; only refresh Arena.ai rankings",
    )
    parser.add_argument(
        "--skip-arena", action="store_true",
        help="Skip Arena.ai ranking refresh after provider sync",
    )
    args = parser.parse_args()

    # ── Connect to MNEMOS PostgreSQL pool ─────────────────────────────────────
    pool = None
    if not args.dry_run:
        try:
            import asyncpg
            dsn = os.getenv(
                "DATABASE_URL",
                "postgresql://mnemos:mnemos@localhost/mnemos"
            )
            pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)
            logger.info("[SYNC] DB pool connected")
        except Exception as exc:
            logger.error(f"[SYNC] cannot connect to DB: {exc}")
            return 1
    else:
        logger.info("[SYNC] DRY-RUN mode — no DB writes")

    exit_code = 0

    try:
        # ── 1. Provider API sync ───────────────────────────────────────────────
        if not args.arena_only:
            from mnemos.domain.graeae.provider_sync import sync_all_providers
            logger.info("=== Provider API sync ===")
            results = await sync_all_providers(
                pool=pool,
                dry_run=args.dry_run,
                providers=args.provider or None,
            )

            total_added = total_updated = total_deprecated = 0
            errors = []
            for r in results:
                status = "ERROR" if r.get("error") else "OK"
                logger.info(
                    f"[{status}] {r['provider']:12s}  "
                    f"found={r['models_found']:3d}  "
                    f"added={r['models_added']:3d}  "
                    f"updated={r['models_updated']:3d}  "
                    f"deprecated={r['models_deprecated']:3d}  "
                    f"{r['duration_ms']}ms"
                )
                if r.get("error"):
                    errors.append(f"{r['provider']}: {r['error']}")
                total_added     += r["models_added"]
                total_updated   += r["models_updated"]
                total_deprecated += r["models_deprecated"]

            logger.info(
                f"=== Provider sync complete: "
                f"+{total_added} added, ~{total_updated} updated, "
                f"-{total_deprecated} deprecated ==="
            )
            if errors:
                logger.warning(f"Errors in {len(errors)} provider(s):")
                for e in errors:
                    logger.warning(f"  {e}")
                exit_code = 2  # partial failure

        # ── 2. Arena.ai ranking sync ───────────────────────────────────────────
        if not args.skip_arena:
            logger.info("=== Arena.ai ranking sync ===")
            try:
                from mnemos.domain.graeae.elo_sync import fetch_elo_weights, save_weights
                from mnemos.domain.graeae.model_registry import _PROVIDER_FAMILIES, _best_per_family, _fetch_arena_rows

                rows = _fetch_arena_rows()
                if rows:
                    best = _best_per_family(rows)

                    # Build arena scores dict for DB update
                    # {provider: (api_model_id, arena_score, arena_rank)}
                    arena_scores: dict = {}
                    sorted_rows = sorted(rows, key=lambda r: float(r.get("rating") or r.get("score") or 0), reverse=True)
                    rank_map = {
                        (r.get("model_name") or r.get("model") or "").lower().strip(): i + 1
                        for i, r in enumerate(sorted_rows)
                    }

                    # Map GRAEAE internal provider names → DB provider column names.
                    # These differ only for providers where GRAEAE uses a descriptive
                    # key (claude_opus) but the registry stores the vendor name (anthropic).
                    _GRAEAE_TO_DB_PROVIDER: dict[str, str] = {
                        "claude_opus": "anthropic",
                        "xai":         "xai",
                        "openai":      "openai",
                        "gemini":      "gemini",
                        "together":    "together",
                        "groq":        "groq",
                        "nvidia":      "nvidia",
                        "perplexity":  "perplexity",
                    }

                    from mnemos.domain.graeae.model_registry import _PROVIDER_FAMILIES
                    for prov, (arena_name, score) in best.items():
                        fam = _PROVIDER_FAMILIES.get(prov, {})
                        normalize = fam.get("normalize")
                        if normalize:
                            api_id = normalize(arena_name)
                        else:
                            api_id = arena_name
                        if api_id:
                            db_prov = _GRAEAE_TO_DB_PROVIDER.get(prov, prov)
                            arena_scores[db_prov] = (api_id, score, rank_map.get(arena_name, 0))

                    if pool and not args.dry_run and arena_scores:
                        from mnemos.domain.graeae.provider_sync import update_arena_scores
                        await update_arena_scores(pool, arena_scores)
                        logger.info(f"[ARENA] updated scores for {len(arena_scores)} providers")

                    # Also refresh the Elo weights cache used by the GRAEAE engine
                    weights = fetch_elo_weights()
                    if weights and not args.dry_run:
                        save_weights(weights)
                        logger.info(f"[ARENA] Elo weights cache refreshed: {weights}")
                    elif args.dry_run:
                        logger.info(f"[ARENA] DRY-RUN: would save weights: {weights}")
                else:
                    logger.warning("[ARENA] no rows from Arena.ai — scores not updated")

            except Exception as exc:
                logger.error(f"[ARENA] ranking sync failed: {exc}", exc_info=True)
                exit_code = max(exit_code, 2)

    finally:
        if pool:
            await pool.close()
            logger.info("[SYNC] DB pool closed")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

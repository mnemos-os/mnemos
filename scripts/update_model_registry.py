#!/usr/bin/env python3
"""
Arena.ai model registry auto-updater.

Updates both GRAEAE (config.toml) and OpenClaw (openclaw.json) with the
highest-ranked models per provider family from the Arena.ai text leaderboard.
Also refreshes Elo-derived provider weights.

Intended to run quarterly via systemd timer alongside refresh_elo_weights.py,
or triggered manually after a major model release cycle.

Usage:
  python3 scripts/update_model_registry.py --dry-run     # preview changes only
  python3 scripts/update_model_registry.py               # apply + restart service

After applying, restart mnemos.service:
  sudo systemctl restart mnemos.service
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("update_model_registry")

# ── Paths ─────────────────────────────────────────────────────────────────
_REPO_ROOT    = Path(__file__).parent.parent
_GRAEAE_CFG   = _REPO_ROOT / "config.toml"
_OPENCLAW_CFG = Path(os.getenv("OPENCLAW_CONFIG", Path.home() / ".openclaw" / "openclaw.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-update GRAEAE + OpenClaw model registry from Arena.ai")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    parser.add_argument("--graeae-only", action="store_true", help="Update GRAEAE config.toml only")
    parser.add_argument("--openclaw-only", action="store_true", help="Update openclaw.json only")
    parser.add_argument("--restart", action="store_true", help="Restart mnemos.service after changes (requires sudo)")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN — no files will be modified ===")

    from mnemos.domain.graeae.elo_sync import fetch_elo_weights, save_weights
    from mnemos.domain.graeae.model_registry import update_graeae_config, update_openclaw_models

    all_changes = []

    # ── 1. Refresh Elo weights ─────────────────────────────────────────────
    logger.info("--- Refreshing Elo weights ---")
    weights = fetch_elo_weights()
    if weights:
        if not args.dry_run:
            save_weights(weights)
        logger.info(f"Elo weights: { {p: f'{w:.3f}' for p, w in weights.items()} }")
    else:
        logger.warning("Elo weight refresh failed")

    # ── 2. Update GRAEAE config.toml model names ───────────────────────────
    if not args.openclaw_only:
        logger.info(f"--- Updating GRAEAE config.toml ({_GRAEAE_CFG}) ---")
        if not _GRAEAE_CFG.exists():
            logger.error(f"config.toml not found at {_GRAEAE_CFG}")
        else:
            changes = update_graeae_config(_GRAEAE_CFG, dry_run=args.dry_run)
            all_changes.extend(changes)
            if not changes:
                logger.info("GRAEAE config.toml: no model changes needed")

    # ── 3. Update openclaw.json ────────────────────────────────────────────
    if not args.graeae_only:
        logger.info(f"--- Updating OpenClaw config ({_OPENCLAW_CFG}) ---")
        if not _OPENCLAW_CFG.exists():
            logger.warning(f"openclaw.json not found at {_OPENCLAW_CFG} — skipping")
        else:
            changes = update_openclaw_models(_OPENCLAW_CFG, dry_run=args.dry_run)
            all_changes.extend(changes)
            if not changes:
                logger.info("openclaw.json: no new models to add")

    # ── Summary ────────────────────────────────────────────────────────────
    if all_changes:
        logger.info(f"\n=== {len(all_changes)} change(s) {'would be' if args.dry_run else 'applied'}: ===")
        for c in all_changes:
            logger.info(f"  • {c}")
    else:
        logger.info("=== All registries up-to-date ===")

    # ── 4. Restart service ─────────────────────────────────────────────────
    if all_changes and not args.dry_run and args.restart:
        logger.info("Restarting mnemos.service...")
        result = subprocess.run(["sudo", "systemctl", "restart", "mnemos.service"])
        if result.returncode == 0:
            logger.info("mnemos.service restarted successfully")
        else:
            logger.warning("systemctl restart returned non-zero — check service status")

    return 0


if __name__ == "__main__":
    sys.exit(main())

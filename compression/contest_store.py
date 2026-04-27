"""Persistence layer for v3.1 compression contests.

One public function, persist_contest(), writes a ContestOutcome from
compression/contest.py into the two tables it spans:

  * memory_compression_candidates — one row per engine attempt (winner
    + every loser, including disabled / error / no_output /
    quality_floor candidates) with their scoring fields.
  * memory_compressed_variants    — upserted for the memory with a
    pointer at the winning candidate's row and an inlined copy of the
    compressed_content so downstream reads don't require a join.

All writes happen in a single transaction so a partial failure can't
leave a memory with a variant whose winner_candidate_id points at a
row that isn't there. The transaction DOES NOT touch
memory_compression_queue — the distillation worker is responsible for
that row's lifecycle (status transitions, attempts counter, error
string) so the persistence function stays idempotent on its own
surface.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .contest import ContestOutcome

logger = logging.getLogger(__name__)


_INSERT_CANDIDATE_SQL = """
INSERT INTO memory_compression_candidates (
    memory_id, owner_id, contest_id, engine_id, engine_version,
    compressed_content, original_tokens, compressed_tokens,
    compression_ratio, quality_score, speed_factor, composite_score,
    scoring_profile, elapsed_ms, judge_model, gpu_used,
    is_winner, reject_reason, manifest
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15, $16,
    $17, $18, $19::jsonb
)
RETURNING id
"""

_UPSERT_VARIANT_SQL = """
INSERT INTO memory_compressed_variants (
    memory_id, owner_id, winner_candidate_id,
    engine_id, engine_version, compressed_content,
    compressed_tokens, compression_ratio, quality_score,
    composite_score, scoring_profile, judge_model
) VALUES (
    $1, $2, $3,
    $4, $5, $6,
    $7, $8, $9,
    $10, $11, $12
)
ON CONFLICT (memory_id) DO UPDATE SET
    winner_candidate_id = EXCLUDED.winner_candidate_id,
    engine_id           = EXCLUDED.engine_id,
    engine_version      = EXCLUDED.engine_version,
    compressed_content  = EXCLUDED.compressed_content,
    compressed_tokens   = EXCLUDED.compressed_tokens,
    compression_ratio   = EXCLUDED.compression_ratio,
    quality_score       = EXCLUDED.quality_score,
    composite_score     = EXCLUDED.composite_score,
    scoring_profile     = EXCLUDED.scoring_profile,
    judge_model         = EXCLUDED.judge_model,
    selected_at         = NOW()
"""


def _nullable_positive(value: Optional[float]) -> Optional[float]:
    """Coerce 0.0 or None to None for fields where 0 would be misleading.

    speed_factor and composite_score are 0.0 on rejected candidates
    (disabled / error / no_output / quality_floor) because they were
    never scored. The DB column allows NULL, so record NULL to make
    the rejection visible instead of an artificial zero.
    """
    if value is None:
        return None
    return value if value > 0 else None


def _enriched_manifest(cand) -> Dict[str, Any]:
    """Return the engine's manifest augmented with a `_audit` block for
    non-winner candidates.

    Winners already carry every useful field in the typed columns
    (compression_ratio, quality_score, composite_score, elapsed_ms,
    gpu_used, engine_version). Non-winners lose data: errored
    candidates have empty manifests (the engine raised before it could
    populate one), quality_floor rejections drop the below-floor
    quality_score into obscurity, and the error text on failed runs
    only lives in `reject_reason='error'` (a bucket label, not the
    actual exception message).

    This function preserves every engine-authored manifest key and
    ADDS a single `_audit` object with:

      * reject_reason           — duplicated for programmatic access
      * error                   — full exception text when present
      * quality_score           — raw score that tripped the floor
      * compression_ratio       — achieved ratio even when rejected
      * elapsed_ms              — non-zero signals engine ran and failed
      * gpu_used                — whether GPU was consumed before failure
      * engine_version          — for post-hoc root-cause across deploys

    Keeping the audit under a namespaced key (`_audit`) avoids colliding
    with engine-chosen keys and makes it greppable in JSONB queries.
    """
    base = dict(cand.result.manifest or {})

    # Winners don't need enrichment — the typed columns are authoritative.
    if cand.is_winner:
        return base

    # Defensive: if an engine populated `_audit` with a non-dict value
    # (pathological but possible from a custom engine), start a fresh
    # dict rather than crashing on setdefault. The engine's prior value
    # is dropped into `_audit_original` for audit.
    existing = base.get("_audit")
    if not isinstance(existing, dict):
        if existing is not None:
            base["_audit_original"] = existing
        audit: Dict[str, Any] = {}
        base["_audit"] = audit
    else:
        audit = existing

    # Only set keys we don't already have, so engines that deliberately
    # populated `_audit.*` (unlikely but possible) aren't clobbered.
    audit.setdefault("reject_reason", cand.reject_reason)
    audit.setdefault("engine_version", cand.result.engine_version)

    if cand.result.error is not None:
        audit.setdefault("error", cand.result.error)

    # quality_score is the most useful single piece of context for
    # quality_floor rejections — captures how close the candidate came.
    if cand.result.quality_score is not None:
        audit.setdefault("quality_score", cand.result.quality_score)

    # compression_ratio survives even on quality_floor / inferior paths
    # and is useful for "engine X produced a ratio of Y but scored too
    # low on quality" forensics.
    if cand.result.compression_ratio is not None:
        audit.setdefault("compression_ratio", cand.result.compression_ratio)

    # Non-zero elapsed_ms on a failed candidate signals the engine
    # reached GPU / LLM before failing — useful for resource accounting.
    # Zero elapsed_ms on a 'disabled' or 'error' candidate means the
    # engine never dispatched (supports()=False or raised pre-call).
    if cand.result.elapsed_ms:
        audit.setdefault("elapsed_ms", cand.result.elapsed_ms)
        audit.setdefault("gpu_used", cand.result.gpu_used)

    return base


async def persist_contest(
    conn: Any,
    outcome: ContestOutcome,
    *,
    judge_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the contest outcome to the v3.1 compression tables.

    `conn` is an asyncpg Connection (not a Pool), and the CALLER is
    responsible for opening a transaction around this call. Persistence
    must be atomic with any follow-on queue-state update the worker
    issues; keeping the transaction boundary at the caller avoids a
    window where contest rows commit but the queue row never transitions
    to done/failed (see commit 9dfcdbf analysis: the v3.1.0-rc Codex
    review flagged this as a blocker).

    Typical caller shape:

        async with pool.acquire() as conn:
            async with conn.transaction():
                await persist_contest(conn, outcome, judge_model=...)
                await conn.execute(_MARK_DONE_SQL, queue_id)

    `judge_model` is used as a fallback for candidates whose result
    didn't record one. If the candidate already set judge_model, that
    value wins.

    Returns {'candidates_written', 'variant_written', 'contest_id',
    'winner_engine'} for the caller to log.
    """

    winner_candidate_db_id: Optional[Any] = None
    candidates_written = 0

    for cand in outcome.candidates:
        r = cand.result
        manifest_json = json.dumps(_enriched_manifest(cand))
        row = await conn.fetchrow(
            _INSERT_CANDIDATE_SQL,
            outcome.memory_id,
            outcome.owner_id,
            outcome.contest_id,
            r.engine_id,
            r.engine_version,
            r.compressed_content,
            r.original_tokens,
            r.compressed_tokens,
            r.compression_ratio,
            r.quality_score,
            _nullable_positive(cand.speed_factor),
            _nullable_positive(cand.composite_score),
            outcome.scoring_profile,
            r.elapsed_ms if r.elapsed_ms > 0 else None,
            r.judge_model or judge_model,
            r.gpu_used,
            cand.is_winner,
            cand.reject_reason,
            manifest_json,
        )
        candidates_written += 1
        if cand.is_winner:
            winner_candidate_db_id = row["id"]

    variant_written = False
    if outcome.winner is not None and winner_candidate_db_id is not None:
        w = outcome.winner
        r = w.result
        await conn.execute(
            _UPSERT_VARIANT_SQL,
            outcome.memory_id,
            outcome.owner_id,
            winner_candidate_db_id,
            r.engine_id,
            r.engine_version,
            r.compressed_content,
            r.compressed_tokens,
            r.compression_ratio,
            r.quality_score,
            w.composite_score,
            outcome.scoring_profile,
            r.judge_model or judge_model,
        )
        variant_written = True

    return {
        "contest_id": str(outcome.contest_id),
        "memory_id": outcome.memory_id,
        "candidates_written": candidates_written,
        "variant_written": variant_written,
        "winner_engine": outcome.winner.result.engine_id if outcome.winner else None,
    }


__all__ = ["persist_contest"]

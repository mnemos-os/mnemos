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

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from mnemos.db.eligibility import eligible_for_compression

from .contest import ContestOutcome

logger = logging.getLogger(__name__)


# Note on sha256: a previous revision routed compression commit hashes
# through ``mnemos_hot.sha256_batch``. Microbench on the actual call
# pattern (single-payload batches) showed the Rust path was ~7x slower
# than ``hashlib.sha256``: Python's hashlib already wraps OpenSSL in
# C with the GIL released, so the PyO3 marshalling cost of crossing
# the FFI for one payload dominates. The Rust path is gone here; the
# ``mnemos_hot`` wheel still exposes ``sha256_batch`` for any future
# call site that genuinely batches payloads (where the FFI cost
# amortizes), but ``_compression_commit_hash`` is naturally one-at-a-
# time and uses ``hashlib`` directly.


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

_FETCH_SOURCE_MAIN_HEAD_SQL = f"""
SELECT
    m.id AS memory_id,
    m.category,
    m.subcategory,
    m.metadata,
    m.verbatim_content,
    m.owner_id,
    m.namespace,
    m.permission_mode,
    m.source_model,
    m.source_provider,
    m.source_session,
    m.source_agent,
    mv.id AS parent_version_id,
    mv.commit_hash AS parent_commit_hash
FROM memory_branches mb
INNER JOIN memory_versions mv
    ON mv.id = mb.head_version_id
   AND mv.memory_id = mb.memory_id
INNER JOIN memories m
    ON m.id = mb.memory_id
WHERE mb.memory_id = $1
  AND mb.name = 'main'
  AND {eligible_for_compression('m', reject_private_parent=True)}
FOR UPDATE OF mb
"""

_INSERT_COMPRESSION_VERSION_SQL = """
INSERT INTO memory_versions (
    memory_id, version_num, content, category, subcategory, metadata,
    verbatim_content, owner_id, namespace, permission_mode,
    source_model, source_provider, source_session, source_agent,
    snapshot_by, change_type, commit_hash, branch, parent_version_id
) VALUES (
    $1,
    (
        SELECT COALESCE(MAX(version_num), 0) + 1
        FROM memory_versions
        WHERE memory_id = $1 AND branch = $15
    ),
    $2, $3, $4, $5::jsonb,
    $6, $7, $8, $9,
    $10, $11, $12, $13,
    'system:compression', 'compress', $14, $15, $16
)
ON CONFLICT (commit_hash) DO NOTHING
RETURNING id, version_num, commit_hash, parent_version_id, branch
"""

_FETCH_COMPRESSION_VERSION_BY_HASH_SQL = """
SELECT id, version_num, commit_hash, parent_version_id, branch
FROM memory_versions
WHERE memory_id = $1 AND branch = $2 AND commit_hash = $3
"""

_UPSERT_COMPRESSION_BRANCH_SQL = """
INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
VALUES ($1, $2, $3, 'system:compression')
ON CONFLICT (memory_id, name) DO UPDATE
SET head_version_id = EXCLUDED.head_version_id
"""


_DISTILLED_REPRESENTATION_KINDS = {
    "apollo_dense",
    "compressed",
    "compression",
    "dense",
    "distilled",
    "llm_dense",
    "raw",
    "raw_compression",
    "schema_dense",
    "structured_dense",
}
_NARRATED_REPRESENTATION_KINDS = {
    "abstractive_prose",
    "extractive_prose",
    "narrated",
    "narration",
    "natural_language",
    "prose",
    "prose_narration",
}
_REPRESENTATION_KIND_KEYS = (
    "representation_kind",
    "representation",
    "output_kind",
    "format",
)


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


def _normalize_representation_kind(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _variant_branch(result: Any) -> str:
    manifest = result.manifest or {}
    for key in _REPRESENTATION_KIND_KEYS:
        kind = _normalize_representation_kind(manifest.get(key))
        if kind in _NARRATED_REPRESENTATION_KINDS:
            return "narrated"
        if kind in _DISTILLED_REPRESENTATION_KINDS:
            return "distilled"

    # Current built-ins: APOLLO emits dense LLM-to-LLM forms; ARTEMIS
    # and third-party extractive engines generally emit prose-shaped
    # variants unless they explicitly mark a denser representation.
    if result.engine_id == "apollo":
        return "distilled"
    return "narrated"


def _compression_commit_hash(
    parent_commit_hash: str,
    variant_content: str,
    branch: str,
) -> str:
    payload = (parent_commit_hash + variant_content + branch).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_batch_python(payloads: list[bytes]) -> list[str]:
    return [hashlib.sha256(payload).hexdigest() for payload in payloads]


def _jsonb_arg(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value)


async def _persist_compression_version(
    conn: Any,
    *,
    memory_id: str,
    result: Any,
) -> Dict[str, Any]:
    variant_content = result.compressed_content
    if variant_content is None:
        raise RuntimeError(
            f"winner for memory {memory_id} has no compressed_content; "
            "cannot create compression DAG version"
        )

    branch = _variant_branch(result)

    # This direct memory_versions insert does not mutate memories, but
    # keep the transaction-local guard set so any future trigger path in
    # the same unit of work cannot double-snapshot this derivation.
    await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")

    source = await conn.fetchrow(_FETCH_SOURCE_MAIN_HEAD_SQL, memory_id)
    if source is None:
        raise RuntimeError(
            f"main branch HEAD missing for memory {memory_id}; "
            "cannot create compression DAG version"
        )

    parent_commit_hash = source["parent_commit_hash"]
    commit_hash = _compression_commit_hash(
        parent_commit_hash,
        variant_content,
        branch,
    )
    version_row = await conn.fetchrow(
        _INSERT_COMPRESSION_VERSION_SQL,
        memory_id,
        variant_content,
        source["category"],
        source["subcategory"],
        _jsonb_arg(source["metadata"]),
        source["verbatim_content"],
        source["owner_id"],
        source["namespace"],
        source["permission_mode"],
        source["source_model"],
        source["source_provider"],
        source["source_session"],
        source["source_agent"],
        commit_hash,
        branch,
        source["parent_version_id"],
    )
    if version_row is None:
        version_row = await conn.fetchrow(
            _FETCH_COMPRESSION_VERSION_BY_HASH_SQL,
            memory_id,
            branch,
            commit_hash,
        )
    if version_row is None:
        raise RuntimeError(
            f"compression DAG version insert produced no row for memory "
            f"{memory_id} branch {branch}"
        )

    await conn.execute(
        _UPSERT_COMPRESSION_BRANCH_SQL,
        memory_id,
        branch,
        version_row["id"],
    )

    return {
        "compression_version_id": str(version_row["id"]),
        "compression_version_branch": branch,
        "compression_version_commit_hash": version_row["commit_hash"],
        "compression_parent_version_id": str(version_row["parent_version_id"]),
    }


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
    version_result: Dict[str, Any] = {}
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
        version_result = await _persist_compression_version(
            conn,
            memory_id=outcome.memory_id,
            result=r,
        )

    result = {
        "contest_id": str(outcome.contest_id),
        "memory_id": outcome.memory_id,
        "candidates_written": candidates_written,
        "variant_written": variant_written,
        "winner_engine": outcome.winner.result.engine_id if outcome.winner else None,
    }
    result.update(version_result)
    return result


__all__ = ["persist_contest"]

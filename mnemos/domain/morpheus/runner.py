"""MORPHEUS run orchestrator.

Creates a morpheus_runs row, walks through the configured phases, and
commits status + counters as it goes. Each phase is a separate async
function; the runner tags every memory mutation with morpheus_run_id so
rollback can delete run-created rows and restore in-place mutations.

The default pipeline is REPLAY → CLUSTER → SYNTHESISE → COMMIT. The
optional CONSOLIDATE phase can be enabled between CLUSTER and SYNTHESISE
once an operator is ready for soft mutation paths.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple
from uuid import UUID, uuid4

import asyncpg
import numpy as np

from mnemos.core.config import get_settings, hot_rs_enabled
from mnemos.core.ids import new_memory_id
from mnemos.db.eligibility import eligible_for_morpheus

logger = logging.getLogger(__name__)

_PRE_CONSOLIDATE_PERMISSION_KEY = "pre_consolidate_permission_mode"
_CONSOLIDATED_PERMISSION_MODE = 400

# Optional Rust hot-path accelerator. Loaded lazily so the absence of
# the wheel on a given build host does NOT break the import - the
# Python implementation below stays the source of truth.
# Opt-in via env var MNEMOS_HOT_RS_ENABLED=1; default off until soak.
_HOT_RS = None
_HOT_RS_ENABLED = hot_rs_enabled()
if _HOT_RS_ENABLED:
    try:
        import mnemos_hot as _HOT_RS  # type: ignore[import-not-found]
        logger.info(
            "mnemos_hot Rust accelerator enabled (MORPHEUS clustering will use mnemos_hot %s)",
            getattr(_HOT_RS, "__version__", "?"),
        )
    except ImportError as _exc:
        logger.warning(
            "MNEMOS_HOT_RS_ENABLED=1 but mnemos_hot wheel is not importable: %s. "
            "Falling back to pure-Python MORPHEUS cosine clustering.",
            _exc,
        )
        _HOT_RS = None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two 1-D float vectors. Returns 0.0 if
    either vector has zero norm — keeps clustering deterministic when
    a degenerate embedding sneaks in."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _vector_to_float_list(vector: np.ndarray) -> list[float]:
    return [float(value) for value in vector]


def _cosine_similarities(query: np.ndarray, candidates: list[np.ndarray]) -> list[float]:
    """Score one query vector against candidate vectors.

    When opted in, dispatch the row-vs-clusters scoring work to the
    Rust batch helper. Any import/runtime mismatch falls back to the
    per-pair Python implementation, which remains the source of truth.
    """
    if _HOT_RS is not None:
        try:
            # The existing NumPy path raises on non-zero length
            # mismatches. Keep that behavior by avoiding Rust's
            # length-mismatch-to-0.0 semantics for this caller.
            if any(len(candidate) != len(query) for candidate in candidates):
                return [_cosine_similarity(query, candidate) for candidate in candidates]
            query_values = _vector_to_float_list(query)
            candidate_values = [_vector_to_float_list(candidate) for candidate in candidates]
            try:
                normalized = _HOT_RS.normalize_embeddings([query_values, *candidate_values])
                if len(normalized) == len(candidate_values) + 1:
                    query_values = [float(value) for value in normalized[0]]
                    candidate_values = [
                        [float(value) for value in vector]
                        for vector in normalized[1:]
                    ]
            except Exception:
                pass
            scores = _HOT_RS.cosine_batch(
                query_values,
                candidate_values,
            )
            if len(scores) == len(candidates):
                return [float(score) for score in scores]
        except Exception:
            pass
    return [_cosine_similarity(query, candidate) for candidate in candidates]


def _parse_pgvector(raw: object) -> Optional[np.ndarray]:
    """asyncpg returns a pgvector column as the literal text "[0.1, 0.2, ...]"
    when the type is not registered. Parse it to a float32 ndarray. Returns
    None if the value is null or unparseable."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return np.asarray(raw, dtype=np.float32)
    if isinstance(raw, str):
        try:
            return np.asarray(json.loads(raw), dtype=np.float32)
        except (ValueError, json.JSONDecodeError):
            return None
    return None


def _parse_run_config(config_raw: object) -> dict:
    if config_raw is None:
        return {}
    if isinstance(config_raw, str):
        try:
            parsed = json.loads(config_raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return config_raw if isinstance(config_raw, dict) else {}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _command_count(result: str) -> int:
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def _metadata_has_key(raw: object, key: str) -> bool:
    if isinstance(raw, dict):
        return key in raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict) and key in parsed
    return False


def _consolidate_enabled(config: Optional[dict]) -> bool:
    configured = config.get("consolidate", False) if isinstance(config, dict) else False
    return _truthy(configured) or bool(get_settings().morpheus.consolidate)


def _extract_enabled(config: Optional[dict]) -> bool:
    configured = config.get("extract", False) if isinstance(config, dict) else False
    return _truthy(configured) or bool(get_settings().morpheus.extract)


def _extract_verify_enabled(config: Optional[dict]) -> bool:
    configured = config.get("extract_verify", False) if isinstance(config, dict) else False
    return _truthy(configured) or bool(get_settings().morpheus.extract_verify)


@dataclass(frozen=True)
class ExtractedTriple:
    subject: str
    predicate: str
    object: str
    confidence: float


async def begin_run(
    pool: asyncpg.Pool,
    *,
    triggered_by: str = "cron",
    window_hours: int = 168,
    cluster_min_size: int = 3,
    config: Optional[dict] = None,
    namespace: Optional[str] = None,
) -> str:
    """Open a new MORPHEUS run row and return its UUID as a string.

    Caller is responsible for advancing the row through phases via
    set_phase() and finalising via finish_run() (or fail_run() on
    exception). The row is created with status='running' so an inspector
    polling /v1/morpheus/runs sees the dream in flight.

    `namespace`, when set, scopes the run to memories with that
    `namespace` value. NULL = "all namespaces" (the default — matches
    the historical behavior before per-namespace scoping).
    """
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=window_hours)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO morpheus_runs
                (triggered_by, window_started_at, window_ended_at,
                 window_hours, cluster_min_size, config, namespace)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            RETURNING id
            """,
            triggered_by, window_start, window_end,
            window_hours, cluster_min_size,
            json.dumps(config or {}),
            namespace,
        )
    run_id = str(row["id"])
    logger.info(
        "[MORPHEUS] run %s opened (window=%dh, triggered_by=%s, namespace=%s)",
        run_id, window_hours, triggered_by, namespace or "<all>",
    )
    return run_id


async def set_phase(pool: asyncpg.Pool, run_id: str, phase: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET phase=$2 WHERE id=$1::uuid",
            run_id, phase,
        )
    logger.info("[MORPHEUS] run %s → phase=%s", run_id, phase)


async def update_counters(
    pool: asyncpg.Pool,
    run_id: str,
    *,
    memories_scanned: Optional[int] = None,
    clusters_found: Optional[int] = None,
    summaries_created: Optional[int] = None,
    memories_consolidated: Optional[int] = None,
    clusters_consolidated: Optional[int] = None,
    triples_extracted: Optional[int] = None,
    memories_processed_for_extraction: Optional[int] = None,
) -> None:
    """Bump counters as phases finish. Pass only the fields to update."""
    sets: list[str] = []
    args: list = []
    if memories_scanned is not None:
        args.append(memories_scanned)
        sets.append(f"memories_scanned=${len(args)}")
    if clusters_found is not None:
        args.append(clusters_found)
        sets.append(f"clusters_found=${len(args)}")
    if summaries_created is not None:
        args.append(summaries_created)
        sets.append(f"summaries_created=${len(args)}")
    if memories_consolidated is not None:
        args.append(memories_consolidated)
        sets.append(f"memories_consolidated=${len(args)}")
    if clusters_consolidated is not None:
        args.append(clusters_consolidated)
        sets.append(f"clusters_consolidated=${len(args)}")
    if triples_extracted is not None:
        args.append(triples_extracted)
        sets.append(f"triples_extracted=${len(args)}")
    if memories_processed_for_extraction is not None:
        args.append(memories_processed_for_extraction)
        sets.append(f"memories_processed_for_extraction=${len(args)}")
    if not sets:
        return
    args.append(run_id)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE morpheus_runs SET {', '.join(sets)} "
            f"WHERE id=${len(args)}::uuid",
            *args,
        )


async def finish_run(pool: asyncpg.Pool, run_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET status='success', finished_at=now() "
            "WHERE id=$1::uuid",
            run_id,
        )
    logger.info("[MORPHEUS] run %s finished SUCCESS", run_id)


async def fail_run(pool: asyncpg.Pool, run_id: str, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET status='failed', finished_at=now(), error=$2 "
            "WHERE id=$1::uuid",
            run_id, error[:4000],
        )
    logger.warning("[MORPHEUS] run %s finished FAILED: %s", run_id, error[:200])


async def rollback_run(pool: asyncpg.Pool, run_id: str) -> Tuple[int, int]:
    """Undo every memory mutation tagged with this run and roll it back.

    Returns (memories_deleted, run_rows_updated).

    Synthesis rows are run-created and still deleted. Consolidated
    originals are restored in place from their metadata audit before
    that delete runs, so rollback never hard-deletes user memories.
    """
    try:
        UUID(run_id)
    except (ValueError, TypeError):
        raise ValueError(f"invalid run_id: {run_id!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            extract_reset_result = await conn.execute(
                """
                WITH deleted_extract_triples AS (
                    DELETE FROM kg_triples
                    WHERE extracted_by_run_id=$1::uuid
                    RETURNING memory_id
                ), run_memories AS (
                    DELETE FROM morpheus_extract_run_memories
                    WHERE run_id=$1::uuid
                    RETURNING memory_id
                ), affected_memories AS (
                    SELECT memory_id FROM deleted_extract_triples
                    UNION
                    SELECT memory_id FROM run_memories
                )
                UPDATE memories
                SET triples_extracted_at = NULL
                WHERE id IN (
                    SELECT DISTINCT memory_id
                    FROM affected_memories
                    WHERE memory_id IS NOT NULL
                )
                """,
                run_id,
            )
            n_extract_reset = _command_count(extract_reset_result)
            restore_result = await conn.execute(
                """
                UPDATE memories
                SET consolidated_into = NULL,
                    consolidated_at = NULL,
                    permission_mode = COALESCE(
                        (metadata->>$2)::int,
                        permission_mode
                    ),
                    metadata = COALESCE(metadata, '{}'::jsonb) - $2,
                    morpheus_run_id = NULL
                WHERE morpheus_run_id=$1::uuid
                  AND deleted_at IS NULL
                  AND COALESCE(metadata, '{}'::jsonb) ? $2
                """,
                run_id, _PRE_CONSOLIDATE_PERMISSION_KEY,
            )
            n_restored = _command_count(restore_result)
            # Per-row tagging means rollback never crosses runs.
            del_result = await conn.execute(
                "DELETE FROM memories WHERE morpheus_run_id=$1::uuid "
                "AND provenance='morpheus_local' "
                "AND deleted_at IS NULL",
                run_id,
            )
            n_deleted = _command_count(del_result)
            run_result = await conn.execute(
                "UPDATE morpheus_runs "
                "SET status='rolled_back', finished_at=COALESCE(finished_at, now()) "
                "WHERE id=$1::uuid",
                run_id,
            )
            n_run = _command_count(run_result)
    logger.warning(
        "[MORPHEUS] run %s rolled back: %d memories deleted, "
        "%d consolidated rows restored, %d extraction markers reset",
        run_id, n_deleted, n_restored, n_extract_reset,
    )
    return n_deleted, n_run


# ── Phases ────────────────────────────────────────────────────────────────────
#
# Each phase function below carries out one stage of the REPLAY →
# CLUSTER → optional CONSOLIDATE → SYNTHESISE → COMMIT pipeline. Every
# memory the run creates or mutates is tagged with its ``morpheus_run_id``
# so rollback can remain scoped to the run and ``morpheus_runs`` stays
# authoritative for what the run did.

async def phase_replay(pool: asyncpg.Pool, run_id: str) -> int:
    """Scan memories from the run's window. Returns count scanned.

    When the run has `namespace` set, the scan is scoped to memories
    with that namespace; NULL means "all namespaces".
    """
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            f"""
            SELECT COUNT(*)
            FROM memories m
            JOIN morpheus_runs r ON r.id = $1::uuid
            WHERE m.created BETWEEN r.window_started_at AND r.window_ended_at
              AND m.provenance IS DISTINCT FROM 'morpheus_local'
              AND m.morpheus_run_id IS NULL
              AND {eligible_for_morpheus('m')}
              AND (r.namespace IS NULL OR m.namespace = r.namespace)
            """,
            run_id,
        )
    await update_counters(pool, run_id, memories_scanned=int(n or 0))
    return int(n or 0)


async def phase_cluster(pool: asyncpg.Pool, run_id: str) -> int:
    """Cosine-cluster the replayed memories. Returns cluster count.

    Single-pass online clustering: walk memories in created order, for
    each one find the existing cluster whose centroid has the highest
    cosine similarity; if >= threshold add and update the centroid as
    a running mean, else open a new cluster. Filter clusters smaller
    than the run's cluster_min_size before persisting.

    Threshold default 0.85, override via MNEMOS_MORPHEUS_CLUSTER_THRESHOLD.

    Surviving clusters are serialized into morpheus_runs.config under
    key "clusters" so phase_synthesise can consume them without a
    separate table.
    """
    threshold = get_settings().morpheus.cluster_threshold

    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT cluster_min_size, window_started_at, window_ended_at, "
            "       namespace "
            "FROM morpheus_runs WHERE id=$1::uuid",
            run_id,
        )
        if run_row is None:
            await update_counters(pool, run_id, clusters_found=0)
            return 0
        min_size = int(run_row["cluster_min_size"])

        rows = await conn.fetch(
            f"""
            SELECT id, embedding::text AS embedding
            FROM memories
            WHERE created BETWEEN $1 AND $2
              AND provenance IS DISTINCT FROM 'morpheus_local'
              AND morpheus_run_id IS NULL
              AND embedding IS NOT NULL
              AND {eligible_for_morpheus('')}
              AND ($3::text IS NULL OR namespace = $3)
            ORDER BY created
            """,
            run_row["window_started_at"], run_row["window_ended_at"],
            run_row["namespace"],
        )

    if not rows:
        await update_counters(pool, run_id, clusters_found=0)
        return 0

    clusters: List[dict] = []  # [{"centroid": ndarray, "members": [memory_ids]}]
    for row in rows:
        vec = _parse_pgvector(row["embedding"])
        if vec is None:
            continue
        if not clusters:
            clusters.append({"centroid": vec.copy(), "members": [row["id"]]})
            continue
        best_idx = -1
        best_sim = -1.0
        scores = _cosine_similarities(vec, [cl["centroid"] for cl in clusters])
        for i, sim in enumerate(scores):
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_sim >= threshold:
            cl = clusters[best_idx]
            n = len(cl["members"])
            # Running mean update of the centroid (not the more accurate
            # but more expensive per-step recompute — clusters are small).
            cl["centroid"] = (cl["centroid"] * n + vec) / (n + 1)
            cl["members"].append(row["id"])
        else:
            clusters.append({"centroid": vec.copy(), "members": [row["id"]]})

    surviving = [c for c in clusters if len(c["members"]) >= min_size]
    cluster_payload = [
        {"cluster_id": i, "member_memory_ids": c["members"]}
        for i, c in enumerate(surviving)
    ]

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE morpheus_runs
            SET config = config || jsonb_build_object('clusters', $2::jsonb)
            WHERE id=$1::uuid
            """,
            run_id, json.dumps(cluster_payload),
        )

    n_clusters = len(surviving)
    await update_counters(pool, run_id, clusters_found=n_clusters)
    logger.info(
        "[MORPHEUS] run %s clustered %d memories into %d cluster(s) "
        "(threshold=%.2f, min_size=%d, dropped %d below min)",
        run_id, len(rows), n_clusters, threshold, min_size,
        len(clusters) - n_clusters,
    )
    return n_clusters


async def phase_consolidate(pool: asyncpg.Pool, run_id: str) -> int:
    """Soft-merge duplicate cluster members into a canonical memory.

    Reads the cluster payload written by phase_cluster. For each
    cluster at or above cluster_min_size, the canonical is the live,
    unconsolidated member with highest recall_count, tie-broken by the
    earliest created timestamp. Non-canonical live members are updated
    in place to point at the canonical, made owner-read-only, and tagged
    with the run id for rollback.

    Returns the number of memories newly or previously consolidated by
    this run. Running the phase again for the same run does not mutate
    rows a second time and leaves counters stable.
    """
    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT config, cluster_min_size, namespace "
            "FROM morpheus_runs WHERE id=$1::uuid",
            run_id,
        )
    if run_row is None:
        await update_counters(
            pool,
            run_id,
            memories_consolidated=0,
            clusters_consolidated=0,
        )
        return 0

    config = _parse_run_config(run_row["config"])
    clusters = config.get("clusters", []) if isinstance(config, dict) else []
    if not clusters:
        await update_counters(
            pool,
            run_id,
            memories_consolidated=0,
            clusters_consolidated=0,
        )
        return 0

    min_size = int(run_row["cluster_min_size"])
    namespace = run_row["namespace"]
    memories_consolidated = 0
    clusters_consolidated = 0

    for cluster in clusters:
        member_ids = [str(mid) for mid in cluster.get("member_memory_ids", []) if mid]
        if len(member_ids) < min_size:
            continue

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, recall_count, created, permission_mode,
                       consolidated_into, morpheus_run_id, metadata
                FROM memories
                WHERE id = ANY($1::text[])
                  AND {eligible_for_morpheus('')}
                  AND ($2::text IS NULL OR namespace = $2)
                """,
                member_ids, namespace,
            )
        if len(rows) < min_size:
            already_count = 0
            if len(rows) == 1:
                async with pool.acquire() as conn:
                    already_count = int(await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM memories
                        WHERE id = ANY($1::text[])
                          AND deleted_at IS NULL
                          AND archived_at IS NULL
                          AND consolidated_into=$2
                          AND morpheus_run_id=$3::uuid
                          AND COALESCE(metadata, '{}'::jsonb)
                              ? 'pre_consolidate_permission_mode'
                          AND ($4::text IS NULL OR namespace=$4)
                        """,
                        member_ids,
                        str(rows[0]["id"]),
                        run_id,
                        namespace,
                    ) or 0)
            if len(rows) + already_count < min_size:
                continue

        canonical = sorted(
            rows,
            key=lambda row: (
                -int(row["recall_count"] or 0),
                row["created"],
                str(row["id"]),
            ),
        )[0]
        canonical_id = str(canonical["id"])
        async with pool.acquire() as conn:
            cluster_count = int(await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memories
                WHERE id = ANY($1::text[])
                  AND deleted_at IS NULL
                  AND archived_at IS NULL
                  AND consolidated_into=$2
                  AND morpheus_run_id=$3::uuid
                  AND COALESCE(metadata, '{}'::jsonb)
                      ? 'pre_consolidate_permission_mode'
                  AND ($4::text IS NULL OR namespace=$4)
                """,
                member_ids,
                canonical_id,
                run_id,
                namespace,
            ) or 0)

        for row in rows:
            member_id = str(row["id"])
            if member_id == canonical_id:
                continue

            async with pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE memories
                    SET consolidated_into=$2,
                        consolidated_at=NOW(),
                        permission_mode=$5,
                        morpheus_run_id=$3::uuid,
                        metadata = CASE
                            WHEN COALESCE(metadata, '{}'::jsonb)
                                 ? 'pre_consolidate_permission_mode'
                            THEN COALESCE(metadata, '{}'::jsonb)
                            ELSE jsonb_set(
                                COALESCE(metadata, '{}'::jsonb),
                                '{pre_consolidate_permission_mode}',
                                to_jsonb(permission_mode),
                                true
                            )
                        END
                    WHERE id=$1
                      AND deleted_at IS NULL
                      AND archived_at IS NULL
                      AND consolidated_into IS NULL
                      AND morpheus_run_id IS NULL
                      AND ($4::text IS NULL OR namespace=$4)
                    """,
                    member_id,
                    canonical_id,
                    run_id,
                    namespace,
                    _CONSOLIDATED_PERMISSION_MODE,
                )
            cluster_count += _command_count(result)

        if cluster_count:
            memories_consolidated += cluster_count
            clusters_consolidated += 1

    await update_counters(
        pool,
        run_id,
        memories_consolidated=memories_consolidated,
        clusters_consolidated=clusters_consolidated,
    )
    logger.info(
        "[MORPHEUS] run %s consolidated %d memor%s across %d cluster(s)",
        run_id,
        memories_consolidated,
        "y" if memories_consolidated == 1 else "ies",
        clusters_consolidated,
    )
    return memories_consolidated


async def phase_synthesise(pool: asyncpg.Pool, run_id: str) -> int:
    """Generate summary memories per cluster. Returns count created.

    Reads the cluster payload phase_cluster wrote to morpheus_runs.config.
    For each cluster:

      1. Fetches member contents + category + owner_id from memories.
      2. Synthesises a summary string (deterministic by default;
         LLM-driven when MNEMOS_MORPHEUS_USE_LLM=true — matches the
         APOLLO LLM-fallback gate pattern).
      3. Inserts a new memory with:
           - morpheus_run_id        = run_id
           - source_memories        = [member ids]
           - provenance             = 'morpheus_local'
           - category / owner / ns  = inherited from cluster majority
           - subcategory            = 'morpheus-synthesis'

    All inserts are append-only and tagged with morpheus_run_id, so
    rollback_run() can delete them without touching user originals.
    """
    use_llm = get_settings().morpheus.use_llm

    async with pool.acquire() as conn:
        config_raw = await conn.fetchval(
            "SELECT config FROM morpheus_runs WHERE id=$1::uuid", run_id,
        )
    if config_raw is None:
        await update_counters(pool, run_id, summaries_created=0)
        return 0
    config = _parse_run_config(config_raw)
    clusters = config.get("clusters", []) if isinstance(config, dict) else []
    if not clusters:
        await update_counters(pool, run_id, summaries_created=0)
        return 0

    n_created = 0
    for cluster in clusters:
        member_ids = cluster.get("member_memory_ids", [])
        if not member_ids:
            continue

        async with pool.acquire() as conn:
            members = await conn.fetch(
                f"""
                SELECT id, content, category, owner_id, namespace
                FROM memories
                WHERE id = ANY($1::text[])
                  AND {eligible_for_morpheus('')}
                """,
                member_ids,
            )
        if not members:
            continue
        visible_member_ids = [str(m["id"]) for m in members]

        summary = await _synthesise_cluster_summary(
            [m["content"] for m in members], use_llm=use_llm,
        )

        # Inherit category/owner/namespace from the cluster majority,
        # tie-broken by first-occurrence so rollback is deterministic.
        category = _majority([m["category"] for m in members])
        owner_id = _majority([m["owner_id"] for m in members]) or "default"
        namespace = _majority([m["namespace"] for m in members]) or "default"

        new_id = new_memory_id()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memories
                    (id, content, category, subcategory, metadata,
                     quality_rating, verbatim_content,
                     owner_id, namespace, permission_mode,
                     morpheus_run_id, source_memories, provenance)
                VALUES ($1, $2, $3, $4, $5::jsonb, 75, $2,
                        $6, $7, 600,
                        $8::uuid, $9::text[], 'morpheus_local')
                """,
                new_id, summary, category, "morpheus-synthesis",
                json.dumps({
                    "morpheus_run_id": run_id,
                    "cluster_id": cluster.get("cluster_id"),
                    "member_count": len(visible_member_ids),
                    "synthesis_mode": "llm" if use_llm else "extractive",
                }),
                owner_id, namespace,
                run_id, visible_member_ids,
            )
        n_created += 1

    await update_counters(pool, run_id, summaries_created=n_created)
    logger.info(
        "[MORPHEUS] run %s synthesised %d summary memor%s "
        "(mode=%s)",
        run_id, n_created, "y" if n_created == 1 else "ies",
        "llm" if use_llm else "extractive",
    )
    return n_created


async def phase_extract(pool: asyncpg.Pool, run_id: str) -> int:
    """Extract latent KG triples from unprocessed prose memories.

    The phase is opt-in via run config (`extract=true`) or
    MNEMOS_MORPHEUS_EXTRACT. Each source memory is processed at most
    once by the `triples_extracted_at` guard. The LLM calls happen
    outside DB transactions; the timestamp mark and all triple inserts
    for one memory commit atomically.
    """
    settings = get_settings().morpheus
    min_chars = max(0, int(settings.extract_min_chars))

    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT config, namespace FROM morpheus_runs WHERE id=$1::uuid",
            run_id,
        )
    if run_row is None:
        await update_counters(
            pool,
            run_id,
            triples_extracted=0,
            memories_processed_for_extraction=0,
        )
        return 0

    config = _parse_run_config(run_row["config"])
    if not _extract_enabled(config):
        await update_counters(
            pool,
            run_id,
            triples_extracted=0,
            memories_processed_for_extraction=0,
        )
        return 0

    namespace = run_row["namespace"]
    verify = _extract_verify_enabled(config)
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            f"""
            SELECT id, verbatim_content, owner_id, namespace
            FROM memories
            WHERE {eligible_for_morpheus('')}
              AND triples_extracted_at IS NULL
              AND verbatim_content IS NOT NULL
              AND length(verbatim_content) >= $1
              AND ($2::text IS NULL OR namespace = $2)
            ORDER BY created
            """,
            min_chars, namespace,
        )

    memories_processed = 0
    triples_extracted = 0

    for row in candidates:
        memory_id = str(row["id"])
        content = str(row["verbatim_content"] or "")
        triples = await _extract_triples_from_prose(content)
        if verify and triples:
            triples = await _verify_extracted_triples(content, triples)

        async with pool.acquire() as conn:
            async with conn.transaction():
                marked_id = await conn.fetchval(
                    f"""
                    UPDATE memories
                    SET triples_extracted_at = NOW()
                    WHERE id=$1
                      AND triples_extracted_at IS NULL
                      AND {eligible_for_morpheus('')}
                      AND ($2::text IS NULL OR namespace = $2)
                    RETURNING id
                    """,
                    memory_id, namespace,
                )
                if marked_id is None:
                    continue

                await conn.execute(
                    """
                    INSERT INTO morpheus_extract_run_memories
                        (run_id, memory_id)
                    VALUES ($1::uuid, $2)
                    ON CONFLICT (run_id, memory_id) DO UPDATE
                    SET processed_at = EXCLUDED.processed_at
                    """,
                    run_id,
                    memory_id,
                )

                for triple in triples:
                    await conn.execute(
                        """
                        INSERT INTO kg_triples
                            (id, subject, predicate, object,
                             memory_id, confidence, extracted_by_run_id,
                             owner_id, namespace)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::uuid, $8, $9)
                        """,
                        _new_kg_triple_id(),
                        triple.subject,
                        triple.predicate,
                        triple.object,
                        memory_id,
                        triple.confidence,
                        run_id,
                        row["owner_id"],
                        row["namespace"],
                    )

        memories_processed += 1
        triples_extracted += len(triples)

    await update_counters(
        pool,
        run_id,
        triples_extracted=triples_extracted,
        memories_processed_for_extraction=memories_processed,
    )
    logger.info(
        "[MORPHEUS] run %s extracted %d KG triple(s) from %d prose memor%s",
        run_id,
        triples_extracted,
        memories_processed,
        "y" if memories_processed == 1 else "ies",
    )
    return triples_extracted


def _new_kg_triple_id() -> str:
    try:
        from mnemos.core import ids as _ids

        factory = getattr(_ids, "new_kg_triple_id", None)
        if callable(factory):
            return str(factory())
    except Exception:
        pass
    return str(uuid4())


async def _extract_triples_from_prose(verbatim_content: str) -> list[ExtractedTriple]:
    settings = get_settings().morpheus
    prompt = (
        "Extract latent knowledge-graph triples from the prose memory below.\n"
        "Return ONLY strict JSON: an array of objects with exactly these keys: "
        "subject, predicate, object, confidence.\n"
        "Rules: subject, predicate, and object must be non-empty strings; "
        "confidence must be a number from 0 to 1; include only triples directly "
        "supported by the memory; do not include commentary, markdown, or code fences.\n\n"
        "Memory:\n"
        '"""\n'
        f"{verbatim_content}\n"
        '"""'
    )
    raw = await _call_morpheus_muse(
        prompt,
        muse=settings.extract_muse,
        task_type="kg_extraction",
        timeout=120,
    )
    return _parse_extracted_triples(raw)


async def _verify_extracted_triples(
    verbatim_content: str,
    triples: list[ExtractedTriple],
) -> list[ExtractedTriple]:
    if not triples:
        return []
    settings = get_settings().morpheus
    min_confidence = float(settings.extract_min_confidence)
    payload = [
        {
            "index": idx,
            "subject": triple.subject,
            "predicate": triple.predicate,
            "object": triple.object,
            "confidence": triple.confidence,
        }
        for idx, triple in enumerate(triples)
    ]
    prompt = (
        "Verify whether each proposed knowledge-graph triple is directly supported "
        "by the prose memory. Return ONLY strict JSON: an array of objects with "
        "index and confidence keys. Confidence must be a number from 0 to 1. "
        "Do not include commentary, markdown, or code fences.\n\n"
        "Memory:\n"
        '"""\n'
        f"{verbatim_content}\n"
        '"""\n\n'
        "Triples:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    raw = await _call_morpheus_muse(
        prompt,
        muse=settings.extract_verifier,
        task_type="kg_extraction_verification",
        timeout=180,
    )
    verified_confidences = _parse_verifier_confidences(raw, len(triples))
    out: list[ExtractedTriple] = []
    for idx, triple in enumerate(triples):
        confidence = verified_confidences.get(idx, triple.confidence)
        if confidence >= min_confidence:
            out.append(replace(triple, confidence=confidence))
    return out


async def _call_morpheus_muse(
    prompt: str,
    *,
    muse: str,
    task_type: str,
    timeout: int,
) -> str:
    try:
        from mnemos.domain.graeae.engine import get_graeae_engine

        engine = get_graeae_engine()
        result = await engine.consult(
            prompt=prompt,
            task_type=task_type,
            timeout=timeout,
            selection=_morpheus_muse_selection(engine, muse),
            mode="single",
        )
        consensus = result.get("consensus_response")
        if isinstance(consensus, str) and consensus.strip():
            return consensus.strip()
        for response in (result.get("all_responses") or {}).values():
            if response.get("status") in {"success", "ok"} and response.get("response_text"):
                return str(response["response_text"]).strip()
    except Exception as exc:  # pragma: no cover - defensive around external LLMs
        logger.warning("[MORPHEUS] extract muse call failed: %s", exc)
    return ""


def _morpheus_muse_selection(engine: Any, muse: str) -> Optional[dict[str, Optional[str]]]:
    muse = str(muse or "").strip()
    if not muse or muse == "auto":
        return None
    providers = getattr(engine, "providers", {}) or {}
    if muse in providers:
        return {muse: None}
    for provider_name, cfg in providers.items():
        if str(cfg.get("model") or "") == muse:
            return {provider_name: muse}

    from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP

    registry_to_graeae = {
        cfg["registry_provider"]: name
        for name, cfg in GRAEAE_REGISTRY_MAP.items()
    }
    provider_name = registry_to_graeae.get(muse)
    if provider_name in providers:
        return {provider_name: None}

    logger.warning(
        "[MORPHEUS] configured muse %r is not in the GRAEAE provider map; "
        "falling back to single best available muse",
        muse,
    )
    return None


def _parse_extracted_triples(raw: object) -> list[ExtractedTriple]:
    parsed = _json_array(raw)
    if parsed is None:
        return []
    triples: list[ExtractedTriple] = []
    for item in parsed:
        triple = _validated_triple(item)
        if triple is not None:
            triples.append(triple)
    return triples


def _parse_verifier_confidences(raw: object, n_triples: int) -> dict[int, float]:
    parsed = _json_array(raw)
    if parsed is None:
        return {}
    confidences: dict[int, float] = {}
    for fallback_idx, item in enumerate(parsed):
        if isinstance(item, dict):
            raw_idx = item.get("index", fallback_idx)
            raw_confidence = item.get("confidence")
        else:
            raw_idx = fallback_idx
            raw_confidence = item
        if not isinstance(raw_idx, int) or not 0 <= raw_idx < n_triples:
            continue
        confidence = _validated_confidence(raw_confidence)
        if confidence is None:
            continue
        confidences[raw_idx] = confidence
    return confidences


def _json_array(raw: object) -> Optional[list]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        parsed = raw
    return parsed if isinstance(parsed, list) else None


def _validated_triple(item: object) -> Optional[ExtractedTriple]:
    if not isinstance(item, dict):
        return None
    required = {"subject", "predicate", "object", "confidence"}
    if not required.issubset(item):
        return None
    subject = _validated_text(item.get("subject"))
    predicate = _validated_text(item.get("predicate"))
    obj = _validated_text(item.get("object"))
    confidence = _validated_confidence(item.get("confidence"))
    if subject is None or predicate is None or obj is None or confidence is None:
        return None
    return ExtractedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        confidence=confidence,
    )


def _validated_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _validated_confidence(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _majority(values: List[str]) -> Optional[str]:
    """Return the most-common value, breaking ties by first occurrence.
    Returns None for empty input."""
    if not values:
        return None
    counts: dict = {}
    first_seen: dict = {}
    for i, v in enumerate(values):
        counts[v] = counts.get(v, 0) + 1
        first_seen.setdefault(v, i)
    return max(counts, key=lambda v: (counts[v], -first_seen[v]))


def _first_sentence(text: str) -> str:
    """Best-effort first sentence: up to first '. ', '\\n', or '. ' at EOL.
    Falls back to the first 200 chars if no terminator found."""
    if not text:
        return ""
    text = text.strip()
    for sep in (". ", ".\n", "\n\n", "\n"):
        idx = text.find(sep)
        if idx > 0:
            return text[:idx].strip().rstrip(".")
    if text.endswith("."):
        return text[:-1]
    return text[:200].strip()


async def _synthesise_cluster_summary(
    contents: List[str], *, use_llm: bool
) -> str:
    """Generate a summary string from a cluster's member memory contents.

    Default extractive mode: first sentence of each member, bulleted.
    Predictable, zero LLM cost, fine for tests + casual deployments.

    LLM mode (MNEMOS_MORPHEUS_USE_LLM=true): one GRAEAE consultation
    per cluster, take the first ok response or the consensus. Falls
    back to extractive on any error so a dream still produces output.
    """
    if not contents:
        return ""
    if not use_llm:
        bullets = [f"• {_first_sentence(c)}" for c in contents]
        return (
            "MORPHEUS synthesis (extractive — first sentence of each "
            f"member of this {len(contents)}-memory cluster):\n\n"
            + "\n".join(bullets)
        )
    try:
        from mnemos.domain.graeae.engine import get_graeae_engine
        engine = get_graeae_engine()
        prompt = (
            "You are MORPHEUS, the dream-state of a memory system. "
            "Synthesise the following memory fragments into a single "
            "concise summary memory (3-5 sentences). Preserve identifiers, "
            "names, dates, and code references verbatim. Output ONLY the "
            "summary text — no preamble, no headers, no quoting of the "
            "input.\n\nFragments:\n\n"
            + "\n\n---\n\n".join(contents)
        )
        result = await engine.consult(prompt=prompt, task_type="summarisation")
        for resp in (result.get("all_responses") or {}).values():
            if resp.get("status") == "ok" and resp.get("response_text"):
                return str(resp["response_text"]).strip()
        # No usable response — fall through to extractive.
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "[MORPHEUS] LLM synthesis failed, falling back to extractive: %s",
            exc,
        )
    return await _synthesise_cluster_summary(contents, use_llm=False)


async def run_dream(
    pool: asyncpg.Pool,
    *,
    triggered_by: str = "cron",
    window_hours: int = 168,
    cluster_min_size: int = 3,
    config: Optional[dict] = None,
    namespace: Optional[str] = None,
) -> str:
    """End-to-end MORPHEUS run.

    Returns the run_id whether the run succeeded, failed, or
    short-circuited (zero memories in window). Caller can poll
    /v1/morpheus/runs/{id} for the final state. Exceptions inside
    phases are caught and recorded on the run row; they do not
    propagate to the trigger (cron / API caller / scheduler).

    `namespace`, when set, scopes the run to that tenant's memories.
    """
    run_id = await begin_run(
        pool,
        triggered_by=triggered_by,
        window_hours=window_hours,
        cluster_min_size=cluster_min_size,
        config=config,
        namespace=namespace,
    )
    try:
        await set_phase(pool, run_id, "replay")
        await phase_replay(pool, run_id)
        await set_phase(pool, run_id, "cluster")
        await phase_cluster(pool, run_id)
        if _consolidate_enabled(config):
            await set_phase(pool, run_id, "consolidate")
            await phase_consolidate(pool, run_id)
        await set_phase(pool, run_id, "synthesise")
        await phase_synthesise(pool, run_id)
        if _extract_enabled(config):
            await set_phase(pool, run_id, "extract")
            await phase_extract(pool, run_id)
        await set_phase(pool, run_id, "commit")
        await finish_run(pool, run_id)
    except Exception as exc:
        logger.exception("[MORPHEUS] run %s failed in phase", run_id)
        await fail_run(pool, run_id, f"{type(exc).__name__}: {exc}")
    return run_id

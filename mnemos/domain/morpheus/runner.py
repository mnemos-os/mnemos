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

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from uuid import UUID

import asyncpg
import numpy as np

from mnemos.core.config import get_settings
from mnemos.core.ids import new_memory_id

logger = logging.getLogger(__name__)

_PRE_CONSOLIDATE_PERMISSION_KEY = "pre_consolidate_permission_mode"
_CONSOLIDATED_PERMISSION_MODE = 400

# Optional Rust hot-path accelerator. Loaded lazily so the absence of
# the wheel on a given build host does NOT break the import - the
# Python implementation below stays the source of truth.
# Opt-in via env var MNEMOS_HOT_RS_ENABLED=1; default off until soak.
_HOT_RS = None
_HOT_RS_ENABLED = os.environ.get("MNEMOS_HOT_RS_ENABLED", "").strip().lower() in ("1", "true", "yes")
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
            scores = _HOT_RS.cosine_batch(
                _vector_to_float_list(query),
                [_vector_to_float_list(candidate) for candidate in candidates],
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
            restore_result = await conn.execute(
                """
                UPDATE memories
                SET consolidated_into = NULL,
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
        "[MORPHEUS] run %s rolled back: %d memories deleted, %d consolidated rows restored",
        run_id, n_deleted, n_restored,
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
            """
            SELECT COUNT(*)
            FROM memories m
            JOIN morpheus_runs r ON r.id = $1::uuid
            WHERE m.created BETWEEN r.window_started_at AND r.window_ended_at
              AND m.provenance IS DISTINCT FROM 'morpheus_local'
              AND m.morpheus_run_id IS NULL
              AND m.deleted_at IS NULL
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
            """
            SELECT id, embedding::text AS embedding
            FROM memories
            WHERE created BETWEEN $1 AND $2
              AND provenance IS DISTINCT FROM 'morpheus_local'
              AND morpheus_run_id IS NULL
              AND embedding IS NOT NULL
              AND deleted_at IS NULL
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
                """
                SELECT id, recall_count, created, permission_mode,
                       consolidated_into, morpheus_run_id, metadata
                FROM memories
                WHERE id = ANY($1::text[])
                  AND deleted_at IS NULL
                  AND ($2::text IS NULL OR namespace = $2)
                """,
                member_ids, namespace,
            )
        if len(rows) < min_size:
            continue

        candidates = [row for row in rows if row["consolidated_into"] is None]
        if not candidates:
            continue

        canonical = sorted(
            candidates,
            key=lambda row: (
                -int(row["recall_count"] or 0),
                row["created"],
                str(row["id"]),
            ),
        )[0]
        canonical_id = str(canonical["id"])
        cluster_count = 0

        for row in rows:
            member_id = str(row["id"])
            if member_id == canonical_id:
                continue
            consolidated_into = row["consolidated_into"]
            if consolidated_into is not None:
                if (
                    str(consolidated_into) == canonical_id
                    and str(row["morpheus_run_id"]) == run_id
                    and _metadata_has_key(row["metadata"], _PRE_CONSOLIDATE_PERMISSION_KEY)
                ):
                    cluster_count += 1
                continue

            async with pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE memories
                    SET consolidated_into=$2,
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
                """
                SELECT id, content, category, owner_id, namespace
                FROM memories
                WHERE id = ANY($1::text[])
                  AND deleted_at IS NULL
                  AND consolidated_into IS NULL
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
        await set_phase(pool, run_id, "commit")
        await finish_run(pool, run_id)
    except Exception as exc:
        logger.exception("[MORPHEUS] run %s failed in phase", run_id)
        await fail_run(pool, run_id, f"{type(exc).__name__}: {exc}")
    return run_id

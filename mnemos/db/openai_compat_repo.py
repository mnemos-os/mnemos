import logging
from typing import Any, Dict, List, Optional

import mnemos.core.lifecycle as _lc
from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP

logger = logging.getLogger(__name__)


class ModelRegistryUnavailable(Exception):
    pass


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        if hasattr(row, "get"):
            return row.get(key, default)
        return default


async def fetch_memory_context(query: str, user: Any, limit: int = 5) -> List[Dict[str, Any]]:
    if not _lc._pool:
        logger.debug("[MNEMOS] No DB pool available")
        return []

    try:
        async with _lc._pool.acquire() as conn:
            if user.role == "root":
                memories = await conn.fetch(
                    """
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE
                        m.deleted_at IS NULL
                        AND (
                            to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)
                            OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                        )
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT $2
                    """,
                    query,
                    limit,
                )
            else:
                from mnemos.core.visibility import read_visibility_predicate

                vis_clause, vis_params = read_visibility_predicate(
                    user.user_id,
                    list(user.group_ids),
                    start_param_idx=1,
                    table_alias="m",
                )
                ns_ph = f"${len(vis_params) + 1}"
                q_ph = f"${len(vis_params) + 2}"
                lim_ph = f"${len(vis_params) + 3}"
                memories = await conn.fetch(
                    f"""
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE m.deleted_at IS NULL
                      AND {vis_clause}
                      AND m.namespace = {ns_ph}
                      AND (
                          to_tsvector('english', m.content) @@ plainto_tsquery('english', {q_ph})
                          OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                      )
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT {lim_ph}
                    """,
                    *vis_params,
                    user.namespace,
                    query,
                    limit,
                )
            logger.info("[MNEMOS] Found %s memories for query '%s...'", len(memories), query[:30])
            return [{"id": m["id"], "content": m["content"]} for m in memories]
    except Exception as e:
        logger.warning("[MNEMOS] Search failed for '%s...': %s", query[:50], e)
        return []


async def fetch_model_recommendation(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
) -> Optional[Dict[str, Any]]:
    pool = _lc._pool
    if not pool:
        logger.warning("[OPTIMIZER] No DB pool available")
        return None

    try:
        async with pool.acquire() as conn:
            capability_map = {
                "code_generation": ["coding"],
                "reasoning": ["reasoning", "logic"],
                "architecture_design": ["reasoning"],
                "summarization": ["reasoning"],
                "web_search": ["online", "search"],
            }
            required_caps = capability_map.get(task_type, ["reasoning"])

            # Budgeted selection EXCLUDES rows with NULL costs.
            # COALESCEing them to 0 would silently bypass the budget
            # and rank partially-synced rows ahead of priced models.
            # Same invariant in mnemos/db/mcp_repo.py and
            # mnemos/api/routes/providers.py.
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name,
                    input_cost_per_mtok, output_cost_per_mtok,
                    capabilities,
                    COALESCE(graeae_weight, 0) AS graeae_weight,
                    context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND input_cost_per_mtok IS NOT NULL
                AND output_cost_per_mtok IS NOT NULL
                AND COALESCE(graeae_weight, 0) >= $1
                AND (input_cost_per_mtok + output_cost_per_mtok) / 2.0 <= $2
                AND capabilities @> $3
                ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                LIMIT 1
                """,
                quality_floor,
                cost_budget,
                required_caps,
            )

            if not models:
                logger.info(
                    "[OPTIMIZER] No priced model met %s budget "
                    "(budget=$%s/MTok, quality>=%s), trying degraded fallback",
                    task_type,
                    cost_budget,
                    quality_floor,
                )
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name,
                        input_cost_per_mtok, output_cost_per_mtok,
                        capabilities,
                        COALESCE(graeae_weight, 0) AS graeae_weight,
                        context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC NULLS LAST
                    LIMIT 1
                    """
                )

            if not models:
                logger.warning("[OPTIMIZER] No models available, using default gpt-4o")
                return None

            model = models[0]
            # cost_per_mtok is None when EITHER cost column is NULL —
            # only reachable via the degraded fallback. Surface the
            # unknown cost honestly rather than fabricate 0.0 which
            # would silently lie about pricing semantics.
            from mnemos.core.numeric import safe_float
            in_cost = _row_get(model, "input_cost_per_mtok")
            out_cost = _row_get(model, "output_cost_per_mtok")
            if in_cost is None or out_cost is None:
                avg_cost: float | None = None
            else:
                avg_cost = (safe_float(in_cost) + safe_float(out_cost)) / 2.0

            cost_label = f"${avg_cost:.2f}/MTok" if avg_cost is not None else "unknown"
            logger.info(
                "[OPTIMIZER] Recommended %s/%s for %s (cost=%s)",
                _row_get(model, "provider"),
                _row_get(model, "model_id"),
                task_type,
                cost_label,
            )

            return {
                "provider": _row_get(model, "provider"),
                "model_id": _row_get(model, "model_id"),
                "display_name": _row_get(model, "display_name"),
                "cost_per_mtok": avg_cost,
                "quality_score": safe_float(_row_get(model, "graeae_weight")),
                "context_window": _row_get(model, "context_window"),
            }

    except Exception as e:
        logger.warning("[OPTIMIZER] Recommendation failed: %s, using default", e)
        return None


async def lookup_provider_for_model(model: str) -> Optional[str]:
    if _lc._pool is None:
        return None

    try:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT provider FROM model_registry "
                "WHERE model_id = $1 "
                "  AND available = true AND deprecated = false",
                model,
            )
            if row is not None:
                return row["provider"]

            if "/" in model:
                head, tail = model.split("/", 1)
                head_registry = GRAEAE_REGISTRY_MAP.get(head, {"registry_provider": head})[
                    "registry_provider"
                ]
                row = await conn.fetchrow(
                    "SELECT provider FROM model_registry "
                    "WHERE provider = $1 AND model_id = $2 "
                    "  AND available = true AND deprecated = false",
                    head_registry,
                    tail,
                )
                if row is not None:
                    return row["provider"]
    except Exception as exc:
        logger.warning("[MNEMOS] model_registry lookup failed for model=%s: %s", model, exc)
    return None


async def fetch_available_models() -> list[Any]:
    rows: list[Any] = []
    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT provider, model_id, display_name
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY graeae_weight DESC NULLS LAST, model_id ASC
                    """
                )
        except Exception as exc:
            logger.warning(
                "[/v1/models] model_registry query failed, "
                "returning an empty discovery list: %s",
                exc,
            )
            rows = []
    return rows


async def fetch_model_provider(model_id: str) -> Optional[str]:
    if _lc._pool is None:
        return None

    try:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT provider
                FROM model_registry
                WHERE model_id = $1
                  AND available = true
                  AND deprecated = false
                LIMIT 1
                """,
                model_id,
            )
            if row is not None:
                return row["provider"]
    except Exception as exc:
        logger.warning("[/v1/models/%s] registry lookup failed: %s", model_id, exc)
        raise ModelRegistryUnavailable from exc

    return None

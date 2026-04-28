"""Provider routing & model registry endpoints — v3.0.0.

/v1/providers — GRAEAE provider management and model recommendation.

"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["providers"])


@router.get("/providers")
async def list_providers(
    user: UserContext = Depends(get_current_user),
):
    """List available LLM providers with model counts."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    try:
        from mnemos.domain.graeae.engine import get_graeae_engine
        engine = get_graeae_engine()
        providers = engine.providers
        status = engine.provider_status()

        return {
            "providers": list(providers.keys()),
            "total_models": len(providers),
            "status": status,
        }
    except Exception as e:
        logger.error(f"[PROVIDERS] Error listing providers: {e}")
        raise HTTPException(status_code=503, detail="Failed to load providers")


@router.get("/providers/health")
async def provider_health(
    user: UserContext = Depends(get_current_user),
):
    """Check health status of all LLM providers."""
    try:
        from mnemos.domain.graeae.engine import get_graeae_engine
        engine = get_graeae_engine()
        return engine.provider_status()
    except Exception as e:
        logger.error(f"[PROVIDERS] Health check error: {e}")
        raise HTTPException(status_code=503, detail="Health check failed")


@router.get("/providers/recommend")
async def recommend_model(
    task_type: str = Query(..., description="Task type: code_generation, reasoning, architecture_design, etc."),
    cost_budget: float = Query(10.0, description="Max cost per 1M tokens ($/MTok)"),
    quality_floor: float = Query(0.80, description="Minimum quality score (0-1)"),
    user: UserContext = Depends(get_current_user),
):
    """Recommend cheapest model meeting quality + capability requirements.

    Returns model with lowest cost that:
    - Has required capabilities for task_type
    - Has quality (weight) >= quality_floor
    - Costs <= cost_budget per 1M tokens

    If no model meets criteria, returns cheapest available.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    try:
        # Query model registry for candidates
        async with _lc._pool.acquire() as conn:
            # Map task types to required capabilities
            capability_map = {
                "code_generation": ["coding"],
                "reasoning": ["reasoning", "logic"],
                "architecture_design": ["reasoning"],
                "summarization": ["reasoning"],
                "web_search": ["online", "search"],
            }
            required_caps = capability_map.get(task_type, ["reasoning"])

            # Find models meeting criteria
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name, input_cost_per_mtok,
                    output_cost_per_mtok, capabilities, graeae_weight, context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND graeae_weight >= $1
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
                # Fallback: cheapest model available (ignore budget)
                logger.info(
                    f"[PROVIDERS] No model found for {task_type} "
                    f"(budget=${cost_budget}/MTok, quality>={quality_floor}), "
                    f"using fallback cheapest model"
                )
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name, input_cost_per_mtok,
                        output_cost_per_mtok, capabilities, graeae_weight, context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                    LIMIT 1
                    """
                )

            if not models:
                # Final fallback: no rows in model_registry at all (fresh install),
                # recommend from the static mnemos.domain.graeae.providers config in config.toml.
                try:
                    from mnemos.domain.graeae.engine import get_graeae_engine
                    engine = get_graeae_engine()
                    providers = engine.providers
                    # Pick the configured provider with the highest weight at/above the floor.
                    candidates = [
                        (name, cfg) for name, cfg in providers.items()
                        if cfg.get("weight", 0.0) >= quality_floor
                    ]
                    if not candidates:
                        # Relax the floor — pick overall highest weight.
                        candidates = sorted(providers.items(),
                                            key=lambda kv: kv[1].get("weight", 0.0),
                                            reverse=True)
                    if not candidates:
                        raise HTTPException(status_code=404, detail="No providers configured")
                    name, cfg = max(candidates, key=lambda kv: kv[1].get("weight", 0.0))
                    return {
                        "recommended": {
                            "provider": name,
                            "model_id": cfg.get("model"),
                            "display_name": cfg.get("model"),
                            "cost_per_mtok": None,
                        },
                        "reasoning": (
                            f"model_registry empty; recommended highest-weight "
                            f"configured provider ({name}, weight={cfg.get('weight', 0.0)})"
                        ),
                        "quality_score": cfg.get("weight"),
                        "context_window": None,
                    }
                except HTTPException:
                    raise
                except Exception as fallback_err:
                    logger.warning(
                        f"[PROVIDERS] Fallback to graeae config failed: {fallback_err}"
                    )
                raise HTTPException(status_code=404, detail="No models available")

            model = models[0]
            avg_cost = (model["input_cost_per_mtok"] + model["output_cost_per_mtok"]) / 2.0

            logger.info(
                f"[PROVIDERS] Recommended {model['provider']}/{model['model_id']} "
                f"for {task_type} (cost=${avg_cost:.2f}/MTok)"
            )

            return {
                "recommended": {
                    "provider": model["provider"],
                    "model_id": model["model_id"],
                    "display_name": model.get("display_name"),
                    "cost_per_mtok": avg_cost,
                },
                "reasoning": f"Cheapest model with {', '.join(required_caps)} capability "
                f"above quality floor {quality_floor}",
                "quality_score": model["graeae_weight"],
                "context_window": model.get("context_window"),
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PROVIDERS] Recommendation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Recommendation failed: {str(e)}")

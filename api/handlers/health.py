"""Health check and statistics endpoints."""
import json
import logging
from _version import __version__ as _MNEMOS_VERSION
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, HTTPException

import api.lifecycle as _lc
from api.models import HealthResponse, StatsResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return health status including DB pool and background workers."""
    db_ok = False
    if _lc._pool:
        try:
            async with _lc._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_ok = True
        except Exception as e:
            logger.warning(f"[HEALTH] DB probe failed: {e}")

    # Get worker status
    worker_status = _lc._worker_status.get("distillation_worker", "unknown")

    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        database_connected=db_ok,
        version=_MNEMOS_VERSION,
        distillation_worker=worker_status,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get system statistics from database (cached 60 s)."""
    # Cache key versioned to invalidate after the federation-aware
    # native/federated split shipped in v3.4.x. Bumping the suffix
    # forces clients to recompute against the new schema.
    cache_key = "stats:global:v2"

    if _lc._cache:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug("[CACHE] /stats hit")
                return StatsResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] /stats read error: {e}")

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    try:
        async with _lc._pool.acquire() as conn:
            total = await conn.fetchval('SELECT COUNT(*) FROM memories')
            # Federation-aware split: native rows have federation_source
            # NULL; pulled rows carry the peer name there. memories_by_peer
            # is the per-peer count for operator visibility into what a
            # given staging/prod node is hosting from each upstream.
            native = await conn.fetchval(
                'SELECT COUNT(*) FROM memories WHERE federation_source IS NULL'
            )
            federated = await conn.fetchval(
                'SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL'
            )
            peer_rows = await conn.fetch(
                'SELECT federation_source, COUNT(*) AS cnt FROM memories '
                'WHERE federation_source IS NOT NULL '
                'GROUP BY federation_source ORDER BY cnt DESC'
            )
            memories_by_peer = {r['federation_source']: r['cnt'] for r in peer_rows}
            cat_rows = await conn.fetch('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category')
            memories_by_category = {row['category']: row['cnt'] for row in cat_rows}
            sub_rows = await conn.fetch(
                'SELECT category, subcategory, COUNT(*) as cnt FROM memories '
                'WHERE subcategory IS NOT NULL GROUP BY category, subcategory ORDER BY cnt DESC'
            )
            memories_by_subcategory: dict = {}
            for r in sub_rows:
                memories_by_subcategory.setdefault(r['category'], {})[r['subcategory']] = r['cnt']
            avg_quality = await conn.fetchval(
                'SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL'
            )
            total_compressions = (
                await conn.fetchval("SELECT COUNT(*) FROM memory_compressed_variants") or 0
            )
            avg_ratio_row = await conn.fetchval("""
                SELECT AVG(v.compression_ratio)
                FROM memory_compressed_variants v
            """)
            unreviewed_compressions = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_compressed_variants "
                    "WHERE quality_score IS NULL"
                ) or 0
            )

        result = StatsResponse(
            total_memories=total or 0,
            native_memories=native or 0,
            federated_memories=federated or 0,
            memories_by_peer=memories_by_peer,
            total_compressions=total_compressions,
            average_compression_ratio=round(avg_ratio_row, 2) if avg_ratio_row else 0.57,
            average_quality_rating=int(avg_quality) if avg_quality else 75,
            memories_by_category=memories_by_category,
            memories_by_subcategory=memories_by_subcategory,
            unreviewed_compressions=unreviewed_compressions,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        )

        if _lc._cache:
            try:
                await _lc._cache.setex(cache_key, 60, result.model_dump_json())
            except Exception as e:
                logger.warning(f"[CACHE] /stats write error: {e}")

        return result

    except asyncpg.PostgresError as e:
        logger.error(f"Stats DB error: {e}")
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=503, detail=f"Internal error: {e}")

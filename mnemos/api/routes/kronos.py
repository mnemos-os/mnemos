"""KRONOS admin routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.core.config import get_settings
from mnemos.domain.kronos import (
    detect_namespace_drift,
    detect_recall_anomalies,
    forecast_recall_load,
)

router = APIRouter(prefix="/admin/kronos", tags=["admin", "kronos"])


def _require_enabled() -> None:
    if not get_settings().kronos.enabled:
        raise HTTPException(status_code=503, detail="KRONOS disabled in this profile")


@router.get("/anomalies")
async def recall_anomalies(
    namespace: str = Query(..., min_length=1),
    _: UserContext = Depends(require_root),
) -> dict:
    _require_enabled()
    settings = get_settings().kronos
    pool = require_postgres_pool_or_503(route_label="GET /admin/kronos/anomalies")
    anomalies = await detect_recall_anomalies(
        pool,
        namespace,
        lookback_hours=settings.default_lookback_hours,
        sensitivity=settings.default_sensitivity,
    )
    return {
        "namespace": namespace,
        "count": len(anomalies),
        "anomalies": [asdict(item) for item in anomalies],
    }


@router.get("/drift")
async def namespace_drift(
    namespace: str = Query(..., min_length=1),
    _: UserContext = Depends(require_root),
) -> dict:
    _require_enabled()
    settings = get_settings().kronos
    pool = require_postgres_pool_or_503(route_label="GET /admin/kronos/drift")
    drift = await detect_namespace_drift(
        pool,
        namespace,
        baseline_days=settings.default_baseline_days,
    )
    return asdict(drift)


@router.get("/forecast")
async def recall_forecast(
    namespace: str = Query(..., min_length=1),
    hours_ahead: int = Query(24, ge=1, le=24 * 30),
    _: UserContext = Depends(require_root),
) -> dict:
    _require_enabled()
    pool = require_postgres_pool_or_503(route_label="GET /admin/kronos/forecast")
    result = await forecast_recall_load(pool, namespace, hours_ahead=hours_ahead)
    return asdict(result)

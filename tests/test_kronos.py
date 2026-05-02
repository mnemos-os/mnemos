from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mnemos.api.dependencies import UserContext, get_current_user


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(
        self,
        *,
        daily_rows=None,
        hourly_rows=None,
        event_rows=None,
        memory_rows=None,
        eligibility_rows=None,
    ):
        self.conn = _Conn(
            daily_rows=daily_rows or [],
            hourly_rows=hourly_rows or [],
            event_rows=event_rows or [],
            memory_rows=memory_rows or [],
            eligibility_rows=eligibility_rows or [],
        )

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, *, daily_rows, hourly_rows, event_rows, memory_rows, eligibility_rows):
        self.daily_rows = daily_rows
        self.hourly_rows = hourly_rows
        self.event_rows = event_rows
        self.memory_rows = memory_rows
        self.eligibility_rows = eligibility_rows

    async def fetch(self, query: str, *args):
        if "date_trunc('day'" in query:
            return self.daily_rows
        if "date_trunc('hour'" in query:
            return self.hourly_rows
        if "FROM memory_recall_log" in query and "recalled_at" in query:
            return self.event_rows
        if "archived_at IS NULL" in query:
            return self.eligibility_rows
        if "SELECT id, recall_count, last_recalled_at" in query:
            return self.memory_rows
        return []


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _stable_daily_rows(memory_id: str, baseline_count: int, current_count: int) -> list[dict]:
    today = _day_start()
    rows = [
        {
            "memory_id": memory_id,
            "bucket_day": today - timedelta(days=offset),
            "recall_count": baseline_count,
        }
        for offset in range(7, 0, -1)
    ]
    rows.append({"memory_id": memory_id, "bucket_day": today, "recall_count": current_count})
    return rows


def test_z_score_and_ewma_helpers():
    from mnemos.domain.kronos.scoring import ewma, z_score

    assert z_score(12.0, 10.0, 2.0) == 1.0
    assert z_score(10.0, 10.0, 0.0) == 0.0
    np.testing.assert_allclose(
        ewma(np.asarray([10.0, 20.0, 30.0]), alpha=0.5),
        np.asarray([10.0, 15.0, 22.5]),
    )


def test_hourly_buckets_correctness_with_fixed_timestamps():
    from mnemos.domain.kronos.scoring import hourly_buckets

    base = datetime(2026, 1, 1, 12, 15, tzinfo=timezone.utc)
    buckets = hourly_buckets(
        [
            base,
            base + timedelta(hours=1),
            base + timedelta(hours=1, minutes=40),
            base + timedelta(hours=3),
        ],
        window_hours=4,
    )

    np.testing.assert_array_equal(buckets, np.asarray([1.0, 2.0, 0.0, 1.0]))


@pytest.mark.asyncio
async def test_detect_recall_anomalies_spike_case():
    from mnemos.domain.kronos import detect_recall_anomalies

    pool = _Pool(daily_rows=_stable_daily_rows("mem_spike", 100, 500))
    anomalies = await detect_recall_anomalies(pool, "default", sensitivity=2.5)

    assert len(anomalies) == 1
    assert anomalies[0].memory_id == "mem_spike"
    assert anomalies[0].anomaly_type == "spike"
    assert anomalies[0].z_score > 2.5
    assert anomalies[0].suggested_action == "trending"


@pytest.mark.asyncio
async def test_detect_recall_anomalies_drop_case():
    from mnemos.domain.kronos import detect_recall_anomalies

    pool = _Pool(daily_rows=_stable_daily_rows("mem_drop", 100, 5))
    anomalies = await detect_recall_anomalies(pool, "default", sensitivity=2.5)

    assert len(anomalies) == 1
    assert anomalies[0].memory_id == "mem_drop"
    assert anomalies[0].anomaly_type == "drop"
    assert anomalies[0].z_score < -2.5
    assert anomalies[0].suggested_action == "eligible_for_persephone"


@pytest.mark.asyncio
async def test_forecast_recall_load_returns_sane_ci_bounds():
    from mnemos.domain.kronos import forecast_recall_load

    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = [
        {
            "bucket_hour": now_hour - timedelta(hours=idx),
            "recall_count": 10 + (idx % 4),
        }
        for idx in range(24 * 7)
    ]
    pool = _Pool(hourly_rows=rows)
    result = await forecast_recall_load(pool, "default", hours_ahead=24)

    assert result.forecast_window_hours == 24
    assert result.ci_lower < result.predicted_total_recalls < result.ci_upper
    assert result.predicted_p95_per_hour > 0


@pytest.mark.asyncio
async def test_forecast_persephone_eligibility_counts_future_window():
    from mnemos.domain.kronos import forecast_persephone_eligibility

    now = datetime.now(timezone.utc)
    pool = _Pool(
        eligibility_rows=[
            {"id": "soon", "last_recalled_at": now - timedelta(days=170)},
            {"id": "too_late", "last_recalled_at": now - timedelta(days=149)},
            {"id": "already", "last_recalled_at": now - timedelta(days=181)},
            {"id": "never", "last_recalled_at": None},
        ]
    )

    assert await forecast_persephone_eligibility(pool, "default", archive_after_days=180, days_ahead=30) == 1


def test_kronos_routes_disabled_return_503(monkeypatch):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests

    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "false")
    _reset_settings_for_tests()
    app.dependency_overrides[get_current_user] = lambda: _root()
    try:
        with TestClient(app) as client:
            response = client.get("/admin/kronos/anomalies", params={"namespace": "default"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        _reset_settings_for_tests()

    assert response.status_code == 503
    assert response.json()["detail"] == "KRONOS disabled in this profile"


def test_kronos_routes_enabled_return_expected_json_shape(monkeypatch):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests
    import mnemos.core.lifecycle as lc

    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "true")
    _reset_settings_for_tests()
    app.dependency_overrides[get_current_user] = lambda: _root()
    try:
        with TestClient(app) as client:
            monkeypatch.setattr(
                lc,
                "_pool",
                _Pool(daily_rows=_stable_daily_rows("mem_spike", 100, 500)),
            )
            response = client.get("/admin/kronos/anomalies", params={"namespace": "default"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        _reset_settings_for_tests()

    assert response.status_code == 200
    data = response.json()
    assert data["namespace"] == "default"
    assert data["count"] == 1
    assert data["anomalies"][0]["anomaly_type"] == "spike"
    assert data["anomalies"][0]["observed_count"] == 500


@pytest.mark.asyncio
async def test_mcp_kronos_anomalies_cross_namespace_non_root_is_empty(monkeypatch):
    from mnemos.core.config import _reset_settings_for_tests
    from mnemos.mcp.tools import execute_tool

    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "true")
    _reset_settings_for_tests()
    try:
        result = await execute_tool(
            "kronos_anomalies",
            {"namespace": "other-ns"},
            user=_alice(),
        )
    finally:
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        _reset_settings_for_tests()

    assert result == {"success": True, "namespace": "other-ns", "count": 0, "anomalies": []}

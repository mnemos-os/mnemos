"""Regression coverage for MORPHEUS orphan-timeout sweeps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import pytest
from typer.testing import CliRunner

from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import sweep_orphan_runs


OLD_RUN_ID = "00000000-0000-0000-0000-000000000509"
FRESH_RUN_ID = "00000000-0000-0000-0000-000000000510"


class _Conn:
    def __init__(self, *, now: datetime):
        self.now = now
        self.runs: dict[str, dict] = {}

    def insert_morpheus_run(
        self,
        run_id: str,
        *,
        started_at: datetime,
        status: str = "running",
    ) -> None:
        self.runs[run_id] = {
            "id": run_id,
            "started_at": started_at,
            "status": status,
            "error": None,
            "finished_at": None,
        }

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        assert "UPDATE morpheus_runs" in compact
        assert "status = 'running'" in compact
        max_age_hours, error = args
        cutoff = self.now - timedelta(hours=float(max_age_hours))
        swept = []
        for row in self.runs.values():
            if row["status"] != "running" or row["started_at"] >= cutoff:
                continue
            row["status"] = "failed"
            row["error"] = error
            row["finished_at"] = self.now
            swept.append({"id": row["id"], "started_at": row["started_at"]})
        return swept


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn
        self.closed = False

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()

    async def close(self) -> None:
        self.closed = True


def _old_and_fresh_pool() -> tuple[_Pool, _Conn]:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    conn = _Conn(now=now)
    conn.insert_morpheus_run(
        OLD_RUN_ID,
        started_at=now - timedelta(hours=3),
        status="running",
    )
    conn.insert_morpheus_run(
        FRESH_RUN_ID,
        started_at=now - timedelta(hours=1),
        status="running",
    )
    pool = _Pool(conn)
    return pool, conn


@pytest.mark.asyncio
async def test_sweep_orphan_runs_marks_old_running_rows_failed(caplog):
    pool, conn = _old_and_fresh_pool()
    caplog.set_level(logging.INFO, logger=runner.__name__)

    swept = await sweep_orphan_runs(pool, max_age_hours=2)

    assert swept == 1
    old = conn.runs[OLD_RUN_ID]
    assert old["status"] == "failed"
    assert old["error"] == "orphan_timeout_sweep"
    assert old["finished_at"] == conn.now
    assert conn.runs[FRESH_RUN_ID]["status"] == "running"
    assert OLD_RUN_ID in caplog.text
    assert str(old["started_at"]) in caplog.text


def test_morpheus_sweep_orphans_cli_prints_swept_count(monkeypatch):
    from mnemos.cli import main as cli_main

    pool, conn = _old_and_fresh_pool()

    async def _open_pool():
        return pool, False

    monkeypatch.setattr(cli_main, "_open_cli_morpheus_pool", _open_pool)
    result = CliRunner().invoke(
        cli_main.app,
        ["morpheus", "sweep-orphans", "--max-age-hours", "2"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Swept 1 MORPHEUS orphan run(s)."
    assert conn.runs[OLD_RUN_ID]["status"] == "failed"
    assert conn.runs[OLD_RUN_ID]["error"] == "orphan_timeout_sweep"

"""Regression tests for webhook retry row terminalization."""
from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn: "_FakeWebhookConn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn: "_FakeWebhookConn"):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class _FakeWebhookConn:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.subscription = {
            "id": rows[0]["subscription_id"] if rows else str(uuid.uuid4()),
            "url": "https://hooks.example.test/mnemos",
            "secret": "secret",
            "revoked": False,
        }

    def transaction(self):
        return _FakeTransaction()

    async def fetch(self, sql: str, *args):
        if "SELECT d.id FROM webhook_deliveries d" not in sql:
            raise AssertionError(f"unexpected fetch SQL: {sql}")
        max_attempts = args[0]
        now = datetime.now(timezone.utc)
        recoverable = []
        for row in sorted(self.rows, key=lambda item: item["scheduled_at"]):
            if row["scheduled_at"] > now or row["attempt_num"] > max_attempts:
                continue
            if row["status"] == "pending":
                recoverable.append({"id": row["id"]})
            elif row["status"] == "retrying" and not self._has_successor(row):
                recoverable.append({"id": row["id"]})
        return recoverable[:50]

    async def fetchrow(self, sql: str, *args):
        if "FROM webhook_deliveries d" not in sql or "JOIN webhook_subscriptions" not in sql:
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")
        row = self._row(args[0])
        if row is None:
            return None
        return {
            **row,
            "url": self.subscription["url"],
            "secret": self.subscription["secret"],
            "revoked": self.subscription["revoked"],
        }

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT EXISTS"):
            probe = {
                "subscription_id": args[0],
                "event_type": args[1],
                "payload_hash": args[2],
                "attempt_num": args[3],
            }
            return self._has_successor(probe)
        if compact.startswith("UPDATE webhook_deliveries SET status='retrying'"):
            row = self._row(args[0])
            if row is None or row["status"] != "pending":
                return None
            row["status"] = "retrying"
            return row["id"]
        if compact.startswith("SELECT status FROM webhook_deliveries"):
            row = self._row(args[0])
            return None if row is None else row["status"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO webhook_deliveries"):
            self.rows.append({
                "id": str(uuid.uuid4()),
                "subscription_id": args[0],
                "event_type": args[1],
                "payload": args[2],
                "payload_hash": args[3],
                "attempt_num": args[4],
                "status": "pending",
                "scheduled_at": args[5],
                "response_status": None,
                "response_body": None,
                "error": None,
                "delivered_at": None,
            })
            return "INSERT 0 1"
        if "SET status='succeeded'" in compact:
            row = self._row(args[0])
            row["status"] = "succeeded"
            row["response_status"] = args[1]
            row["response_body"] = args[2]
            row["delivered_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        if "SET status='abandoned'" in compact:
            row = self._row(args[0])
            row["status"] = "abandoned"
            row["error"] = args[-1]
            row["delivered_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        if "SET status=$2" in compact:
            row = self._row(args[0])
            row["status"] = args[1]
            if len(args) > 2:
                row["response_status"] = args[2]
                row["response_body"] = args[3]
                row["error"] = args[4]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    def _row(self, row_id: str):
        for row in self.rows:
            if row["id"] == str(row_id):
                return row
        return None

    def _has_successor(self, row: dict[str, Any]) -> bool:
        return any(
            other["subscription_id"] == row["subscription_id"]
            and other["event_type"] == row["event_type"]
            and other["payload_hash"] == row["payload_hash"]
            and other["attempt_num"] > row["attempt_num"]
            for other in self.rows
        )


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = f"status={status_code}"


class _HTTPClientFactory:
    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.delivery_ids: list[str] = []

    def __call__(self, *args, **kwargs):
        return _FakeHTTPClient(self)


class _FakeHTTPClient:
    def __init__(self, factory: _HTTPClientFactory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, content, headers):
        self.factory.delivery_ids.append(headers["X-MNEMOS-Delivery-ID"])
        return _FakeResponse(self.factory.statuses.pop(0))


def _attempt(attempt_num: int = 1) -> dict[str, Any]:
    payload = '{"data":{"memory_id":"mem_test"},"event":"memory.created"}'
    return {
        "id": str(uuid.uuid4()),
        "subscription_id": str(uuid.uuid4()),
        "event_type": "memory.created",
        "payload": payload,
        "payload_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "attempt_num": attempt_num,
        "status": "pending",
        "scheduled_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "response_status": None,
        "response_body": None,
        "error": None,
        "delivered_at": None,
    }


async def _install(monkeypatch, rows: list[dict[str, Any]], statuses: list[int]):
    from api import lifecycle, webhook_dispatcher
    from api.handlers import webhooks

    conn = _FakeWebhookConn(rows)
    monkeypatch.setattr(lifecycle, "_pool", _FakePool(conn))

    async def _accept_url(url: str) -> None:
        return None

    http_factory = _HTTPClientFactory(statuses)
    monkeypatch.setattr(webhooks, "validate_webhook_url", _accept_url)
    monkeypatch.setattr(webhook_dispatcher.httpx, "AsyncClient", http_factory)
    return webhook_dispatcher, conn, http_factory


def _make_due(row: dict[str, Any]) -> None:
    row["scheduled_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_failed_attempt_terminalizes_and_only_successor_is_recoverable(monkeypatch):
    async def run():
        first = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [first], [500, 204])

        await dispatcher._attempt_delivery(first["id"])
        second = conn.rows[1]
        _make_due(second)

        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert [row["id"] for row in recoverable] == [second["id"]]

        await dispatcher._attempt_delivery(first["id"])
        await dispatcher._attempt_delivery(second["id"])

        assert first["status"] == dispatcher.SUPERSEDED_RETRY_STATUS
        assert second["status"] == "succeeded"
        assert http.delivery_ids == [first["id"], second["id"]]

    asyncio.run(run())


def test_retry_chain_terminalizes_all_prior_attempts(monkeypatch):
    async def run():
        first = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [first], [500, 502])

        await dispatcher._attempt_delivery(first["id"])
        second = conn.rows[1]
        _make_due(second)
        await dispatcher._attempt_delivery(second["id"])
        third = conn.rows[2]
        _make_due(third)

        await dispatcher._attempt_delivery(first["id"])
        await dispatcher._attempt_delivery(second["id"])

        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert [row["id"] for row in recoverable] == [third["id"]]
        assert [row["status"] for row in conn.rows] == [
            dispatcher.SUPERSEDED_RETRY_STATUS,
            dispatcher.SUPERSEDED_RETRY_STATUS,
            "pending",
        ]
        assert http.delivery_ids == [first["id"], second["id"]]

    asyncio.run(run())


def test_successful_successor_does_not_replay_prior_failed_attempt(monkeypatch):
    async def run():
        first = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [first], [503, 200])

        await dispatcher._attempt_delivery(first["id"])
        second = conn.rows[1]
        _make_due(second)
        await dispatcher._attempt_delivery(second["id"])
        await dispatcher._attempt_delivery(first["id"])

        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert recoverable == []
        assert first["status"] == dispatcher.SUPERSEDED_RETRY_STATUS
        assert second["status"] == "succeeded"
        assert http.delivery_ids == [first["id"], second["id"]]

    asyncio.run(run())


def test_final_attempt_failure_is_abandoned_without_successor(monkeypatch):
    async def run():
        from api.webhook_dispatcher import MAX_ATTEMPTS

        final = _attempt(attempt_num=MAX_ATTEMPTS)
        dispatcher, conn, http = await _install(monkeypatch, [final], [500])

        await dispatcher._attempt_delivery(final["id"])

        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert recoverable == []
        assert len(conn.rows) == 1
        assert final["status"] == "abandoned"
        assert http.delivery_ids == [final["id"]]

    asyncio.run(run())


def test_retry_terminal_state_migration_repairs_existing_superseded_rows():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_retry_terminal_state.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "retry_scheduled" in sql
    assert "SET status = 'retry_scheduled'" in compact
    assert "newer.subscription_id = d.subscription_id" in compact
    assert "newer.event_type = d.event_type" in compact
    assert "newer.payload_hash = d.payload_hash" in compact
    assert "newer.attempt_num > d.attempt_num" in compact
    assert "WHERE status IN ('pending', 'retrying')" in compact

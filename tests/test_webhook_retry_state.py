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


class _FakeWebhookStore:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.subscription = {
            "id": rows[0]["subscription_id"] if rows else str(uuid.uuid4()),
            "url": "https://hooks.example.test/mnemos",
            "secret": "secret",
            "revoked": False,
        }
        self.locked_by: dict[str, int] = {}
        self.lock_acquisitions = 0
        self.pool: "_FakePool | None" = None

    def row(self, row_id: str):
        for row in self.rows:
            if row["id"] == str(row_id):
                return row
        return None

    def has_successor(self, row: dict[str, Any]) -> bool:
        return any(
            other["subscription_id"] == row["subscription_id"]
            and other["event_type"] == row["event_type"]
            and other["payload_hash"] == row["payload_hash"]
            and other["attempt_num"] > row["attempt_num"]
            for other in self.rows
        )

    def release_locks(self, conn_id: int) -> None:
        self.locked_by = {
            row_id: owner
            for row_id, owner in self.locked_by.items()
            if owner != conn_id
        }


class _FakeTransaction:
    def __init__(self, conn: "_FakeWebhookConn"):
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_transaction += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.store.release_locks(self.conn.conn_id)
        self.conn.in_transaction -= 1
        return False


class _FakeAcquire:
    def __init__(self, conn: "_FakeWebhookConn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, store: _FakeWebhookStore):
        self.store = store
        self.next_conn_id = 1

    def acquire(self):
        conn = _FakeWebhookConn(self.store, self.next_conn_id)
        self.next_conn_id += 1
        return _FakeAcquire(conn)


class _FakeWebhookConn:
    def __init__(self, store: _FakeWebhookStore, conn_id: int = 0):
        self.store = store
        self.conn_id = conn_id
        self.in_transaction = 0

    @property
    def rows(self):
        return self.store.rows

    @property
    def subscription(self):
        return self.store.subscription

    @property
    def lock_acquisitions(self):
        return self.store.lock_acquisitions

    @property
    def pool(self):
        return self.store.pool

    def transaction(self):
        return _FakeTransaction(self)

    async def fetch(self, sql: str, *args):
        if "SELECT d.id FROM webhook_deliveries d" not in sql:
            raise AssertionError(f"unexpected fetch SQL: {sql}")
        max_attempts = args[0]
        limit = args[1] if len(args) > 1 else 50
        now = datetime.now(timezone.utc)
        recoverable = []
        for row in sorted(self.rows, key=lambda item: item["scheduled_at"]):
            if row["scheduled_at"] > now or row["attempt_num"] > max_attempts:
                continue
            if row["status"] == "pending":
                if self._try_lock(row["id"]):
                    recoverable.append({"id": row["id"]})
            elif row["status"] == "retrying" and not self._has_successor(row):
                if self._try_lock(row["id"]):
                    recoverable.append({"id": row["id"]})
        return recoverable[:limit]

    async def fetchrow(self, sql: str, *args):
        if "FROM webhook_deliveries d" not in sql or "JOIN webhook_subscriptions" not in sql:
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")
        row = self._row(args[0])
        if row is None:
            return None
        max_attempts = args[1] if len(args) > 1 else 4
        if (
            row["scheduled_at"] > datetime.now(timezone.utc)
            or row["attempt_num"] > max_attempts
            or row["status"] not in {"pending", "retrying"}
        ):
            return None
        if not self._try_lock(row["id"]):
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
        if compact.startswith("UPDATE webhook_deliveries d SET status = 'retry_scheduled'"):
            updated = 0
            for row in self.rows:
                if row["status"] == "retrying" and self._has_successor(row):
                    row["status"] = "retry_scheduled"
                    updated += 1
            return f"UPDATE {updated}"
        if compact.startswith("UPDATE webhook_deliveries SET status='retrying'"):
            row = self._row(args[0])
            if row is None or row["status"] != "pending":
                return "UPDATE 0"
            row["status"] = "retrying"
            return "UPDATE 1"
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
            if len(args) > 1:
                row["response_status"] = args[1]
                row["response_body"] = args[2]
                row["error"] = args[3]
            else:
                row["error"] = "subscription revoked"
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
        return self.store.row(row_id)

    def _has_successor(self, row: dict[str, Any]) -> bool:
        return self.store.has_successor(row)

    def _try_lock(self, row_id: str) -> bool:
        if self.in_transaction == 0:
            return True
        owner = self.store.locked_by.get(str(row_id))
        if owner is not None and owner != self.conn_id:
            return False
        if owner is None:
            self.store.locked_by[str(row_id)] = self.conn_id
            self.store.lock_acquisitions += 1
        return True


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = f"status={status_code}"


class _HTTPClientFactory:
    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.delivery_ids: list[str] = []
        self.delay = 0.0

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
        if self.factory.delay:
            await asyncio.sleep(self.factory.delay)
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

    store = _FakeWebhookStore(rows)
    pool = _FakePool(store)
    store.pool = pool
    conn = _FakeWebhookConn(store)
    monkeypatch.setattr(lifecycle, "_pool", pool)

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


def test_recovery_dequeue_uses_skip_locked_claim():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "api" / "webhook_dispatcher.py").read_text()
    compact = " ".join(source.split())

    assert "FOR UPDATE SKIP LOCKED" in compact
    assert "await _attempt_delivery_locked(conn, str(rows[0][\"id\"]))" in compact


def test_concurrent_recovery_claims_retrying_row_once(monkeypatch):
    async def run():
        retrying = _attempt()
        retrying["status"] = "retrying"
        dispatcher, conn, http = await _install(monkeypatch, [retrying], [204])
        http.delay = 0.05

        recovered = await asyncio.gather(
            dispatcher._recover_due_deliveries(conn.pool, limit=1),
            dispatcher._recover_due_deliveries(conn.pool, limit=1),
        )

        assert sum(recovered) == 1
        assert conn.lock_acquisitions == 1
        assert retrying["status"] == "succeeded"
        assert http.delivery_ids == [retrying["id"]]

    asyncio.run(run())


def test_startup_repair_sweep_closes_upgrade_race(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        dispatcher, conn, _http = await _install(monkeypatch, [parent], [])

        migration_result = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)
        assert migration_result == "UPDATE 0"
        assert parent["status"] == "retrying"

        successor = _attempt(attempt_num=parent["attempt_num"] + 1)
        successor.update({
            "subscription_id": parent["subscription_id"],
            "event_type": parent["event_type"],
            "payload": parent["payload"],
            "payload_hash": parent["payload_hash"],
        })
        conn.rows.append(successor)

        result = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)

        assert result == "UPDATE 1"
        assert parent["status"] == dispatcher.SUPERSEDED_RETRY_STATUS

    asyncio.run(run())


def test_lifecycle_runs_webhook_retry_repair_on_startup():
    repo_root = Path(__file__).resolve().parents[1]
    lifecycle_source = (repo_root / "api" / "lifecycle.py").read_text()
    dispatcher_source = (repo_root / "api" / "webhook_dispatcher.py").read_text()
    compact_dispatcher = " ".join(dispatcher_source.split())

    assert "repair_superseded_retrying_deliveries" in lifecycle_source
    assert "await _webhook_retry_repair(_pool)" in lifecycle_source
    assert "UPDATE webhook_deliveries d SET status = 'retry_scheduled'" in compact_dispatcher
    assert "newer.attempt_num > d.attempt_num" in compact_dispatcher


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
    assert "same idempotent sweep on every startup" in sql

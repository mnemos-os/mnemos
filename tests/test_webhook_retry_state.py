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
        self.advisory_locks: dict[int, asyncio.Lock] = {}
        self.advisory_acquisitions = 0
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

    def advisory_lock(self, key: int) -> asyncio.Lock:
        lock = self.advisory_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.advisory_locks[key] = lock
        return lock


class _FakeTransaction:
    def __init__(self, conn: "_FakeWebhookConn"):
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_transaction += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.store.release_locks(self.conn.conn_id)
        self.conn.release_advisory_locks()
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
        self.advisory_keys: list[int] = []

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

    def release_advisory_locks(self) -> None:
        while self.advisory_keys:
            key = self.advisory_keys.pop()
            lock = self.store.advisory_lock(key)
            if lock.locked():
                lock.release()

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
            if not self._lease_available(row):
                continue
            if row["status"] == "pending":
                if self._try_lock(row["id"]):
                    recoverable.append({"id": row["id"]})
            elif row["status"] == "retrying" and not self._has_successor(row):
                if self._try_lock(row["id"]):
                    recoverable.append({"id": row["id"]})
        return recoverable[:limit]

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT d.id, d.subscription_id"):
            row = self._row(args[0])
            if row is None:
                return None
            max_attempts = args[1] if len(args) > 1 else 4
            if not self._due_live_and_leaseable(row, max_attempts):
                return None
            return self._with_subscription(row)

        if compact.startswith("UPDATE webhook_deliveries d SET lease_token"):
            row = self._row(args[0])
            max_attempts = args[3]
            if row is None or not self._due_live_and_leaseable(row, max_attempts):
                return None
            if row["status"] == "retrying" and self._has_successor(row):
                return None
            row["lease_token"] = str(args[1])
            row["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=int(args[2]))
            if row["status"] == "pending":
                row["status"] = "retrying"
            return self._with_subscription(row)

        if "SET status='succeeded'" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_live_lease(row, args[1]):
                return None
            row["status"] = "succeeded"
            row["response_status"] = args[2]
            row["response_body"] = args[3]
            row["error"] = None
            row["delivered_at"] = datetime.now(timezone.utc)
            self._clear_lease(row)
            return {"id": row["id"]}

        if "SET status='abandoned'" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_live_lease(row, args[1]):
                return None
            row["status"] = "abandoned"
            if "subscription revoked" in compact:
                row["error"] = "subscription revoked"
            else:
                row["response_status"] = args[2]
                row["response_body"] = args[3]
                row["error"] = args[4]
            row["delivered_at"] = datetime.now(timezone.utc)
            self._clear_lease(row)
            return {"id": row["id"]}

        if "SET status=$3" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_live_lease(row, args[1]):
                return None
            row["status"] = args[2]
            row["response_status"] = args[3]
            row["response_body"] = args[4]
            row["error"] = args[5]
            self._clear_lease(row)
            return {"id": row["id"]}

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

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
        if compact.startswith("SELECT pg_advisory_xact_lock"):
            key = int(args[0])
            lock = self.store.advisory_lock(key)
            await lock.acquire()
            self.advisory_keys.append(key)
            self.store.advisory_acquisitions += 1
            return "SELECT 1"
        if compact.startswith("UPDATE webhook_deliveries d SET status = 'retry_scheduled'"):
            updated = 0
            for row in self.rows:
                if row["status"] == "retrying" and self._has_successor(row):
                    row["status"] = "retry_scheduled"
                    self._clear_lease(row)
                    updated += 1
            return f"UPDATE {updated}"
        if compact.startswith("UPDATE webhook_deliveries SET status=$2"):
            row = self._row(args[0])
            if row is None or row["status"] != "retrying":
                return "UPDATE 0"
            row["status"] = args[1]
            self._clear_lease(row)
            return "UPDATE 1"
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
                "lease_token": None,
                "lease_expires_at": None,
            })
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    def _row(self, row_id: str):
        return self.store.row(row_id)

    def _has_successor(self, row: dict[str, Any]) -> bool:
        return self.store.has_successor(row)

    def _lease_available(self, row: dict[str, Any]) -> bool:
        expires_at = row.get("lease_expires_at")
        return row.get("lease_token") is None or (
            expires_at is not None and expires_at < datetime.now(timezone.utc)
        )

    def _owns_live_lease(self, row: dict[str, Any], lease_token: str) -> bool:
        expires_at = row.get("lease_expires_at")
        return (
            row.get("lease_token") == str(lease_token)
            and expires_at is not None
            and expires_at >= datetime.now(timezone.utc)
        )

    def _clear_lease(self, row: dict[str, Any]) -> None:
        row["lease_token"] = None
        row["lease_expires_at"] = None

    def _due_live_and_leaseable(self, row: dict[str, Any], max_attempts: int) -> bool:
        return (
            row["scheduled_at"] <= datetime.now(timezone.utc)
            and row["attempt_num"] <= max_attempts
            and row["status"] in {"pending", "retrying"}
            and self._lease_available(row)
        )

    def _with_subscription(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "url": self.subscription["url"],
            "secret": self.subscription["secret"],
            "revoked": self.subscription["revoked"],
        }

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
    def __init__(
        self,
        status_code: int,
        *,
        chunks: list[bytes],
        chunk_delay: float,
        never_complete: bool,
        factory: "_HTTPClientFactory",
    ):
        self.status_code = status_code
        self.chunks = chunks
        self.chunk_delay = chunk_delay
        self.never_complete = never_complete
        self.factory = factory

    async def aiter_bytes(self):
        try:
            for chunk in self.chunks:
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                self.factory.chunks_yielded += 1
                yield chunk
            while self.never_complete:
                await asyncio.sleep(self.chunk_delay or 3600)
                self.factory.chunks_yielded += 1
                yield b"x"
        except asyncio.CancelledError:
            self.factory.cancelled = True
            raise


class _HTTPClientFactory:
    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.delivery_ids: list[str] = []
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.response_chunks: list[list[bytes]] = []
        self.chunk_delay = 0.0
        self.never_complete = False
        self.started = asyncio.Event()
        self.exited = 0
        self.chunks_yielded = 0
        self.cancelled = False

    def __call__(self, *args, **kwargs):
        return _FakeHTTPClient(self)


class _FakeHTTPStream:
    def __init__(self, factory: _HTTPClientFactory, headers):
        self.factory = factory
        self.headers = headers

    async def __aenter__(self):
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        if self.factory.delay:
            await asyncio.sleep(self.factory.delay)
        status_code = self.factory.statuses.pop(0)
        chunks = (
            self.factory.response_chunks.pop(0)
            if self.factory.response_chunks
            else [f"status={status_code}".encode("utf-8")]
        )
        self.factory.delivery_ids.append(self.headers["X-MNEMOS-Delivery-ID"])
        self.factory.started.set()
        return _FakeResponse(
            status_code,
            chunks=chunks,
            chunk_delay=self.factory.chunk_delay,
            never_complete=self.factory.never_complete,
            factory=self.factory,
        )

    async def __aexit__(self, exc_type, exc, tb):
        self.factory.active -= 1
        self.factory.exited += 1
        return False


class _FakeHTTPClient:
    def __init__(self, factory: _HTTPClientFactory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, *, content, headers):
        return _FakeHTTPStream(self.factory, headers)

    async def post(self, url, *, content, headers):
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        try:
            if self.factory.delay:
                await asyncio.sleep(self.factory.delay)
            self.factory.delivery_ids.append(headers["X-MNEMOS-Delivery-ID"])
            status_code = self.factory.statuses.pop(0)
            return _FakeResponse(
                status_code,
                chunks=[f"status={status_code}".encode("utf-8")],
                chunk_delay=0.0,
                never_complete=False,
                factory=self.factory,
            )
        finally:
            self.factory.active -= 1


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
        "lease_token": None,
        "lease_expires_at": None,
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
    monkeypatch.setattr(webhook_dispatcher, "_send_semaphore", None)
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
    assert "SET lease_token=$2::uuid" in compact
    assert "await _send_claimed_delivery(delivery)" in compact
    assert "FOR UPDATE OF d SKIP LOCKED" not in compact
    assert "_attempt_delivery_locked" not in compact


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
        assert conn.store.advisory_acquisitions >= 1
        assert retrying["status"] == "succeeded"
        assert http.delivery_ids == [retrying["id"]]

    asyncio.run(run())


def test_expired_delivery_lease_can_be_reclaimed(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])

        first = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token="00000000-0000-0000-0000-000000000001",
            lease_seconds=1,
        )
        assert first is not None
        assert row["status"] == "retrying"
        assert row["lease_token"] == "00000000-0000-0000-0000-000000000001"

        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        second = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token="00000000-0000-0000-0000-000000000002",
            lease_seconds=1,
        )

        assert second is not None
        assert row["lease_token"] == "00000000-0000-0000-0000-000000000002"

    asyncio.run(run())


def test_finalize_with_stale_lease_token_is_noop(monkeypatch):
    async def run():
        row = _attempt()
        row["status"] = "retrying"
        row["lease_token"] = "00000000-0000-0000-0000-000000000001"
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])
        delivery = conn._with_subscription(row)

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            delivery,
            "00000000-0000-0000-0000-000000000001",
            dispatcher._DeliveryResult(succeeded=True, response_status=204, response_body="ok"),
        )

        assert not finalized
        assert row["status"] == "retrying"
        assert row["response_status"] is None
        assert row["lease_token"] == "00000000-0000-0000-0000-000000000001"

    asyncio.run(run())


def test_webhook_send_concurrency_cap(monkeypatch):
    async def run():
        rows = [_attempt(), _attempt(), _attempt()]
        dispatcher, conn, http = await _install(monkeypatch, rows, [204, 204, 204])
        http.delay = 0.05
        monkeypatch.setattr(dispatcher, "_send_semaphore", asyncio.Semaphore(2))

        sent = await asyncio.gather(*(dispatcher._attempt_delivery(row["id"]) for row in rows))

        assert sent == [True, True, True]
        assert http.max_active == 2
        assert sorted(http.delivery_ids) == sorted(row["id"] for row in rows)
        assert [row["status"] for row in rows] == ["succeeded", "succeeded", "succeeded"]
        assert conn.store.advisory_acquisitions >= 6

    asyncio.run(run())


def test_webhook_send_deadline_aborts_before_lease_replay_and_releases_semaphore(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        lease_seconds = 2
        finalize_buffer = 1.9
        send_deadline = dispatcher._derive_total_send_deadline_seconds(
            lease_seconds,
            finalize_buffer,
        )
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(dispatcher, "WEBHOOK_LEASE_SECONDS", lease_seconds)
        monkeypatch.setattr(dispatcher, "WEBHOOK_FINALIZE_BUFFER_SECONDS", finalize_buffer)
        monkeypatch.setattr(dispatcher, "TOTAL_SEND_DEADLINE_SECONDS", send_deadline)
        monkeypatch.setattr(dispatcher, "_send_semaphore", semaphore)
        http.response_chunks = [[b"trickle"]]
        http.chunk_delay = 0.02
        http.never_complete = True

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        task = asyncio.create_task(dispatcher._attempt_delivery(row["id"]))
        await asyncio.wait_for(http.started.wait(), timeout=0.5)

        assert row["status"] == "retrying"
        assert row["lease_expires_at"] > datetime.now(timezone.utc)
        recovered_while_leased = await dispatcher._recover_due_deliveries(conn.pool, limit=1)
        assert recovered_while_leased == 0
        assert http.delivery_ids == [row["id"]]

        finalized = await task
        elapsed = loop.time() - started_at

        assert finalized
        assert elapsed < send_deadline + 0.35
        assert http.cancelled
        assert http.exited == 1
        assert not semaphore.locked()
        assert row["status"] == dispatcher.SUPERSEDED_RETRY_STATUS
        assert row["error"].startswith("send-timeout:")
        assert len(conn.rows) == 2
        assert conn.rows[1]["status"] == "pending"

    asyncio.run(run())


def test_webhook_response_body_is_streamed_and_capped(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        monkeypatch.setattr(dispatcher, "WEBHOOK_RESPONSE_BODY_MAX_BYTES", 10)
        http.response_chunks = [[b"abcde", b"fghij", b"klmnop"]]

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert row["status"] == "succeeded"
        assert row["response_body"] == "abcdefghij"
        assert len(row["response_body"].encode("utf-8")) == 10
        assert http.chunks_yielded == 2

    asyncio.run(run())


def test_webhook_send_deadline_is_derived_from_lease_with_finalize_buffer():
    from api import webhook_dispatcher as dispatcher

    assert dispatcher._derive_total_send_deadline_seconds(2, 0.5) == 1.5
    assert (
        dispatcher.TOTAL_SEND_DEADLINE_SECONDS
        == dispatcher.WEBHOOK_LEASE_SECONDS - dispatcher.WEBHOOK_FINALIZE_BUFFER_SECONDS
    )
    try:
        dispatcher._derive_total_send_deadline_seconds(2, 2)
    except ValueError as exc:
        assert "WEBHOOK_LEASE_SECONDS" in str(exc)
    else:
        raise AssertionError("invalid webhook lease/deadline contract did not fail fast")


def test_rolling_upgrade_interleaving_waits_for_chain_advisory_lock_before_no_successor_check(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        blocker = _FakeWebhookConn(conn.store, conn_id=999)

        async with blocker.transaction():
            # This models a successor insert that is already in progress when
            # recovery tries to claim the parent. Without the chain advisory
            # lock, recovery would pass the no-successor check and POST parent.
            await dispatcher._lock_delivery_chain(blocker, parent)
            task = asyncio.create_task(dispatcher._attempt_delivery(parent["id"]))
            await asyncio.sleep(0.01)
            assert http.delivery_ids == []

            successor = _attempt(attempt_num=parent["attempt_num"] + 1)
            successor.update({
                "subscription_id": parent["subscription_id"],
                "event_type": parent["event_type"],
                "payload": parent["payload"],
                "payload_hash": parent["payload_hash"],
            })
            conn.rows.append(successor)

        sent = await task

        assert not sent
        assert http.delivery_ids == []
        assert parent["status"] == dispatcher.SUPERSEDED_RETRY_STATUS
        assert conn.store.advisory_acquisitions >= 2

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

    assert "recovery_worker_loop" in lifecycle_source
    assert "_schedule_background(_webhook_recovery(_pool))" in lifecycle_source
    assert "REPAIR_BURST_SECONDS" in dispatcher_source
    assert "REPAIR_PERIODIC_INTERVAL" in dispatcher_source
    assert "_repair_superseded_retrying_deliveries_safely" in dispatcher_source
    assert "UPDATE webhook_deliveries d SET status = 'retry_scheduled'" in compact_dispatcher
    assert "newer.attempt_num > d.attempt_num" in compact_dispatcher


def test_startup_repair_burst_then_periodic(monkeypatch):
    async def run():
        from api import webhook_dispatcher as dispatcher

        phases: list[str] = []
        intervals: list[float] = []
        now = 0.0

        class _FakeLoop:
            def time(self):
                return now

        async def _repair(pool, *, phase):
            phases.append(phase)

        async def _recover(pool):
            return 0

        async def _sleep(delay):
            nonlocal now
            intervals.append(delay)
            now += delay
            if len(intervals) >= 3:
                raise asyncio.CancelledError

        monkeypatch.setattr(dispatcher, "REPAIR_BURST_SECONDS", 10)
        monkeypatch.setattr(dispatcher, "REPAIR_BURST_INTERVAL", 5)
        monkeypatch.setattr(dispatcher, "REPAIR_PERIODIC_INTERVAL", 300)
        monkeypatch.setattr(dispatcher, "_repair_superseded_retrying_deliveries_safely", _repair)
        monkeypatch.setattr(dispatcher, "_recover_due_deliveries", _recover)
        monkeypatch.setattr(dispatcher.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(dispatcher.asyncio, "sleep", _sleep)

        try:
            await dispatcher.recovery_worker_loop(object())
        except asyncio.CancelledError:
            pass

        assert phases == ["burst", "burst", "periodic"]
        assert intervals == [5, 5, 30.0]

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
    assert "same idempotent sweep on every startup" in sql


def test_webhook_attempt_lease_migration_adds_claim_columns():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_attempt_lease.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "ADD COLUMN IF NOT EXISTS lease_token UUID NULL" in compact
    assert "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL" in compact
    assert "idx_webhook_deliveries_lease_expires_at" in sql
    assert "ON webhook_deliveries(lease_expires_at)" in compact

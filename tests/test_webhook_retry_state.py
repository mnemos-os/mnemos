"""Regression tests for webhook retry row terminalization."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))


def _webhook_module_source(*module_names: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return "\n".join(
        (repo_root / "mnemos" / "webhooks" / f"{module_name}.py").read_text()
        for module_name in module_names
    )


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
        self.claim_update_stall = None
        self.enforce_succeeded_unique = False
        self.unique_violation_cls: type[Exception] = RuntimeError
        self.enforce_succeeded_terminal = False
        self.check_violation_cls: type[Exception] = RuntimeError
        self.succeeded_update_attempts = 0
        self.savepoint_commands: list[str] = []
        self.bulk_successor_fetches = 0
        self.single_successor_fetches = 0
        self.successor_cleanup_updates = 0
        self.after_bulk_successor_fetch = None

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

    def has_live_successor(self, row: dict[str, Any]) -> bool:
        return any(
            other["subscription_id"] == row["subscription_id"]
            and other["event_type"] == row["event_type"]
            and other["payload_hash"] == row["payload_hash"]
            and other["attempt_num"] > row["attempt_num"]
            and other["status"] in {"pending", "retrying"}
            and not other.get("superseded", False)
            for other in self.rows
        )

    def has_succeeded_chain_peer(self, row: dict[str, Any]) -> bool:
        return any(
            other["subscription_id"] == row["subscription_id"]
            and other["event_type"] == row["event_type"]
            and other["payload_hash"] == row["payload_hash"]
            and other.get("id") != row.get("id")
            and other["status"] == "succeeded"
            for other in self.rows
        )

    def has_live_attempt_conflict(
        self,
        subscription_id: str,
        event_type: str,
        payload_hash: str,
        attempt_num: int,
    ) -> bool:
        return any(
            other["subscription_id"] == subscription_id
            and other["event_type"] == event_type
            and other["payload_hash"] == payload_hash
            and other["attempt_num"] == attempt_num
            and other["status"] in {"pending", "retrying"}
            and not other.get("superseded", False)
            for other in self.rows
        )

    def live_unleased_successor(self, row: dict[str, Any]):
        return min(self.live_unleased_successors(row), key=lambda item: item["attempt_num"], default=None)

    def live_unleased_successors(self, row: dict[str, Any]):
        return [
            other
            for other in self.rows
            if other["subscription_id"] == row["subscription_id"]
            and other["event_type"] == row["event_type"]
            and other["payload_hash"] == row["payload_hash"]
            and other["attempt_num"] > row["attempt_num"]
            and other["status"] in {"pending", "retrying"}
            and not other.get("superseded", False)
            and (
                other.get("lease_token") is None
                or (
                    other.get("lease_expires_at") is not None
                    and other["lease_expires_at"] < datetime.now(timezone.utc)
                )
            )
        ]

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
        self.conn._transaction_logs.append({})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        log = self.conn._transaction_logs.pop()
        if exc_type is not None:
            self.conn._restore_log(log)
        self.conn.in_transaction -= 1
        if self.conn.in_transaction == 0:
            self.conn._savepoint_logs.clear()
            self.conn.store.release_locks(self.conn.conn_id)
            self.conn.release_advisory_locks()
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
        self._transaction_logs: list[dict[str, dict[str, Any] | None]] = []
        self._savepoint_logs: list[tuple[str, dict[str, dict[str, Any] | None]]] = []

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

    def _active_change_logs(self):
        return [
            *self._transaction_logs,
            *(log for _name, log in self._savepoint_logs),
        ]

    def _record_row_change(self, row: dict[str, Any]) -> None:
        row_id = str(row["id"])
        for log in self._active_change_logs():
            if row_id not in log:
                log[row_id] = dict(row)

    def _record_row_insert(self, row_id: str) -> None:
        for log in self._active_change_logs():
            if row_id not in log:
                log[row_id] = None

    def _restore_log(self, log: dict[str, dict[str, Any] | None]) -> None:
        for row_id, snapshot in reversed(list(log.items())):
            row = self._row(row_id)
            if snapshot is None:
                if row is not None:
                    self.rows.remove(row)
                continue
            if row is None:
                self.rows.append(dict(snapshot))
            else:
                row.clear()
                row.update(snapshot)

    def _create_savepoint(self, name: str) -> None:
        self._savepoint_logs.append((name, {}))

    def _rollback_to_savepoint(self, name: str) -> None:
        for index in range(len(self._savepoint_logs) - 1, -1, -1):
            savepoint_name, log = self._savepoint_logs[index]
            if savepoint_name == name:
                self._restore_log(log)
                self._savepoint_logs[index] = (savepoint_name, {})
                del self._savepoint_logs[index + 1 :]
                return
        raise AssertionError(f"unknown savepoint: {name}")

    def _release_savepoint(self, name: str) -> None:
        for index in range(len(self._savepoint_logs) - 1, -1, -1):
            savepoint_name, _log = self._savepoint_logs[index]
            if savepoint_name == name:
                del self._savepoint_logs[index]
                return
        raise AssertionError(f"unknown savepoint: {name}")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("WITH claim_clock AS") and "UPDATE webhook_deliveries d SET lease_token=$2::uuid" in compact:
            max_attempts = args[0]
            lease_token = str(args[1])
            limit = args[2]
            lease_seconds = int(args[3])
            current_revision = args[4]
            claim_db_now = datetime.now(timezone.utc)
            claimed = []
            for row in sorted(self.rows, key=lambda item: item["scheduled_at"]):
                if row["scheduled_at"] > claim_db_now or row["attempt_num"] > max_attempts:
                    continue
                if row.get("superseded", False):
                    continue
                if not self._lease_available(row):
                    continue
                if row.get("writer_revision") != current_revision:
                    continue
                if self._has_succeeded_chain_peer(row):
                    continue
                if row["status"] == "pending":
                    if not self._try_lock(row["id"]):
                        continue
                elif row["status"] == "retrying" and not self._has_successor(row):
                    if not self._try_lock(row["id"]):
                        continue
                else:
                    continue
                self._record_row_change(row)
                row["lease_token"] = lease_token
                row["lease_expires_at"] = claim_db_now + timedelta(seconds=lease_seconds)
                if row["status"] == "pending":
                    self._set_status(row, "retrying", changed_at=claim_db_now)
                delivery = self._with_subscription(row)
                delivery["claim_db_now"] = claim_db_now
                delivery["lease_token"] = lease_token
                claimed.append(delivery)
                if len(claimed) >= limit:
                    break
            return claimed

        if compact.startswith("SELECT newer.id FROM webhook_deliveries newer"):
            self.store.bulk_successor_fetches += 1
            probe = {
                "subscription_id": args[0],
                "event_type": args[1],
                "payload_hash": args[2],
                "attempt_num": args[3],
            }
            successors = sorted(
                self.store.live_unleased_successors(probe),
                key=lambda item: item["attempt_num"],
            )
            hook = self.store.after_bulk_successor_fetch
            if hook is not None:
                self.store.after_bulk_successor_fetch = None
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            return [{"id": successor["id"]} for successor in successors]

        if "SELECT d.id FROM webhook_deliveries d" not in sql:
            raise AssertionError(f"unexpected fetch SQL: {sql}")
        max_attempts = args[0]
        limit = args[1] if len(args) > 1 else 50
        current_revision = args[2] if len(args) > 2 else 1
        now = datetime.now(timezone.utc)
        recoverable = []
        for row in sorted(self.rows, key=lambda item: item["scheduled_at"]):
            if row["scheduled_at"] > now or row["attempt_num"] > max_attempts:
                continue
            if row.get("superseded", False):
                continue
            if not self._lease_available(row):
                continue
            if self._has_succeeded_chain_peer(row):
                continue
            if row["status"] == "pending":
                if row.get("writer_revision") == current_revision and self._try_lock(row["id"]):
                    recoverable.append({"id": row["id"]})
            elif (
                row["status"] == "retrying"
                and not self._has_successor(row)
                and row.get("writer_revision") == current_revision
            ):
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
            current_revision = args[4] if len(args) > 4 else 1
            if row is None or not self._due_live_and_leaseable(row, max_attempts):
                return None
            if row["status"] == "retrying" and self._has_successor(row):
                return None
            if self.store.claim_update_stall is not None:
                await self.store.claim_update_stall()
            claim_db_now = datetime.now(timezone.utc)
            if (
                row["status"] in {"pending", "retrying"}
                and row.get("writer_revision") != current_revision
            ):
                return None
            self._record_row_change(row)
            row["lease_token"] = str(args[1])
            row["lease_expires_at"] = claim_db_now + timedelta(seconds=int(args[2]))
            if row["status"] == "pending":
                self._set_status(row, "retrying", changed_at=claim_db_now)
            delivery = self._with_subscription(row)
            delivery["claim_db_now"] = claim_db_now
            return delivery

        if compact.startswith("SELECT newer.id FROM webhook_deliveries newer"):
            self.store.single_successor_fetches += 1
            probe = {
                "subscription_id": args[0],
                "event_type": args[1],
                "payload_hash": args[2],
                "attempt_num": args[3],
            }
            successor = self.store.live_unleased_successor(probe)
            return None if successor is None else {"id": successor["id"]}

        if compact.startswith("UPDATE webhook_deliveries SET lease_token=NULL"):
            row = self._row(args[0])
            if row is None or row.get("lease_token") != str(args[1]):
                return None
            self._clear_lease(row)
            return {"id": row["id"], "status": row["status"], "superseded": row["superseded"]}

        if "SET status='succeeded'" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_lease_token(row, args[1]):
                return None
            if (
                "lease_expires_at >= clock_timestamp()" in compact
                and not self._lease_unexpired(row)
            ):
                return None
            self.store.succeeded_update_attempts += 1
            if (
                "AND status IN ('pending', 'retrying')" in compact
                and (
                    row["status"] not in {"pending", "retrying"}
                    or row.get("superseded", False)
                )
            ):
                return None
            if self.store.enforce_succeeded_unique and self.store.has_succeeded_chain_peer(row):
                raise self.store.unique_violation_cls()
            self._set_status(row, "succeeded")
            row["response_status"] = args[2]
            row["response_body"] = args[3]
            row["error"] = None
            row["delivered_at"] = datetime.now(timezone.utc)
            self._clear_lease(row)
            return {"id": row["id"]}

        if (
            compact.startswith(
                "UPDATE webhook_deliveries SET status='abandoned', "
                "superseded=TRUE, status_updated_at=clock_timestamp()"
            )
            and "lease_token=$2::uuid" in compact
        ):
            row = self._row(args[0])
            if (
                row is None
                or not self._owns_live_lease(row, args[1])
                or row["status"] not in {"pending", "retrying"}
                or row.get("superseded", False)
            ):
                return None
            self._set_status(row, "abandoned")
            row["superseded"] = True
            self._clear_lease(row)
            return {"id": row["id"]}

        if "SET status='abandoned'" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_lease_token(row, args[1]):
                return None
            if (
                "lease_expires_at >= clock_timestamp()" in compact
                and not self._lease_unexpired(row)
            ):
                return None
            if (
                "AND status IN ('pending', 'retrying')" in compact
                and (
                    row["status"] not in {"pending", "retrying"}
                    or row.get("superseded", False)
                )
            ):
                return None
            self._set_status(row, "abandoned")
            row["superseded"] = "superseded=TRUE" in compact
            if "subscription revoked" in compact:
                row["error"] = "subscription revoked"
            else:
                row["response_status"] = args[2]
                row["response_body"] = args[3]
                row["error"] = args[4]
            row["delivered_at"] = datetime.now(timezone.utc)
            self._clear_lease(row)
            return {"id": row["id"]}

        if compact.startswith("INSERT INTO webhook_deliveries"):
            return self._insert_delivery(*args)

        if "SET status=$3" in compact:
            row = self._row(args[0])
            if row is None or not self._owns_live_lease(row, args[1]):
                return None
            self._set_status(row, args[2])
            row["response_status"] = args[3]
            row["response_body"] = args[4]
            row["error"] = args[5]
            self._clear_lease(row)
            return {"id": row["id"]}

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT EXISTS"):
            if "FROM webhook_deliveries WHERE id=$1::uuid" in compact:
                row = self._row(args[0])
                return (
                    row is not None
                    and self._owns_live_lease(row, args[1])
                    and row["status"] in {"pending", "retrying"}
                    and not row.get("superseded", False)
                )
            probe = {
                "subscription_id": args[0],
                "event_type": args[1],
                "payload_hash": args[2],
            }
            if "status = 'succeeded'" in compact:
                if len(args) > 3:
                    probe["id"] = str(args[3])
                return self._has_succeeded_chain_peer(probe)
            probe["attempt_num"] = args[3]
            if "newer.status IN ('pending', 'retrying')" in compact:
                return self.store.has_live_successor(probe)
            return self._has_successor(probe)
        if compact.startswith("UPDATE webhook_deliveries SET status='retrying'"):
            row = self._row(args[0])
            if row is None:
                return None
            self._set_status(row, "retrying")
            return row["id"]
        if compact.startswith("SELECT status FROM webhook_deliveries"):
            row = self._row(args[0])
            return None if row is None else row["status"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SAVEPOINT "):
            name = compact.split()[1]
            self.store.savepoint_commands.append(compact)
            self._create_savepoint(name)
            return "SAVEPOINT"
        if compact.startswith("ROLLBACK TO SAVEPOINT "):
            name = compact.split()[3]
            self.store.savepoint_commands.append(compact)
            self._rollback_to_savepoint(name)
            return "ROLLBACK"
        if compact.startswith("RELEASE SAVEPOINT "):
            name = compact.split()[2]
            self.store.savepoint_commands.append(compact)
            self._release_savepoint(name)
            return "RELEASE"
        if compact.startswith("SELECT pg_advisory_xact_lock"):
            key = int(args[0])
            lock = self.store.advisory_lock(key)
            await lock.acquire()
            self.advisory_keys.append(key)
            self.store.advisory_acquisitions += 1
            return "SELECT 1"
        if compact.startswith("UPDATE webhook_deliveries d SET status = 'abandoned'"):
            updated = 0
            for row in self.rows:
                if (
                    row["status"] in {"pending", "retrying"}
                    and self._lease_available(row)
                    and (
                        self._has_successor(row)
                        or self._has_succeeded_chain_peer(row)
                    )
                ):
                    self._set_status(row, "abandoned")
                    row["superseded"] = True
                    self._clear_lease(row)
                    updated += 1
            return f"UPDATE {updated}"
        if compact.startswith(
            "UPDATE webhook_deliveries SET status='abandoned', superseded=TRUE, "
            "status_updated_at=clock_timestamp()"
        ):
            self.store.successor_cleanup_updates += 1
            row = self._row(args[0])
            if (
                row is None
                or row["status"] not in {"pending", "retrying"}
                or row.get("superseded", False)
                or not self._lease_available(row)
            ):
                return "UPDATE 0"
            self._set_status(row, "abandoned")
            row["superseded"] = True
            self._clear_lease(row)
            return "UPDATE 1"
        if compact.startswith("UPDATE webhook_deliveries SET status='abandoned', superseded=TRUE"):
            row = self._row(args[0])
            if row is None or row["status"] != "retrying" or row.get("superseded", False):
                return "UPDATE 0"
            self._set_status(row, "abandoned")
            row["superseded"] = True
            self._clear_lease(row)
            return "UPDATE 1"
        if compact.startswith("UPDATE webhook_deliveries SET status=$2"):
            row = self._row(args[0])
            if row is None or row["status"] != "retrying":
                return "UPDATE 0"
            self._set_status(row, args[1])
            self._clear_lease(row)
            return "UPDATE 1"
        if compact.startswith("UPDATE webhook_deliveries SET status='retrying'"):
            row = self._row(args[0])
            if row is None:
                return "UPDATE 0"
            self._set_status(row, "retrying")
            return "UPDATE 1"
        if compact.startswith("UPDATE webhook_deliveries SET status='pending'"):
            row = self._row(args[0])
            if row is None:
                return "UPDATE 0"
            self._set_status(row, "pending")
            return "UPDATE 1"
        if compact.startswith("UPDATE webhook_deliveries SET response_body=$2"):
            row = self._row(args[0])
            if row is None:
                return "UPDATE 0"
            self._record_row_change(row)
            row["response_body"] = args[1]
            return "UPDATE 1"
        if compact.startswith("INSERT INTO webhook_deliveries"):
            inserted = self._insert_delivery(*args)
            return "INSERT 0 1" if inserted else "INSERT 0 0"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    def _row(self, row_id: str):
        return self.store.row(row_id)

    def _has_successor(self, row: dict[str, Any]) -> bool:
        return self.store.has_successor(row)

    def _has_succeeded_chain_peer(self, row: dict[str, Any]) -> bool:
        return self.store.has_succeeded_chain_peer(row)

    def _insert_delivery(self, *args):
        if self.store.has_live_attempt_conflict(args[0], args[1], args[3], args[4]):
            return None
        row = {
            "id": str(uuid.uuid4()),
            "subscription_id": args[0],
            "event_type": args[1],
            "payload": args[2],
            "payload_hash": args[3],
            "attempt_num": args[4],
            "status": "pending",
            "superseded": False,
            "scheduled_at": args[5],
            "status_updated_at": datetime.now(timezone.utc),
            "response_status": None,
            "response_body": None,
            "error": None,
            "delivered_at": None,
            "lease_token": None,
            "lease_expires_at": None,
            "writer_revision": args[6] if len(args) > 6 else 1,
        }
        self._record_row_insert(row["id"])
        self.rows.append(row)
        return {"id": row["id"]}

    def _lease_available(self, row: dict[str, Any]) -> bool:
        expires_at = row.get("lease_expires_at")
        return row.get("lease_token") is None or (
            expires_at is not None and expires_at < datetime.now(timezone.utc)
        )

    def _owns_live_lease(self, row: dict[str, Any], lease_token: str) -> bool:
        return self._owns_lease_token(row, lease_token) and self._lease_unexpired(row)

    def _owns_lease_token(self, row: dict[str, Any], lease_token: str) -> bool:
        return row.get("lease_token") == str(lease_token)

    def _lease_unexpired(self, row: dict[str, Any]) -> bool:
        expires_at = row.get("lease_expires_at")
        return expires_at is not None and expires_at >= datetime.now(timezone.utc)

    def _clear_lease(self, row: dict[str, Any]) -> None:
        self._record_row_change(row)
        row["lease_token"] = None
        row["lease_expires_at"] = None

    def _set_status(
        self,
        row: dict[str, Any],
        status: str,
        *,
        changed_at: datetime | None = None,
    ) -> None:
        if (
            self.store.enforce_succeeded_terminal
            and row.get("status") == "succeeded"
            and status != "succeeded"
        ):
            raise self.store.check_violation_cls(
                "webhook_deliveries: cannot transition status away from "
                f"succeeded (id={row['id']}, attempted new status={status})"
            )
        self._record_row_change(row)
        if row.get("status") != status:
            row["status_updated_at"] = changed_at or datetime.now(timezone.utc)
        row["status"] = status

    def _due_live_and_leaseable(self, row: dict[str, Any], max_attempts: int) -> bool:
        return (
            row["scheduled_at"] <= datetime.now(timezone.utc)
            and row["attempt_num"] <= max_attempts
            and not row.get("superseded", False)
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
        headers: dict[str, str] | None = None,
        body_error: Exception | None = None,
    ):
        self.status_code = status_code
        self.chunks = chunks
        self.chunk_delay = chunk_delay
        self.never_complete = never_complete
        self.factory = factory
        self.headers = headers or {}
        self.body_error = body_error

    async def aiter_bytes(self):
        async for chunk in self.aiter_raw():
            yield chunk

    async def aiter_raw(self):
        try:
            for chunk in self.chunks:
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                self.factory.chunks_yielded += 1
                yield chunk
            if self.body_error is not None:
                raise self.body_error
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
        self.request_headers: list[dict[str, str]] = []
        self.response_headers: list[dict[str, str]] = []
        self.body_errors: list[Exception | None] = []
        self.stream_exit_errors: list[BaseException | None] = []
        self.client_exit_errors: list[BaseException | None] = []
        self.stream_exit_delay = 0.0
        self.client_exit_delay = 0.0
        self.stream_exit_cancelled = False
        self.client_exit_cancelled = False

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
        response_headers = self.factory.response_headers.pop(0) if self.factory.response_headers else {}
        body_error = self.factory.body_errors.pop(0) if self.factory.body_errors else None
        self.factory.delivery_ids.append(self.headers["X-MNEMOS-Delivery-ID"])
        self.factory.request_headers.append(dict(self.headers))
        self.factory.started.set()
        return _FakeResponse(
            status_code,
            chunks=chunks,
            chunk_delay=self.factory.chunk_delay,
            never_complete=self.factory.never_complete,
            factory=self.factory,
            headers=response_headers,
            body_error=body_error,
        )

    async def __aexit__(self, exc_type, exc, tb):
        self.factory.active -= 1
        self.factory.exited += 1
        try:
            if self.factory.stream_exit_delay:
                await asyncio.sleep(self.factory.stream_exit_delay)
        except asyncio.CancelledError:
            self.factory.stream_exit_cancelled = True
            raise
        exit_error = (
            self.factory.stream_exit_errors.pop(0)
            if self.factory.stream_exit_errors
            else None
        )
        if exit_error is not None:
            raise exit_error
        return False


class _FakeHTTPClient:
    def __init__(self, factory: _HTTPClientFactory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.factory.client_exit_delay:
                await asyncio.sleep(self.factory.client_exit_delay)
        except asyncio.CancelledError:
            self.factory.client_exit_cancelled = True
            raise
        exit_error = (
            self.factory.client_exit_errors.pop(0)
            if self.factory.client_exit_errors
            else None
        )
        if exit_error is not None:
            raise exit_error
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
            self.factory.request_headers.append(dict(headers))
            status_code = self.factory.statuses.pop(0)
            return _FakeResponse(
                status_code,
                chunks=[f"status={status_code}".encode("utf-8")],
                chunk_delay=0.0,
                never_complete=False,
                factory=self.factory,
                headers={},
            )
        finally:
            self.factory.active -= 1


def _attempt(attempt_num: int = 1) -> dict[str, Any]:
    payload = '{"data":{"memory_id":"mem_test"},"event":"memory.created"}'
    scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    return {
        "id": str(uuid.uuid4()),
        "subscription_id": str(uuid.uuid4()),
        "event_type": "memory.created",
        "payload": payload,
        "payload_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "attempt_num": attempt_num,
        "status": "pending",
        "superseded": False,
        "scheduled_at": scheduled_at,
        "status_updated_at": scheduled_at,
        "response_status": None,
        "response_body": None,
        "error": None,
        "delivered_at": None,
        "lease_token": None,
        "lease_expires_at": None,
        "writer_revision": 1,
    }


async def _install(monkeypatch, rows: list[dict[str, Any]], statuses: list[int]):
    from mnemos.core import lifecycle
    from mnemos.webhooks import dispatcher as webhook_dispatcher
    from mnemos.webhooks import validation as webhook_validation

    store = _FakeWebhookStore(rows)
    pool = _FakePool(store)
    store.pool = pool
    conn = _FakeWebhookConn(store)
    monkeypatch.setattr(lifecycle, "_pool", pool)

    async def _accept_url(url: str) -> webhook_validation.ValidatedWebhookURL:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return webhook_validation.ValidatedWebhookURL(
            url=url,
            hostname=parsed.hostname or "example.test",
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            resolved_ip="203.0.113.1",
        )

    http_factory = _HTTPClientFactory(statuses)
    monkeypatch.setattr(webhook_validation, "validate_webhook_url", _accept_url)
    monkeypatch.setattr(webhook_dispatcher.httpx, "AsyncClient", http_factory)
    monkeypatch.setattr(webhook_dispatcher, "_send_semaphore", None)
    return webhook_dispatcher, conn, http_factory


def _make_due(row: dict[str, Any]) -> None:
    row["scheduled_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)


def _successor_for(row: dict[str, Any]) -> dict[str, Any]:
    successor = _attempt(attempt_num=row["attempt_num"] + 1)
    successor.update({
        "subscription_id": row["subscription_id"],
        "event_type": row["event_type"],
        "payload": row["payload"],
        "payload_hash": row["payload_hash"],
    })
    return successor


def _old_v35_succeeded_peer_replay_candidates(rows: list[dict[str, Any]]) -> list[str]:
    succeeded_chains = {
        (row["subscription_id"], row["event_type"], row["payload_hash"])
        for row in rows
        if row["status"] == "succeeded"
    }
    return [
        row["id"]
        for row in rows
        if row["status"] in {"pending", "retrying"}
        and (row["subscription_id"], row["event_type"], row["payload_hash"]) in succeeded_chains
    ]


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

        assert first["status"] == "abandoned"
        assert first["superseded"] is True
        assert second["status"] == "succeeded"
        assert second["superseded"] is False
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
            "abandoned",
            "abandoned",
            "pending",
        ]
        assert [row["superseded"] for row in conn.rows] == [True, True, False]
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
        assert first["status"] == "abandoned"
        assert first["superseded"] is True
        assert second["status"] == "succeeded"
        assert second["superseded"] is False
        assert http.delivery_ids == [first["id"], second["id"]]

    asyncio.run(run())


def test_final_attempt_failure_is_abandoned_without_successor(monkeypatch):
    async def run():
        from mnemos.webhooks.dispatcher import MAX_ATTEMPTS

        final = _attempt(attempt_num=MAX_ATTEMPTS)
        dispatcher, conn, http = await _install(monkeypatch, [final], [500])

        await dispatcher._attempt_delivery(final["id"])

        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert recoverable == []
        assert len(conn.rows) == 1
        assert final["status"] == "abandoned"
        assert final["superseded"] is False
        assert http.delivery_ids == [final["id"]]

    asyncio.run(run())


def test_old_worker_compat_skips_superseded_abandoned_attempt():
    row = _attempt()
    row["status"] = "abandoned"
    row["superseded"] = True

    def old_worker_would_post(delivery: dict[str, Any]) -> bool:
        return delivery["status"] not in {"succeeded", "abandoned"}

    assert not old_worker_would_post(row)


def test_delivery_audit_exposes_superseded_marker_for_abandoned_rows():
    repo_root = Path(__file__).resolve().parents[1]
    handler_source = (repo_root / "mnemos" / "api" / "routes" / "webhooks.py").read_text()
    model_source = (repo_root / "mnemos" / "domain" / "models.py").read_text()
    compact_handler = " ".join(handler_source.split())

    assert "superseded: bool = False" in model_source
    assert "SELECT id, subscription_id, event_type, attempt_num, status, superseded," in compact_handler
    assert "superseded=r[\"superseded\"]" in compact_handler


def test_successor_insert_uses_live_chain_attempt_uniqueness(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, _http = await _install(monkeypatch, [parent], [])
        delivery = conn._with_subscription(parent)
        scheduled_at = datetime.now(timezone.utc)
        next_attempt = parent["attempt_num"] + 1

        first = await dispatcher._insert_successor_delivery(
            conn, delivery, next_attempt, scheduled_at,
        )
        second = await dispatcher._insert_successor_delivery(
            conn, delivery, next_attempt, scheduled_at,
        )

        live_successors = [
            row for row in conn.rows
            if row["subscription_id"] == parent["subscription_id"]
            and row["event_type"] == parent["event_type"]
            and row["payload_hash"] == parent["payload_hash"]
            and row["attempt_num"] == next_attempt
            and row["status"] in {"pending", "retrying"}
            and not row["superseded"]
        ]
        assert first is not None
        assert second is None
        assert len(live_successors) == 1

    asyncio.run(run())


def test_recovery_dequeue_uses_skip_locked_claim():
    source = _webhook_module_source("workers", "sender", "types")
    compact = " ".join(source.split())

    assert "async def _claim_recoverable_deliveries" in source
    assert "def _semaphore_available" in source
    assert "WITH claim_clock AS" in compact
    assert "FOR UPDATE SKIP LOCKED" in compact
    assert "UPDATE webhook_deliveries d SET lease_token=$2::uuid" in compact
    assert "_attempt_delivery(str(claimed.delivery[\"id\"]), pool=pool, claimed=claimed)" in compact
    assert "min(50, limit, _semaphore_available())" in compact
    assert "max(0, _get_send_semaphore()._value)" in compact
    assert "pre_claim_monotonic = time.monotonic()" in compact
    assert "pre_claim_monotonic=claimed.pre_claim_monotonic" in compact
    assert "claim_observed_monotonic = time.monotonic()" not in compact
    assert "FOR UPDATE OF d SKIP LOCKED" not in compact
    assert "_attempt_delivery_locked" not in compact


def test_recoverable_predicate_requires_current_writer_revision():
    source = _webhook_module_source("workers", "lease", "types")
    compact = " ".join(source.split())

    # NEW_CODE_WRITER_REVISION canonically lives in mnemos.core.webhook_constants
    # so the persistence layer can reference it without importing webhooks
    # (per the import-linter "persistence has no upward deps" contract).
    # webhooks/types.py re-exports the symbol; verify the source-of-truth.
    repo_root = Path(__file__).resolve().parents[1]
    constants_source = (repo_root / "mnemos" / "core" / "webhook_constants.py").read_text()
    assert "NEW_CODE_WRITER_REVISION = 1" in constants_source
    assert "AND d.status NOT IN ('succeeded', 'abandoned') AND NOT d.superseded" in compact
    assert "AND NOT d.superseded AND d.status IN ('pending', 'retrying')" in compact
    assert "AND d.writer_revision = $3" in compact
    assert "AND d.writer_revision = $5" in compact
    assert "status_updated_at + " not in compact
    assert "lease_expires_at=claim_clock.claim_now + ($3::int * INTERVAL '1 second')" in compact
    assert "claim_clock.claim_now AS claim_db_now" in compact
    assert "NOW() AS claim_db_now" not in compact


def test_non_current_writer_retrying_is_not_recoverable(monkeypatch):
    async def run():
        for writer_revision in (None, 0):
            parent = _attempt()
            parent["status"] = "retrying"
            parent["writer_revision"] = writer_revision
            dispatcher, conn, _http = await _install(monkeypatch, [parent], [])

            parent["scheduled_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
            parent["status_updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
            recoverable = await dispatcher._recoverable_delivery_ids(conn)
            assert recoverable == []

    asyncio.run(run())


def test_non_current_writer_pending_is_not_recoverable(monkeypatch):
    async def run():
        parent = _attempt()
        parent["writer_revision"] = None
        dispatcher, conn, _http = await _install(monkeypatch, [parent], [])

        parent["scheduled_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        parent["status_updated_at"] = parent["scheduled_at"]
        recoverable = await dispatcher._recoverable_delivery_ids(conn)
        assert recoverable == []

    asyncio.run(run())


def test_lease_less_new_writer_pending_and_retrying_recover_immediately(monkeypatch):
    async def run():
        for status in ("pending", "retrying"):
            parent = _attempt()
            parent["status"] = status
            dispatcher, conn, _http = await _install(monkeypatch, [parent], [])
            parent["writer_revision"] = dispatcher.NEW_CODE_WRITER_REVISION

            parent["scheduled_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            parent["status_updated_at"] = datetime.now(timezone.utc)
            recoverable = await dispatcher._recoverable_delivery_ids(conn)
            assert [row["id"] for row in recoverable] == [parent["id"]]

    asyncio.run(run())


def test_status_updated_at_trigger_model_advances_on_status_change(monkeypatch):
    async def run():
        parent = _attempt()
        _dispatcher, conn, _http = await _install(monkeypatch, [parent], [])

        before = parent["status_updated_at"]
        result = await conn.execute(
            "UPDATE webhook_deliveries SET status='retrying' WHERE id=$1::uuid",
            parent["id"],
        )

        assert result == "UPDATE 1"
        assert parent["status"] == "retrying"
        assert parent["status_updated_at"] > before

    asyncio.run(run())


def test_concurrent_recovery_claims_retrying_row_once(monkeypatch):
    async def run():
        from mnemos.core import lifecycle

        retrying = _attempt()
        retrying["status"] = "retrying"
        retrying["lease_token"] = "00000000-0000-0000-0000-000000000001"
        retrying["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        dispatcher, conn, http = await _install(monkeypatch, [retrying], [204])
        http.delay = 0.05
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())

        recovered = await asyncio.gather(
            dispatcher._recover_due_deliveries(conn.pool, limit=1),
            dispatcher._recover_due_deliveries(conn.pool, limit=1),
        )
        tasks = list(lifecycle._delivery_attempt_tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)

        assert sum(recovered) >= 1
        assert conn.store.advisory_acquisitions >= 1
        assert retrying["status"] == "succeeded"
        assert http.delivery_ids == [retrying["id"]]

    asyncio.run(run())


def test_recovery_preclaims_rows_before_send_semaphore_backpressure(monkeypatch):
    async def run():
        from mnemos.core import lifecycle

        rows = [_attempt() for _ in range(5)]
        dispatcher, conn, http = await _install(monkeypatch, rows, [204] * len(rows))
        http.delay = 0.05
        monkeypatch.setattr(dispatcher, "_send_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        scheduled_tasks = []
        real_schedule = lifecycle._schedule_delivery_attempt

        def _record_schedule(coro):
            task = real_schedule(coro)
            scheduled_tasks.append(task)
            return task

        monkeypatch.setattr(lifecycle, "_schedule_delivery_attempt", _record_schedule)

        recovered = [
            await dispatcher._recover_due_deliveries(conn.pool, limit=5),
            await dispatcher._recover_due_deliveries(conn.pool, limit=5),
            await dispatcher._recover_due_deliveries(conn.pool, limit=5),
        ]
        await asyncio.wait_for(asyncio.gather(*scheduled_tasks), timeout=2.0)

        assert recovered == [1, 0, 0]
        assert len(scheduled_tasks) == 1
        assert http.max_active == 1
        assert http.delivery_ids == [rows[0]["id"]]
        assert len(http.delivery_ids) == len(set(http.delivery_ids)) == 1
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["lease_token"] is None
        assert [row["lease_token"] for row in rows[1:]] == [None] * 4

    asyncio.run(run())


def test_recovery_claim_batch_uses_available_send_slots(monkeypatch):
    async def run():
        from mnemos.core import lifecycle

        rows = [_attempt() for _ in range(5)]
        dispatcher, conn, _http = await _install(monkeypatch, rows, [])
        monkeypatch.setattr(dispatcher, "_send_semaphore", asyncio.Semaphore(2))
        scheduled = []

        def _record_schedule(coro):
            scheduled.append(coro)
            return None

        monkeypatch.setattr(lifecycle, "_schedule_delivery_attempt", _record_schedule)

        recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=5)
        for coro in scheduled:
            coro.close()

        leased = [row for row in rows if row["lease_token"] is not None]
        assert recovered == 2
        assert len(scheduled) == 2
        assert len(leased) == 2
        assert [row["status"] for row in leased] == ["retrying", "retrying"]

    asyncio.run(run())


def test_recovery_backpressure_does_not_burn_retries(monkeypatch):
    async def run():
        from mnemos.core import lifecycle
        from mnemos.webhooks.dispatcher import MAX_ATTEMPTS

        rows = [_attempt() for _ in range(10)]
        original_rows = list(rows)
        expected_delivery_count = len(original_rows) * MAX_ATTEMPTS
        dispatcher, conn, http = await _install(monkeypatch, rows, [500] * (len(rows) * MAX_ATTEMPTS))
        http.delay = 0.01
        monkeypatch.setattr(dispatcher, "_send_semaphore", asyncio.Semaphore(2))
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())

        while len(http.delivery_ids) < expected_delivery_count:
            for row in conn.rows:
                if row["status"] == "pending":
                    _make_due(row)

            recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=len(rows))
            tasks = list(lifecycle._delivery_attempt_tasks)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
            elif recovered == 0:
                await asyncio.sleep(0.01)

        assert http.max_active <= 2
        assert len(http.delivery_ids) == expected_delivery_count
        for original in original_rows:
            chain = [
                row for row in conn.rows
                if row["subscription_id"] == original["subscription_id"]
                and row["event_type"] == original["event_type"]
                and row["payload_hash"] == original["payload_hash"]
            ]
            assert sorted(row["attempt_num"] for row in chain) == list(range(1, MAX_ATTEMPTS + 1))
            assert sum(row["id"] in http.delivery_ids for row in chain) == MAX_ATTEMPTS
            assert all(row["status"] == "abandoned" for row in chain)
            assert sum(not row["superseded"] for row in chain) == 1
            assert max(row["attempt_num"] for row in chain if not row["superseded"]) == MAX_ATTEMPTS

    asyncio.run(asyncio.wait_for(run(), timeout=10.0))


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
        row["lease_token"] = "00000000-0000-0000-0000-000000000002"
        row["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        current_lease_expires_at = row["lease_expires_at"]
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
        assert row["lease_token"] == "00000000-0000-0000-0000-000000000002"
        assert row["lease_expires_at"] == current_lease_expires_at

    asyncio.run(run())


def test_success_finalize_does_not_abandon_same_row_prior_success(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000015"
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])

        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        # The live-row guard prevents any out-of-order terminal writer from
        # turning an already succeeded row into a fresh success transaction.
        row["status"] = "succeeded"
        row["response_status"] = 202
        row["response_body"] = "prior metadata"
        row["delivered_at"] = datetime.now(timezone.utc)
        assert row["lease_token"] == lease_token

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=204,
                response_body="new acknowledged",
                error=None,
            ),
        )

        assert not finalized
        assert conn.store.succeeded_update_attempts == 1
        assert row["status"] == "succeeded"
        assert row["superseded"] is False
        assert row["response_status"] == 202
        assert row["response_body"] == "prior metadata"
        assert row["lease_token"] is None

    asyncio.run(run())


def test_success_finalize_does_not_resurrect_same_row_prior_abandon(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000018"
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])

        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        row["status"] = "abandoned"
        row["superseded"] = False
        row["response_status"] = 410
        row["response_body"] = "prior gone"
        row["error"] = "prior abandoned"
        row["delivered_at"] = datetime.now(timezone.utc)
        assert row["lease_token"] == lease_token

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=204,
                response_body="new worker acknowledged",
                error=None,
            ),
        )

        assert not finalized
        assert conn.store.succeeded_update_attempts == 1
        assert row["status"] == "abandoned"
        assert row["superseded"] is False
        assert row["response_status"] == 410
        assert row["response_body"] == "prior gone"
        assert row["error"] == "prior abandoned"
        assert row["lease_token"] is None

    asyncio.run(run())


def test_failure_finalize_does_not_clobber_same_row_prior_success(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000016"
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])

        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        # The live-row guard keeps a stale failure finalize from overwriting
        # any already terminal same-row success.
        row["status"] = "succeeded"
        row["response_status"] = 204
        row["response_body"] = "prior acknowledged"
        row["delivered_at"] = datetime.now(timezone.utc)
        assert row["lease_token"] == lease_token

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            dispatcher._DeliveryResult(
                succeeded=False,
                response_status=500,
                response_body="server error",
                error=None,
            ),
        )

        assert not finalized
        assert len(conn.rows) == 1
        assert sum(candidate["status"] == "succeeded" for candidate in conn.rows) == 1
        assert not any(
            candidate["status"] in {"pending", "retrying"}
            and candidate["attempt_num"] > row["attempt_num"]
            for candidate in conn.rows
        )
        assert row["status"] == "succeeded"
        assert row["superseded"] is False
        assert row["response_status"] == 204
        assert row["response_body"] == "prior acknowledged"
        assert row["lease_token"] == lease_token

    asyncio.run(run())


def test_failure_finalize_does_not_clobber_same_row_prior_abandon(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000017"
        dispatcher, conn, _http = await _install(monkeypatch, [row], [])

        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        row["status"] = "abandoned"
        row["superseded"] = False
        row["error"] = "prior abandoned"
        row["delivered_at"] = datetime.now(timezone.utc)
        assert row["lease_token"] == lease_token

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            dispatcher._DeliveryResult(
                succeeded=False,
                response_status=500,
                response_body="server error",
                error=None,
            ),
        )

        assert not finalized
        assert len(conn.rows) == 1
        assert not any(
            candidate["status"] in {"pending", "retrying"}
            and candidate["attempt_num"] > row["attempt_num"]
            for candidate in conn.rows
        )
        assert row["status"] == "abandoned"
        assert row["superseded"] is False
        assert row["response_status"] is None
        assert row["error"] == "prior abandoned"
        assert row["lease_token"] == lease_token

    asyncio.run(run())


def test_success_finalize_cancels_free_live_successor(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        real_send = dispatcher._send_claimed_delivery

        async def _send_then_old_writer_successor(delivery, *, pre_claim_monotonic):
            result = await real_send(
                delivery,
                pre_claim_monotonic=pre_claim_monotonic,
            )
            conn.rows.append(_successor_for(parent))
            return result

        monkeypatch.setattr(dispatcher, "_send_claimed_delivery", _send_then_old_writer_successor)

        finalized = await dispatcher._attempt_delivery(parent["id"])
        successor = conn.rows[1]
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert finalized
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert parent["superseded"] is False
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert recoverable == []

    asyncio.run(run())


def test_success_commit_cleanup_failure_rolls_back_ack_and_successors(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        real_send = dispatcher._send_claimed_delivery
        real_abandon = dispatcher._abandon_live_successor_attempt

        async def _send_then_old_writer_successor(delivery, *, pre_claim_monotonic):
            result = await real_send(
                delivery,
                pre_claim_monotonic=pre_claim_monotonic,
            )
            conn.rows.append(_successor_for(parent))
            return result

        async def _fail_successor_cleanup_after_update(cleanup_conn, successor_id):
            await real_abandon(cleanup_conn, successor_id)
            raise RuntimeError("successor cleanup failed")

        monkeypatch.setattr(dispatcher, "_send_claimed_delivery", _send_then_old_writer_successor)
        monkeypatch.setattr(
            dispatcher,
            "_abandon_live_successor_attempt",
            _fail_successor_cleanup_after_update,
        )

        try:
            await dispatcher._attempt_delivery(parent["id"])
        except RuntimeError as exc:
            assert "successor cleanup failed" in str(exc)
        else:
            raise AssertionError("success cleanup failure should roll back the success transaction")
        successor = conn.rows[1]

        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "retrying"
        assert parent["superseded"] is False
        assert parent["lease_token"] is not None
        assert parent["lease_expires_at"] is not None
        assert successor["status"] == "pending"
        assert successor["superseded"] is False
        assert conn.store.savepoint_commands == []

        monkeypatch.setattr(dispatcher, "_abandon_live_successor_attempt", real_abandon)
        retry_result = dispatcher._DeliveryResult(
            succeeded=True,
            response_status=204,
            response_body=None,
        )
        retried = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(parent),
            parent["lease_token"],
            retry_result,
        )

        assert retried
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True

    asyncio.run(run())


def test_success_commit_abandons_successors_atomically_without_savepoints(monkeypatch):
    async def run():
        parent = _attempt()
        successors = []
        for attempt_num in (2, 3, 4):
            successor = _successor_for(parent)
            successor["attempt_num"] = attempt_num
            successors.append(successor)
        dispatcher, conn, http = await _install(monkeypatch, [parent, *successors], [204])

        finalized = await dispatcher._attempt_delivery(parent["id"])

        assert finalized
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert [successor["status"] for successor in successors] == [
            "abandoned",
            "abandoned",
            "abandoned",
        ]
        assert [successor["superseded"] for successor in successors] == [True, True, True]
        assert conn.store.bulk_successor_fetches == 1
        assert conn.store.single_successor_fetches == 0
        assert conn.store.successor_cleanup_updates == len(successors)
        assert conn.store.savepoint_commands == []

    asyncio.run(run())


def test_mixed_version_cleanup_error_leaves_no_succeeded_predecessor_for_old_worker(monkeypatch):
    async def run():
        parent = _attempt()
        successor = _successor_for(parent)
        dispatcher, conn, http = await _install(monkeypatch, [parent, successor], [204])
        real_abandon = dispatcher._abandon_live_successor_attempt

        pre_fix_parent = dict(parent)
        pre_fix_parent["status"] = "succeeded"
        assert _old_v35_succeeded_peer_replay_candidates(
            [pre_fix_parent, dict(successor)]
        ) == [successor["id"]]

        async def _fail_successor_cleanup_after_update(cleanup_conn, successor_id):
            await real_abandon(cleanup_conn, successor_id)
            raise RuntimeError("successor cleanup failed")

        monkeypatch.setattr(
            dispatcher,
            "_abandon_live_successor_attempt",
            _fail_successor_cleanup_after_update,
        )

        try:
            await dispatcher._attempt_delivery(parent["id"])
        except RuntimeError:
            pass
        else:
            raise AssertionError("success cleanup failure should roll back the success transaction")

        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "retrying"
        assert parent["lease_token"] is not None
        assert successor["status"] == "pending"
        assert successor["superseded"] is False
        assert _old_v35_succeeded_peer_replay_candidates(conn.rows) == []
        assert conn.store.bulk_successor_fetches == 1
        assert conn.store.successor_cleanup_updates == 1
        assert conn.store.savepoint_commands == []

        monkeypatch.setattr(dispatcher, "_abandon_live_successor_attempt", real_abandon)
        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(parent),
            parent["lease_token"],
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=204,
                response_body=None,
            ),
        )

        assert finalized
        assert parent["status"] == "succeeded"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert _old_v35_succeeded_peer_replay_candidates(conn.rows) == []

    asyncio.run(run())


def test_success_commit_cancelled_in_transaction_rolls_back_for_retry(monkeypatch):
    async def run():
        parent = _attempt()
        successor = _successor_for(parent)
        dispatcher, conn, http = await _install(monkeypatch, [parent, successor], [204])
        real_abandon = dispatcher._abandon_live_successor_attempt
        cleanup_started = asyncio.Event()
        cleanup_cancelled = asyncio.Event()

        async def _block_successor_cleanup(_conn, _successor_id):
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

        monkeypatch.setattr(dispatcher, "_abandon_live_successor_attempt", _block_successor_cleanup)

        task = asyncio.create_task(dispatcher._attempt_delivery(parent["id"]))
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)

        assert parent["status"] == "succeeded"

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("successor cleanup cancellation should propagate")

        assert cleanup_cancelled.is_set()
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "retrying"
        assert parent["superseded"] is False
        assert parent["lease_token"] is not None
        assert parent["lease_expires_at"] is not None
        assert successor["status"] == "pending"
        assert successor["superseded"] is False

        # Production recovery may resend after lease expiry; this direct finalize
        # models the bounded same-attempt retry once cancellation stops.
        monkeypatch.setattr(dispatcher, "_abandon_live_successor_attempt", real_abandon)
        retry_result = dispatcher._DeliveryResult(
            succeeded=True,
            response_status=204,
            response_body=None,
        )
        retried = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(parent),
            parent["lease_token"],
            retry_result,
        )

        assert retried
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True

    asyncio.run(run())


def test_success_finalize_leaves_active_successor_lease_alone(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        real_send = dispatcher._send_claimed_delivery

        async def _send_then_active_successor(delivery, *, pre_claim_monotonic):
            result = await real_send(
                delivery,
                pre_claim_monotonic=pre_claim_monotonic,
            )
            successor = _successor_for(parent)
            successor["status"] = "retrying"
            successor["lease_token"] = "00000000-0000-0000-0000-000000000099"
            successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
            conn.rows.append(successor)
            return result

        monkeypatch.setattr(dispatcher, "_send_claimed_delivery", _send_then_active_successor)

        finalized = await dispatcher._attempt_delivery(parent["id"])
        successor = conn.rows[1]

        assert finalized
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert parent["superseded"] is False
        assert successor["status"] == "retrying"
        assert successor["superseded"] is False
        assert successor["lease_token"] == "00000000-0000-0000-0000-000000000099"

    asyncio.run(run())


def test_active_successor_failure_after_predecessor_success_closes_chain(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        real_send = dispatcher._send_claimed_delivery

        async def _send_then_active_successor(delivery, *, pre_claim_monotonic):
            result = await real_send(
                delivery,
                pre_claim_monotonic=pre_claim_monotonic,
            )
            successor = _successor_for(parent)
            successor["status"] = "retrying"
            successor["lease_token"] = "00000000-0000-0000-0000-000000000099"
            successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
            conn.rows.append(successor)
            return result

        monkeypatch.setattr(dispatcher, "_send_claimed_delivery", _send_then_active_successor)

        parent_finalized = await dispatcher._attempt_delivery(parent["id"])
        successor = conn.rows[1]
        successor_finalized = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(successor),
            successor["lease_token"],
            dispatcher._DeliveryResult(
                succeeded=False,
                response_status=500,
                response_body="server error",
                error=None,
            ),
        )

        assert parent_finalized
        assert successor_finalized
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert successor["response_status"] == 500
        assert len(conn.rows) == 2

    asyncio.run(run())


def test_active_successor_success_after_predecessor_success_closes_chain(monkeypatch):
    async def run():
        parent = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        real_send = dispatcher._send_claimed_delivery

        async def _send_then_active_successor(delivery, *, pre_claim_monotonic):
            result = await real_send(
                delivery,
                pre_claim_monotonic=pre_claim_monotonic,
            )
            successor = _successor_for(parent)
            successor["status"] = "retrying"
            successor["lease_token"] = "00000000-0000-0000-0000-000000000099"
            successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
            conn.rows.append(successor)
            return result

        monkeypatch.setattr(dispatcher, "_send_claimed_delivery", _send_then_active_successor)

        parent_finalized = await dispatcher._attempt_delivery(parent["id"])
        successor = conn.rows[1]
        successor_finalized = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(successor),
            successor["lease_token"],
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=204,
                response_body="successor acknowledged",
                error=None,
            ),
        )

        assert parent_finalized
        assert successor_finalized
        assert http.delivery_ids == [parent["id"]]
        assert parent["status"] == "succeeded"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert successor["response_status"] == 204
        assert successor["response_body"] == "successor acknowledged"
        assert successor["lease_token"] is None
        assert sum(row["status"] == "succeeded" for row in conn.rows) == 1

    asyncio.run(run())


def test_predecessor_success_after_successor_success_closes_chain(monkeypatch):
    async def run():
        parent = _attempt()
        successor = _successor_for(parent)
        parent["status"] = "retrying"
        parent["lease_token"] = "00000000-0000-0000-0000-000000000011"
        parent["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        successor["status"] = "retrying"
        successor["lease_token"] = "00000000-0000-0000-0000-000000000012"
        successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        successor_finalized = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(successor),
            successor["lease_token"],
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=204,
                response_body="successor acknowledged",
                error=None,
            ),
        )
        parent_finalized = await dispatcher._finalize_delivery(
            conn.pool,
            conn._with_subscription(parent),
            parent["lease_token"],
            dispatcher._DeliveryResult(
                succeeded=True,
                response_status=202,
                response_body="parent acknowledged later",
                error=None,
            ),
        )

        assert successor_finalized
        assert parent_finalized
        assert sum(row["status"] == "succeeded" for row in conn.rows) == 1
        assert successor["status"] == "succeeded"
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["response_status"] == 202
        assert parent["response_body"] == "parent acknowledged later"
        assert parent["lease_token"] is None

    asyncio.run(run())


def test_success_finalize_unique_violation_abandons_duplicate_chain_peer(monkeypatch):
    class _FakeUniqueViolationError(Exception):
        pass

    async def run():
        parent = _attempt()
        successor = _successor_for(parent)
        parent["status"] = "retrying"
        parent["lease_token"] = "00000000-0000-0000-0000-000000000021"
        parent["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        successor["status"] = "retrying"
        successor["lease_token"] = "00000000-0000-0000-0000-000000000022"
        successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        async def _race_window_guard(_conn, _delivery, _delivery_id):
            return False

        conn.store.enforce_succeeded_unique = True
        conn.store.unique_violation_cls = _FakeUniqueViolationError
        monkeypatch.setattr(
            dispatcher.asyncpg.exceptions,
            "UniqueViolationError",
            _FakeUniqueViolationError,
        )
        monkeypatch.setattr(dispatcher, "_has_succeeded_chain_attempt", _race_window_guard)

        results = await asyncio.gather(
            dispatcher._finalize_delivery(
                conn.pool,
                conn._with_subscription(parent),
                parent["lease_token"],
                dispatcher._DeliveryResult(
                    succeeded=True,
                    response_status=200,
                    response_body="parent acknowledged",
                    error=None,
                ),
            ),
            dispatcher._finalize_delivery(
                conn.pool,
                conn._with_subscription(successor),
                successor["lease_token"],
                dispatcher._DeliveryResult(
                    succeeded=True,
                    response_status=201,
                    response_body="successor acknowledged",
                    error=None,
                ),
            ),
        )

        assert results == [True, True]
        assert conn.store.succeeded_update_attempts == 2
        assert sum(row["status"] == "succeeded" for row in conn.rows) == 1
        abandoned = [row for row in conn.rows if row["status"] == "abandoned"]
        assert len(abandoned) == 1
        assert abandoned[0]["superseded"] is True
        assert abandoned[0]["response_status"] in {200, 201}
        assert abandoned[0]["response_body"] in {
            "parent acknowledged",
            "successor acknowledged",
        }
        assert all(row["lease_token"] is None for row in conn.rows)

    asyncio.run(run())


def test_expired_successor_after_predecessor_success_is_repaired(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "succeeded"
        successor = _successor_for(parent)
        successor["status"] = "retrying"
        successor["lease_token"] = "00000000-0000-0000-0000-000000000099"
        successor["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        result = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert result == "UPDATE 1"
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert successor["lease_token"] is None
        assert recoverable == []

    asyncio.run(run())


def test_claim_after_predecessor_success_terminalizes_without_send(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "succeeded"
        successor = _successor_for(parent)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        claimed = await dispatcher._claim_delivery(
            conn.pool,
            successor["id"],
            lease_token="00000000-0000-0000-0000-000000000099",
        )

        assert claimed is None
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert successor["lease_token"] is None

    asyncio.run(run())


def test_preclaimed_successor_waits_for_predecessor_success_before_post(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        successor = _successor_for(parent)
        dispatcher, conn, http = await _install(monkeypatch, [parent, successor], [204])
        blocker = _FakeWebhookConn(conn.store, conn_id=999)

        async with blocker.transaction():
            await dispatcher._lock_delivery_chain(blocker, parent)
            claimed = await dispatcher._claim_recoverable_deliveries(conn, limit=1)
            assert [item.delivery["id"] for item in claimed] == [successor["id"]]

            task = asyncio.create_task(
                dispatcher._attempt_delivery(
                    successor["id"],
                    pool=conn.pool,
                    claimed=claimed[0],
                )
            )
            await asyncio.sleep(0.01)
            assert http.delivery_ids == []

            parent["status"] = "succeeded"
            parent["response_status"] = 204
            parent["delivered_at"] = datetime.now(timezone.utc)

        sent = await task

        assert not sent
        assert http.delivery_ids == []
        assert successor["status"] == "abandoned"
        assert successor["superseded"] is True
        assert successor["lease_token"] is None
        assert conn.store.advisory_acquisitions >= 2

    asyncio.run(run())


def test_preclaim_then_successor_inserted_before_send_abandons_parent(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        claimed = await dispatcher._claim_recoverable_deliveries(conn, limit=1)
        successor = _successor_for(parent)
        conn.rows.append(successor)

        sent = await dispatcher._attempt_delivery(
            parent["id"],
            pool=conn.pool,
            claimed=claimed[0],
        )
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert not sent
        assert http.delivery_ids == []
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["lease_token"] is None
        assert successor["status"] == "pending"
        assert successor["superseded"] is False
        assert [row["id"] for row in recoverable] == [successor["id"]]

    asyncio.run(run())


def test_preclaim_with_active_leased_successor_abandons_parent(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        dispatcher, conn, http = await _install(monkeypatch, [parent], [204])
        claimed = await dispatcher._claim_recoverable_deliveries(conn, limit=1)
        successor = _successor_for(parent)
        successor["status"] = "retrying"
        successor["lease_token"] = "00000000-0000-0000-0000-000000000099"
        successor["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        conn.rows.append(successor)

        sent = await dispatcher._attempt_delivery(
            parent["id"],
            pool=conn.pool,
            claimed=claimed[0],
        )
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert not sent
        assert http.delivery_ids == []
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["lease_token"] is None
        assert successor["status"] == "retrying"
        assert successor["superseded"] is False
        assert successor["lease_token"] == "00000000-0000-0000-0000-000000000099"
        assert recoverable == []

    asyncio.run(run())


def test_preclaimed_clean_chain_posts_normally(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [204])
        claimed = await dispatcher._claim_recoverable_deliveries(conn, limit=1)

        sent = await dispatcher._attempt_delivery(
            row["id"],
            pool=conn.pool,
            claimed=claimed[0],
        )

        assert sent
        assert http.delivery_ids == [row["id"]]
        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert conn.store.advisory_acquisitions >= 2

    asyncio.run(run())


def test_preclaimed_stale_lease_releases_without_post(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [204])
        claimed = await dispatcher._claim_recoverable_deliveries(conn, limit=1)
        assert claimed
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        sent = await dispatcher._attempt_delivery(
            row["id"],
            pool=conn.pool,
            claimed=claimed[0],
        )
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert not sent
        assert http.delivery_ids == []
        assert row["status"] == "retrying"
        assert row["superseded"] is False
        assert row["lease_token"] is None
        assert [item["id"] for item in recoverable] == [row["id"]]

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


def test_slow_2xx_body_finalizes_success_without_retry(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        lease_seconds = 5
        finalize_buffer = 1.0
        send_deadline = dispatcher._derive_total_send_deadline_seconds(
            lease_seconds,
            finalize_buffer,
        )
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(dispatcher, "WEBHOOK_LEASE_SECONDS", lease_seconds)
        monkeypatch.setattr(dispatcher, "WEBHOOK_FINALIZE_BUFFER_SECONDS", finalize_buffer)
        monkeypatch.setattr(dispatcher, "TOTAL_SEND_DEADLINE_SECONDS", send_deadline)
        monkeypatch.setattr(dispatcher, "WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(dispatcher, "_send_semaphore", semaphore)
        http.response_chunks = [[b"trickle"]]
        http.chunk_delay = 0.1
        http.never_complete = True

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        task = asyncio.create_task(dispatcher._attempt_delivery(row["id"]))
        await asyncio.wait_for(http.started.wait(), timeout=0.5)

        for _ in range(20):
            if row["status"] == "succeeded":
                break
            await asyncio.sleep(0)
        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert row["response_body"] is None
        recovered_while_leased = await dispatcher._recover_due_deliveries(conn.pool, limit=1)
        assert recovered_while_leased == 0
        assert http.delivery_ids == [row["id"]]

        finalized = await task
        elapsed = loop.time() - started_at

        assert finalized
        assert elapsed < 0.5
        assert http.cancelled
        assert http.exited == 1
        assert not semaphore.locked()
        assert row["status"] == "succeeded"
        assert row["superseded"] is False
        assert row["response_status"] == 200
        assert row["response_body"] is None
        assert row["error"] is None
        assert len(conn.rows) == 1

    asyncio.run(run())


def test_late_2xx_with_recovery_steal_finalizes_before_body_capture(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        capture_started = asyncio.Event()
        capture_can_finish = asyncio.Event()
        real_read = dispatcher._read_capped_response_body
        http.response_chunks = [[b"late body"]]
        monkeypatch.setattr(dispatcher, "WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS", 60.0)

        async def _blocked_body_read(response):
            capture_started.set()
            await capture_can_finish.wait()
            return await real_read(response)

        monkeypatch.setattr(dispatcher, "_read_capped_response_body", _blocked_body_read)

        task = asyncio.create_task(dispatcher._attempt_delivery(row["id"]))
        await asyncio.wait_for(capture_started.wait(), timeout=0.5)

        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=1)

        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert row["response_status"] == 200
        assert row["response_body"] is None
        assert recovered == 0
        assert http.delivery_ids == [row["id"]]

        capture_can_finish.set()
        finalized = await asyncio.wait_for(task, timeout=0.5)

        assert finalized
        assert row["response_body"] == "late body"
        assert http.delivery_ids == [row["id"]]

    asyncio.run(run())


def test_body_capture_timeout_after_finalize_leaves_response_body_null(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        monkeypatch.setattr(dispatcher, "WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS", 0.01)
        http.response_chunks = [[b"slow"]]
        http.chunk_delay = 0.1
        http.never_complete = True

        finalized = await dispatcher._attempt_delivery(row["id"])
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert finalized
        assert row["status"] == "succeeded"
        assert row["response_status"] == 200
        assert row["response_body"] is None
        assert row["lease_token"] is None
        assert recoverable == []
        assert http.delivery_ids == [row["id"]]

    asyncio.run(run())


def test_5xx_with_fast_body_still_retries(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [503])
        http.response_chunks = [[b"try later"]]

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert row["status"] == "abandoned"
        assert row["superseded"] is True
        assert row["response_status"] == 503
        assert row["response_body"] == "try later"
        assert len(conn.rows) == 2
        assert conn.rows[1]["status"] == "pending"

    asyncio.run(run())


def test_2xx_connection_reset_mid_body_finalizes_success(monkeypatch):
    async def run():
        import httpx

        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        http.response_chunks = [[b"partial"]]
        http.body_errors = [httpx.ReadError("reset")]

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert row["status"] == "succeeded"
        assert row["response_status"] == 200
        assert row["response_body"] == "[body capture: ReadError]"
        assert row["error"] is None
        assert len(conn.rows) == 1

    asyncio.run(run())


def test_webhook_send_refuses_post_when_claim_window_elapsed_before_send(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        semaphore = asyncio.Semaphore(1)
        ticks = [100.0, 106.0]

        def _fake_monotonic():
            if ticks:
                return ticks.pop(0)
            return 106.0

        monkeypatch.setattr(dispatcher, "WEBHOOK_LEASE_SECONDS", 5)
        monkeypatch.setattr(dispatcher, "WEBHOOK_FINALIZE_BUFFER_SECONDS", 1.0)
        monkeypatch.setattr(dispatcher, "_send_semaphore", semaphore)
        monkeypatch.setattr(dispatcher.time, "monotonic", _fake_monotonic)

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert http.delivery_ids == []
        assert not semaphore.locked()
        assert row["status"] == "retrying"
        assert row["superseded"] is False
        assert row["error"] is None
        assert row["lease_token"] is None
        assert len(conn.rows) == 1

    asyncio.run(run())


def test_webhook_send_refuses_post_after_app_stall_following_claim(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        fake_now = 100.0
        lease_seconds = 5

        def _fake_monotonic():
            return fake_now

        real_claim_delivery = dispatcher._claim_delivery

        async def _claim_then_stall(*args, **kwargs):
            nonlocal fake_now
            claimed = await real_claim_delivery(*args, **kwargs)
            await asyncio.sleep(0)
            fake_now += lease_seconds + 1.0
            return claimed

        monkeypatch.setattr(dispatcher, "WEBHOOK_LEASE_SECONDS", lease_seconds)
        monkeypatch.setattr(dispatcher, "WEBHOOK_FINALIZE_BUFFER_SECONDS", 1.0)
        monkeypatch.setattr(dispatcher, "_claim_delivery", _claim_then_stall)
        monkeypatch.setattr(dispatcher.time, "monotonic", _fake_monotonic)

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert http.delivery_ids == []
        assert row["status"] == "retrying"
        assert row["superseded"] is False
        assert row["error"] is None
        assert row["lease_token"] is None
        assert len(conn.rows) == 1

    asyncio.run(run())


def test_pre_claim_monotonic_debits_stalled_claim_update(monkeypatch):
    async def run():
        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        fake_now = 200.0

        def _fake_monotonic():
            return fake_now

        async def _stall_claim_update():
            nonlocal fake_now
            await asyncio.sleep(0)
            fake_now += dispatcher.WEBHOOK_LEASE_SECONDS + 1.0

        monkeypatch.setattr(dispatcher, "WEBHOOK_LEASE_SECONDS", 5)
        monkeypatch.setattr(dispatcher, "WEBHOOK_FINALIZE_BUFFER_SECONDS", 1.0)
        monkeypatch.setattr(dispatcher.time, "monotonic", _fake_monotonic)
        conn.store.claim_update_stall = _stall_claim_update

        finalized = await dispatcher._attempt_delivery(row["id"])

        assert finalized
        assert http.delivery_ids == []
        assert row["status"] == "retrying"
        assert row["superseded"] is False
        assert row["error"] is None
        assert row["lease_token"] is None
        assert len(conn.rows) == 1

    asyncio.run(run())


def test_webhook_send_preserves_2xx_result_when_stream_cleanup_fails(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000031"
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        http.stream_exit_errors = [RuntimeError("stream close failed")]

        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            result,
        )
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert result.succeeded is True
        assert result.response_status == 200
        assert finalized
        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert recoverable == []
        assert http.exited == 1

    asyncio.run(run())


def test_late_2xx_finalize_persists_success_after_lease_expiry(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000041"
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            result,
        )
        recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=1)

        assert result.succeeded is True
        assert finalized
        assert conn.store.succeeded_update_attempts == 1
        assert row["status"] == "succeeded"
        assert row["superseded"] is False
        assert row["response_status"] == 200
        assert row["lease_token"] is None
        assert recovered == 0
        assert http.delivery_ids == [row["id"]]

    asyncio.run(run())


def test_late_2xx_finalize_with_succeeded_peer_abandons_current_attempt(monkeypatch):
    async def run():
        row = _attempt()
        peer = _successor_for(row)
        lease_token = "00000000-0000-0000-0000-000000000042"
        dispatcher, conn, http = await _install(monkeypatch, [row, peer], [200])
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        peer["status"] = "succeeded"
        peer["response_status"] = 204
        peer["delivered_at"] = datetime.now(timezone.utc)

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            result,
        )
        recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=1)

        assert result.succeeded is True
        assert finalized
        assert conn.store.succeeded_update_attempts == 0
        assert row["status"] == "abandoned"
        assert row["superseded"] is True
        assert row["response_status"] == 200
        assert row["response_body"] == "status=200"
        assert row["lease_token"] is None
        assert peer["status"] == "succeeded"
        assert sum(candidate["status"] == "succeeded" for candidate in conn.rows) == 1
        assert recovered == 0
        assert http.delivery_ids == [row["id"]]

    asyncio.run(run())


def test_failure_finalize_still_requires_unexpired_lease(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000043"
        dispatcher, conn, http = await _install(monkeypatch, [row], [503])
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            result,
        )
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert result.succeeded is False
        assert finalized is False
        assert row["status"] == "retrying"
        assert row["superseded"] is False
        assert row["response_status"] is None
        assert row["lease_token"] == lease_token
        assert [candidate["id"] for candidate in recoverable] == [row["id"]]
        assert http.delivery_ids == [row["id"]]

    asyncio.run(run())


def test_webhook_send_cleanup_timeout_preserves_2xx_result(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000044"
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        monkeypatch.setattr(dispatcher, "WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS", 0.01)
        http.stream_exit_delay = 1.0
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        elapsed = loop.time() - started_at
        finalized = await dispatcher._finalize_delivery(
            conn.pool,
            claimed.delivery,
            lease_token,
            result,
        )
        await asyncio.sleep(0)

        assert result.succeeded is True
        assert finalized
        assert elapsed < 0.2
        assert http.stream_exit_cancelled
        assert row["status"] == "succeeded"
        assert row["response_status"] == 200
        assert row["lease_token"] is None

    asyncio.run(run())


def test_webhook_send_propagates_cancelled_error_from_stream_cleanup(monkeypatch):
    async def run():
        row = _attempt()
        lease_token = "00000000-0000-0000-0000-000000000032"
        dispatcher, conn, http = await _install(monkeypatch, [row], [200])
        claimed = await dispatcher._claim_delivery(
            conn.pool,
            row["id"],
            lease_token=lease_token,
        )
        assert claimed is not None

        http.stream_exit_errors = [asyncio.CancelledError()]

        result = await dispatcher._send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )

        try:
            await dispatcher._finalize_delivery(
                conn.pool,
                claimed.delivery,
                lease_token,
                result,
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("stream cleanup cancellation must propagate")

        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert http.exited == 1

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
        assert http.request_headers[0]["Accept-Encoding"] == "identity"

    asyncio.run(run())


def test_webhook_response_body_cap_bounds_ignored_gzip_body(monkeypatch):
    async def run():
        from mnemos.webhooks import dispatcher as dispatcher

        class _GzipResponse:
            headers = {"content-encoding": "gzip"}

            def __init__(self):
                self.raw = gzip.compress(b"x" * (5 * 1024 * 1024))
                self.chunks_yielded = 0

            async def aiter_raw(self):
                for i in range(0, len(self.raw), 7):
                    self.chunks_yielded += 1
                    yield self.raw[i:i + 7]

            async def aiter_bytes(self):
                raise AssertionError("transparent decompression path must not be used")

        monkeypatch.setattr(dispatcher, "WEBHOOK_RESPONSE_BODY_MAX_BYTES", 10)

        response = _GzipResponse()
        body = await dispatcher._read_capped_response_body(response)

        assert body != "x" * 10
        assert len(body.encode("utf-8")) <= 10
        assert response.chunks_yielded == 2

    asyncio.run(run())


def test_webhook_send_deadline_is_derived_from_lease_with_finalize_buffer():
    from mnemos.webhooks import dispatcher as dispatcher

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
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
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
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True

    asyncio.run(run())


def test_repair_sweep_normalizes_old_worker_retrying_resurrect(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "abandoned"
        parent["superseded"] = True
        successor = _successor_for(parent)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        resurrected = await conn.execute(
            "UPDATE webhook_deliveries SET status='retrying' WHERE id=$1::uuid",
            parent["id"],
        )
        assert resurrected == "UPDATE 1"
        assert parent["status"] == "retrying"
        assert parent["superseded"] is True

        result = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert result == "UPDATE 1"
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["id"] not in [row["id"] for row in recoverable]

    asyncio.run(run())


def test_repair_sweep_normalizes_old_worker_pending_resurrect(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "abandoned"
        parent["superseded"] = True
        successor = _successor_for(parent)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        resurrected = await conn.execute(
            "UPDATE webhook_deliveries SET status='pending' WHERE id=$1::uuid",
            parent["id"],
        )
        assert resurrected == "UPDATE 1"
        assert parent["status"] == "pending"
        assert parent["superseded"] is True

        result = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)
        recoverable = await dispatcher._recoverable_delivery_ids(conn)

        assert result == "UPDATE 1"
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["id"] not in [row["id"] for row in recoverable]

    asyncio.run(run())


def test_repair_sweep_does_not_strip_active_lease_with_successor(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "retrying"
        parent["lease_token"] = "00000000-0000-0000-0000-000000000001"
        parent["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        successor = _successor_for(parent)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        while_leased = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)

        assert while_leased == "UPDATE 0"
        assert parent["status"] == "retrying"
        assert parent["superseded"] is False
        assert parent["lease_token"] == "00000000-0000-0000-0000-000000000001"

        parent["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        after_expiry = await dispatcher.repair_superseded_retrying_deliveries(conn.pool)

        assert after_expiry == "UPDATE 1"
        assert parent["status"] == "abandoned"
        assert parent["superseded"] is True
        assert parent["lease_token"] is None
        assert parent["lease_expires_at"] is None

    asyncio.run(run())


def test_lifecycle_runs_webhook_retry_repair_on_startup():
    repo_root = Path(__file__).resolve().parents[1]
    lifecycle_hooks_source = (repo_root / "mnemos" / "api" / "lifecycle_hooks.py").read_text()
    dispatcher_source = _webhook_module_source("workers", "repair", "types")
    compact_dispatcher = " ".join(dispatcher_source.split())

    assert "repair_worker_loop" in lifecycle_hooks_source
    assert "delivery_worker_loop" in lifecycle_hooks_source
    assert 'register_lifespan_worker("webhook retry repair worker"' in lifecycle_hooks_source
    assert 'register_lifespan_worker("webhook delivery recovery worker"' in lifecycle_hooks_source
    assert "REPAIR_BURST_SECONDS" in dispatcher_source
    assert "REPAIR_PERIODIC_INTERVAL" in dispatcher_source
    assert "async def repair_worker_loop" in dispatcher_source
    assert "async def delivery_worker_loop" in dispatcher_source
    assert "_repair_superseded_retrying_deliveries_safely" in dispatcher_source
    assert "UPDATE webhook_deliveries d SET status = 'abandoned', superseded = TRUE" in compact_dispatcher
    assert "status_updated_at = clock_timestamp()" in compact_dispatcher
    assert "WHERE d.status IN ('pending', 'retrying')" in compact_dispatcher
    assert (
        "AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())"
        in compact_dispatcher
    )
    assert "WHERE d.status = 'retrying' AND NOT d.superseded" not in compact_dispatcher
    assert "newer.attempt_num > d.attempt_num" in compact_dispatcher
    assert "peer.status = 'succeeded'" in compact_dispatcher
    assert "peer.attempt_num < d.attempt_num" not in compact_dispatcher


def test_success_finalize_source_shape_is_chain_aware():
    source = _webhook_module_source("finalize", "chain", "lease")
    compact = " ".join(source.split())

    assert "async def _find_live_unleased_successor_attempt(" not in source
    assert "async def _find_live_unleased_successor_attempts" in source
    assert "async def _abandon_live_successor_attempt" in source
    assert "newer.status IN ('pending', 'retrying')" in compact
    assert "AND NOT newer.superseded" in compact
    assert "newer.lease_token IS NULL OR newer.lease_expires_at < clock_timestamp()" in compact
    assert "status='abandoned', superseded=TRUE, status_updated_at=clock_timestamp()" in compact
    assert "except asyncpg.exceptions.UniqueViolationError" in source
    assert "SAVEPOINT" not in source
    assert "_abandon_live_successors_after_success_commit" not in source
    assert "bounded duplicate POST" in source
    assert "async def _clear_stale_owned_lease_after_terminal_finalize" in source
    assert "SET lease_token=NULL, lease_expires_at=NULL WHERE id=$1::uuid AND lease_token=$2::uuid" in compact
    assert "webhook delivery %s was already terminal at success finalize time; stale lease cleared" in source


def test_succeeded_chain_guard_source_shape_is_chain_aware():
    source = _webhook_module_source("finalize", "chain", "lease")
    compact = " ".join(source.split())

    assert "async def _has_succeeded_chain_attempt" in source
    assert "async def _abandon_current_attempt_after_succeeded_chain_peer" in source
    assert "async def _abandon_owned_attempt_after_succeeded_chain_peer" in source
    assert "peer.attempt_num < $4" not in compact
    assert "peer.status = 'succeeded'" in compact
    assert "peer.id <> $4::uuid" in compact
    assert "_has_succeeded_chain_attempt(conn, delivery, delivery_id)" in compact
    assert "AND status IN ('pending', 'retrying') AND NOT superseded" in compact


def test_failure_finalize_source_shape_requires_live_owned_attempt():
    finalize_source = _webhook_module_source("finalize")
    compact = " ".join(finalize_source.split())
    success_update = compact[
        compact.index("SET status='succeeded'"):
        compact.index("except asyncpg.exceptions.UniqueViolationError")
    ]

    assert "SET status='succeeded'" in compact
    assert "AND lease_token=$2::uuid AND status IN ('pending', 'retrying')" in success_update
    assert "lease_expires_at >= clock_timestamp()" not in success_update
    assert compact.count(
        "AND lease_expires_at >= clock_timestamp() "
        "AND status IN ('pending', 'retrying') "
        "AND NOT superseded RETURNING id"
    ) == 3


def test_lifecycle_shutdown_tracks_workers_and_delivery_attempts_separately():
    repo_root = Path(__file__).resolve().parents[1]
    lifecycle_source = (repo_root / "mnemos" / "core" / "lifecycle.py").read_text()
    dispatcher_source = _webhook_module_source("dispatcher", "outbox", "workers")
    workers_source = _webhook_module_source("workers")
    recover_source = workers_source[
        workers_source.index("async def _recover_due_deliveries"):
        workers_source.index("async def _recoverable_delivery_ids")
    ]

    assert "_worker_tasks: set = set()" in lifecycle_source
    assert "_delivery_attempt_tasks: set = set()" in lifecycle_source
    assert "def _schedule_worker" in lifecycle_source
    assert "def _schedule_delivery_attempt" in lifecycle_source
    assert "WEBHOOK_SHUTDOWN_DRAIN_SECONDS" in lifecycle_source
    assert "await _cancel_tracked_tasks(\n        _worker_tasks" in lifecycle_source
    assert "await _drain_delivery_attempt_tasks()" in lifecycle_source
    assert "_schedule_delivery_attempt(_attempt_delivery(str(delivery_id)))" in dispatcher_source
    assert "_schedule_delivery_attempt" in recover_source
    assert "await _attempt_delivery" not in recover_source
    assert "may replay on restart" in lifecycle_source
    assert (
        lifecycle_source.index("await _cancel_tracked_tasks(\n        _worker_tasks")
        < lifecycle_source.index("await _drain_delivery_attempt_tasks()")
    )


def test_lifecycle_shutdown_during_finalize_drains_delivery_attempt(monkeypatch):
    async def run():
        from mnemos.core import lifecycle

        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [204])
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setattr(lifecycle, "WEBHOOK_SHUTDOWN_DRAIN_SECONDS", 1.0)
        finalize_started = asyncio.Event()
        finalize_can_commit = asyncio.Event()
        cancelled = []
        real_finalize = dispatcher._finalize_delivery

        async def _slow_finalize(pool, delivery, lease_token, result):
            finalize_started.set()
            try:
                await finalize_can_commit.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return await real_finalize(pool, delivery, lease_token, result)

        monkeypatch.setattr(dispatcher, "_finalize_delivery", _slow_finalize)

        task = lifecycle._schedule_delivery_attempt(dispatcher._attempt_delivery(row["id"]))
        await asyncio.wait_for(finalize_started.wait(), timeout=0.5)
        drain_task = asyncio.create_task(lifecycle._drain_delivery_attempt_tasks())
        await asyncio.sleep(0)

        assert not task.cancelled()
        assert http.delivery_ids == [row["id"]]
        assert row["status"] == "retrying"

        finalize_can_commit.set()
        await asyncio.wait_for(drain_task, timeout=0.5)

        assert cancelled == []
        assert task.done()
        assert row["status"] == "succeeded"
        assert await dispatcher._recoverable_delivery_ids(conn) == []

    asyncio.run(run())


def test_recovered_send_shutdown_drains_tracked_attempt(monkeypatch):
    async def run():
        from mnemos.core import lifecycle

        row = _attempt()
        dispatcher, conn, http = await _install(monkeypatch, [row], [204])
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setattr(lifecycle, "WEBHOOK_SHUTDOWN_DRAIN_SECONDS", 1.0)
        finalize_started = asyncio.Event()
        finalize_can_commit = asyncio.Event()
        cancelled = []
        real_finalize = dispatcher._finalize_delivery

        async def _slow_finalize(pool, delivery, lease_token, result):
            finalize_started.set()
            try:
                await finalize_can_commit.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return await real_finalize(pool, delivery, lease_token, result)

        monkeypatch.setattr(dispatcher, "_finalize_delivery", _slow_finalize)

        recovered = await dispatcher._recover_due_deliveries(conn.pool, limit=1)
        assert recovered == 1
        assert len(lifecycle._delivery_attempt_tasks) == 1

        await asyncio.wait_for(finalize_started.wait(), timeout=0.5)
        drain_task = asyncio.create_task(lifecycle._drain_delivery_attempt_tasks())
        await asyncio.sleep(0)

        assert http.delivery_ids == [row["id"]]
        assert row["status"] == "retrying"

        finalize_can_commit.set()
        await asyncio.wait_for(drain_task, timeout=0.5)

        assert cancelled == []
        assert row["status"] == "succeeded"
        assert await dispatcher._recoverable_delivery_ids(conn) == []

    asyncio.run(run())


def test_startup_repair_burst_then_periodic(monkeypatch):
    async def run():
        from mnemos.webhooks import dispatcher as dispatcher

        phases: list[str] = []
        intervals: list[float] = []
        now = 0.0

        class _FakeLoop:
            def time(self):
                return now

        async def _repair(pool, *, phase):
            phases.append(phase)

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
        monkeypatch.setattr(dispatcher.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(dispatcher.asyncio, "sleep", _sleep)

        try:
            await dispatcher.repair_worker_loop(object())
        except asyncio.CancelledError:
            pass

        assert phases == ["burst", "burst", "periodic"]
        # The split repair worker no longer wakes for delivery polling, so its
        # post-burst sleep follows the coarser repair-only periodic cadence.
        assert intervals == [5, 5, 300.0]

    asyncio.run(run())


def test_slow_delivery_loop_does_not_starve_repair_worker(monkeypatch):
    async def run():
        parent = _attempt()
        parent["status"] = "abandoned"
        parent["superseded"] = True
        successor = _successor_for(parent)
        dispatcher, conn, _http = await _install(monkeypatch, [parent, successor], [])

        loop = asyncio.get_running_loop()
        repair_ticks: list[float] = []
        delivery_started = asyncio.Event()
        slow_delivery_can_finish = asyncio.Event()
        actual_repair = dispatcher.repair_superseded_retrying_deliveries

        async def _repair(pool, *, phase):
            repair_ticks.append(loop.time())
            await actual_repair(pool)

        async def _recover(pool):
            delivery_started.set()
            await slow_delivery_can_finish.wait()
            return 0

        monkeypatch.setattr(dispatcher, "REPAIR_BURST_SECONDS", 1.0)
        monkeypatch.setattr(dispatcher, "REPAIR_BURST_INTERVAL", 0.02)
        monkeypatch.setattr(dispatcher, "REPAIR_PERIODIC_INTERVAL", 1.0)
        monkeypatch.setattr(dispatcher, "RECOVERY_POLL_INTERVAL", 1.0)
        monkeypatch.setattr(dispatcher, "_repair_superseded_retrying_deliveries_safely", _repair)
        monkeypatch.setattr(dispatcher, "_recover_due_deliveries", _recover)

        repair_task = asyncio.create_task(dispatcher.repair_worker_loop(conn.pool))
        delivery_task = asyncio.create_task(dispatcher.delivery_worker_loop(conn.pool))
        try:
            await asyncio.wait_for(delivery_started.wait(), timeout=1.0)

            first_repair_deadline = loop.time() + 1.0
            while len(repair_ticks) < 1 and loop.time() < first_repair_deadline:
                await asyncio.sleep(0.005)
            assert repair_ticks

            resurrected_at = loop.time()
            parent["status"] = "retrying"
            parent["status_updated_at"] = datetime.now(timezone.utc)
            parent["lease_token"] = None
            parent["lease_expires_at"] = None

            repair_deadline = loop.time() + 1.0
            while parent["status"] != "abandoned" and loop.time() < repair_deadline:
                await asyncio.sleep(0.005)

            assert parent["status"] == "abandoned"
            assert parent["superseded"] is True
            assert loop.time() - resurrected_at < 1.0
            assert not delivery_task.done()
            assert len(repair_ticks) >= 2
        finally:
            slow_delivery_can_finish.set()
            repair_task.cancel()
            delivery_task.cancel()
            await asyncio.gather(repair_task, delivery_task, return_exceptions=True)

    asyncio.run(run())


def test_retry_terminal_state_migration_repairs_existing_superseded_rows():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_retry_terminal_state.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "retry_scheduled" not in sql
    assert "SET status = 'abandoned'" in compact
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


def test_webhook_writer_revision_migration_adds_legacy_marker():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_writer_revision.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "ADD COLUMN IF NOT EXISTS writer_revision INTEGER DEFAULT 0" in compact
    assert "0/NULL means legacy or unknown" in sql
    assert "1 means current lease-aware writer" in sql


def test_webhook_status_updated_at_migration_adds_triggered_transition_clock():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_status_updated_at.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ" in compact
    assert "Live legacy in-flight rows are backfilled to clock_timestamp()" in sql
    assert "d.writer_revision IS DISTINCT FROM 1" in compact
    assert "d.lease_token IS NULL OR d.lease_expires_at IS NULL" in compact
    assert "THEN migration_clock.migrated_at" in compact
    assert "ELSE COALESCE(d.scheduled_at, migration_clock.migrated_at)" in compact
    assert "ALTER COLUMN status_updated_at SET DEFAULT clock_timestamp()" in compact
    assert "ALTER COLUMN status_updated_at SET NOT NULL" in compact
    assert "CREATE OR REPLACE FUNCTION webhook_deliveries_set_status_updated_at()" in compact
    assert "OLD.status IS DISTINCT FROM NEW.status" in compact
    assert "NEW.status_updated_at = clock_timestamp()" in compact
    assert "BEFORE UPDATE ON webhook_deliveries" in compact
    assert "EXECUTE FUNCTION webhook_deliveries_set_status_updated_at()" in compact


def test_webhook_superseded_marker_migration_adds_old_compatible_audit_marker():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_superseded_marker.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE" in compact
    assert "status='abandoned'" in sql
    assert "superseded=TRUE" in sql
    assert "SET status = 'abandoned', superseded = TRUE" in compact
    assert "WHERE status = 'retry_scheduled'" in compact
    assert "newer.attempt_num > d.attempt_num" in compact


def test_webhook_attempt_unique_migration_adds_live_chain_attempt_invariant():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_attempt_unique.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "PARTITION BY subscription_id, event_type, payload_hash, attempt_num" in compact
    assert "ORDER BY created DESC, id DESC" in compact
    assert "SET status = 'abandoned', superseded = TRUE" in compact
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt" in compact
    assert (
        "ON webhook_deliveries(subscription_id, event_type, payload_hash, attempt_num) "
        "WHERE status IN ('pending', 'retrying') AND NOT superseded"
    ) in compact


def test_webhook_succeeded_unique_migration_deduplicates_existing_succeeded_rows():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_succeeded_unique.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "PARTITION BY subscription_id, event_type, payload_hash" in compact
    assert "ORDER BY attempt_num ASC, created ASC, id ASC" in compact
    assert "SET status = 'abandoned', superseded = TRUE" in compact
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain" in compact
    assert (
        "ON webhook_deliveries(subscription_id, event_type, payload_hash) "
        "WHERE status = 'succeeded'"
    ) in compact

    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE webhook_deliveries (
            id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            attempt_num INTEGER NOT NULL,
            status TEXT NOT NULL,
            superseded BOOLEAN NOT NULL DEFAULT FALSE,
            lease_token TEXT,
            lease_expires_at TEXT,
            response_status INTEGER,
            response_body TEXT,
            error TEXT,
            delivered_at TEXT,
            created TEXT NOT NULL
        );
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload_hash, attempt_num,
             status, superseded, response_status, response_body, delivered_at, created)
        VALUES
            ('later', 'sub', 'memory.created', 'hash', 2,
             'succeeded', FALSE, 202, 'later body', '2026-04-27T12:01:00Z',
             '2026-04-27T12:01:00Z'),
            ('earliest', 'sub', 'memory.created', 'hash', 1,
             'succeeded', FALSE, 200, 'earliest body', '2026-04-27T12:00:00Z',
             '2026-04-27T12:00:00Z');
        """
    )

    db.executescript(sql)
    rows = {
        row[0]: row
        for row in db.execute(
            """
            SELECT id, status, superseded, response_status, response_body, delivered_at
            FROM webhook_deliveries
            """
        )
    }

    assert rows["earliest"] == (
        "earliest",
        "succeeded",
        0,
        200,
        "earliest body",
        "2026-04-27T12:00:00Z",
    )
    assert rows["later"] == (
        "later",
        "abandoned",
        1,
        202,
        "later body",
        "2026-04-27T12:01:00Z",
    )
    try:
        db.execute(
            """
            INSERT INTO webhook_deliveries
                (id, subscription_id, event_type, payload_hash, attempt_num,
                 status, created)
            VALUES
                ('duplicate', 'sub', 'memory.created', 'hash', 3,
                 'succeeded', '2026-04-27T12:02:00Z')
            """
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("succeeded-chain unique index did not reject duplicate success")


def test_succeeded_terminal_trigger_migration_blocks_status_revert():
    repo_root = Path(__file__).resolve().parents[1]
    sql = (
        repo_root / "db" / "migrations_v3_5_webhook_succeeded_terminal_trigger.sql"
    ).read_text()
    compact = " ".join(sql.split())

    assert "CREATE OR REPLACE FUNCTION webhook_deliveries_enforce_succeeded_terminal()" in compact
    assert "OLD.status = 'succeeded'" in compact
    assert "NEW.status IS DISTINCT FROM 'succeeded'" in compact
    assert "cannot transition status away from succeeded" in sql
    assert "USING ERRCODE = 'check_violation'" in compact
    assert "RETURN NEW" in compact
    assert "CREATE TRIGGER webhook_deliveries_succeeded_terminal" in compact
    assert "BEFORE UPDATE ON webhook_deliveries" in compact
    assert "EXECUTE FUNCTION webhook_deliveries_enforce_succeeded_terminal()" in compact


def test_stale_writer_cannot_revert_succeeded():
    class _FakeCheckViolationError(Exception):
        pass

    async def run():
        row = _attempt()
        row["status"] = "succeeded"
        store = _FakeWebhookStore([row])
        store.enforce_succeeded_terminal = True
        store.check_violation_cls = _FakeCheckViolationError
        conn = _FakeWebhookConn(store)

        try:
            await conn.execute(
                "UPDATE webhook_deliveries SET status='retrying' WHERE id=$1::uuid",
                row["id"],
            )
        except _FakeCheckViolationError as exc:
            assert "cannot transition status away from succeeded" in str(exc)
        else:
            raise AssertionError("succeeded terminal trigger did not reject stale writer")

        assert row["status"] == "succeeded"

    asyncio.run(run())


def test_succeeded_terminal_trigger_allows_response_body_audit_update():
    async def run():
        row = _attempt()
        row["status"] = "succeeded"
        store = _FakeWebhookStore([row])
        store.enforce_succeeded_terminal = True
        conn = _FakeWebhookConn(store)

        result = await conn.execute(
            "UPDATE webhook_deliveries SET response_body=$2 WHERE id=$1::uuid",
            row["id"],
            "late audit body",
        )

        assert result == "UPDATE 1"
        assert row["status"] == "succeeded"
        assert row["response_body"] == "late audit body"

    asyncio.run(run())


def test_succeeded_terminal_trigger_allows_lease_clear_update():
    async def run():
        row = _attempt()
        row["status"] = "succeeded"
        row["lease_token"] = "00000000-0000-0000-0000-000000000099"
        row["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)
        store = _FakeWebhookStore([row])
        store.enforce_succeeded_terminal = True
        conn = _FakeWebhookConn(store)

        result = await conn.fetchrow(
            "UPDATE webhook_deliveries SET lease_token=NULL, lease_expires_at=NULL "
            "WHERE id=$1::uuid AND lease_token=$2::uuid RETURNING id",
            row["id"],
            "00000000-0000-0000-0000-000000000099",
        )

        assert result == {
            "id": row["id"],
            "status": "succeeded",
            "superseded": False,
        }
        assert row["status"] == "succeeded"
        assert row["lease_token"] is None
        assert row["lease_expires_at"] is None

    asyncio.run(run())


def test_webhook_succeeded_terminal_trigger_migration_list_sync():
    repo_root = Path(__file__).resolve().parents[1]
    migration_name = "migrations_v3_5_webhook_succeeded_terminal_trigger.sql"

    for relative in (
        "mnemos/installer/db.py",
        "docker-compose.yml",
        "docker-compose.staging.yml",
    ):
        text = (repo_root / relative).read_text()
        assert migration_name in text, relative

    for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
        text = (repo_root / compose_name).read_text()
        assert (
            "./db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql:"
            "/docker-entrypoint-initdb.d/33-webhook-succeeded-terminal-trigger.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql:"
            "/migrations/33-webhook-succeeded-terminal-trigger.sql:ro"
        ) in text, compose_name
        assert "-f /migrations/33-webhook-succeeded-terminal-trigger.sql" in text, compose_name

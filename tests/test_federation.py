"""Federation subsystem tests — wiring, id convention, sync protocol.

Unit tests run without DB. Integration tests require MNEMOS_TEST_DB.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _feed_row(memory_id: str, updated: datetime) -> dict:
    return {
        "id": memory_id,
        "content": f"content {memory_id}",
        "category": "facts",
        "subcategory": None,
        "metadata": {"source": "test"},
        "quality_rating": 75,
        "verbatim_content": f"content {memory_id}",
        "owner_id": "owner-1",
        "namespace": "default",
        "permission_mode": 644,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "created": updated,
        "updated": updated,
    }


class _FeedConnection:
    def __init__(self, rows: list[dict]):
        self.rows = sorted(rows, key=lambda r: (r["updated"], r["id"]))
        self.queries: list[str] = []
        self.calls: list[tuple] = []

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        self.calls.append(args)
        limit = args[-1]
        rows = self.rows
        if "m.updated > $1" in query:
            since_updated, since_id = args[0], args[1]
            rows = [
                r for r in rows
                if r["updated"] > since_updated
                or (r["updated"] == since_updated and r["id"] > since_id)
            ]
        return rows[:limit]


class _FeedAcquire:
    def __init__(self, conn: _FeedConnection):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FeedPool:
    def __init__(self, rows: list[dict]):
        self.conn = _FeedConnection(rows)

    def acquire(self):
        return _FeedAcquire(self.conn)


# ── Module wiring ────────────────────────────────────────────────────────────


class TestFederationWiring:
    def test_federation_module_imports(self):
        from api import federation
        for name in (
            "sync_peer",
            "federation_worker_loop",
            "FEDERATION_ID_PREFIX",
            "FEDERATION_BATCH_LIMIT",
        ):
            assert hasattr(federation, name), f"api.federation missing: {name}"

    def test_federation_handler_router(self):
        from api.handlers import federation as handler
        assert hasattr(handler, "router")
        assert handler.router.prefix == "/v1/federation"

    def test_federation_models(self):
        from api.models import (
            FederationPeerCreateRequest,
        )
        req = FederationPeerCreateRequest(
            name="peer-alpha",
            base_url="https://alpha.example.com",
            auth_token="x" * 40,
        )
        assert req.sync_interval_secs == 300

    def test_router_registered_in_app(self):
        import api_server
        paths = {r.path for r in api_server.app.routes}
        fed_paths = [p for p in paths if p.startswith("/v1/federation")]
        # peers CRUD (5) + sync (1) + log (1) + status (1) + feed (1) = 9 at minimum
        assert len(fed_paths) >= 5, f"expected federation routes, got: {fed_paths}"

    def test_feed_route_exists(self):
        import api_server
        paths = {r.path for r in api_server.app.routes}
        assert "/v1/federation/feed" in paths


# ── Identifier convention ────────────────────────────────────────────────────


class TestFederationIdConvention:
    def test_prefix_constant(self):
        from api.federation import FEDERATION_ID_PREFIX
        assert FEDERATION_ID_PREFIX == "fed:"

    def test_local_id_format_example(self):
        # Docs promise: fed:{peer_name}:{remote_id}
        from api.federation import FEDERATION_ID_PREFIX
        peer = "alpha"
        remote_id = "mem_abc123"
        local = f"{FEDERATION_ID_PREFIX}{peer}:{remote_id}"
        assert local == "fed:alpha:mem_abc123"


# ── Feed cursor stability ───────────────────────────────────────────────────


class TestFederationFeedCursor:
    @pytest.mark.asyncio
    async def test_same_timestamp_tie_pages_without_losing_rows(self, monkeypatch):
        from api import federation as fed
        import api.lifecycle as lc
        from api.handlers import federation as handler

        updated = datetime(2026, 4, 27, 12, 0, 0)
        rows = [
            _feed_row("00000000-0000-0000-0000-000000000001", updated),
            _feed_row("00000000-0000-0000-0000-000000000002", updated),
            _feed_row("00000000-0000-0000-0000-000000000003", updated),
        ]
        pool = _FeedPool(rows)
        monkeypatch.setattr(lc, "_pool", pool)

        first = await handler.federation_feed(
            None, None, since=None, namespace=None, category=None, limit=2
        )
        assert [m.id for m in first.memories] == [rows[0]["id"], rows[1]["id"]]
        assert first.has_more is True
        first_cursor = fed._decode_feed_cursor(first.next_cursor)
        assert first_cursor.updated == updated.replace(tzinfo=timezone.utc)
        assert first_cursor.memory_id == rows[1]["id"]

        second = await handler.federation_feed(
            None, None, since=first.next_cursor, namespace=None, category=None, limit=2
        )
        assert [m.id for m in second.memories] == [rows[2]["id"]]
        assert second.has_more is False

    @pytest.mark.asyncio
    async def test_cross_timestamp_paging_advances_with_compound_cursor(self, monkeypatch):
        import api.lifecycle as lc
        from api.handlers import federation as handler

        t0 = datetime(2026, 4, 27, 12, 0, 0)
        t1 = t0 + timedelta(seconds=1)
        rows = [
            _feed_row("00000000-0000-0000-0000-000000000001", t0),
            _feed_row("00000000-0000-0000-0000-000000000002", t1),
            _feed_row("00000000-0000-0000-0000-000000000003", t1),
        ]
        pool = _FeedPool(rows)
        monkeypatch.setattr(lc, "_pool", pool)

        first = await handler.federation_feed(
            None, None, since=None, namespace=None, category=None, limit=2
        )
        second = await handler.federation_feed(
            None, None, since=first.next_cursor, namespace=None, category=None, limit=2
        )

        assert [m.id for m in first.memories + second.memories] == [r["id"] for r in rows]
        assert second.has_more is False

    @pytest.mark.asyncio
    async def test_empty_feed_keeps_cursor_unchanged(self, monkeypatch):
        from api import federation as fed
        import api.lifecycle as lc
        from api.handlers import federation as handler

        updated = datetime(2026, 4, 27, 12, 0, 0)
        cursor = fed._encode_feed_cursor(
            updated,
            "00000000-0000-0000-0000-000000000009",
        )
        pool = _FeedPool([
            _feed_row("00000000-0000-0000-0000-000000000001", updated),
        ])
        monkeypatch.setattr(lc, "_pool", pool)

        response = await handler.federation_feed(
            None, None, since=cursor, namespace=None, category=None, limit=10
        )

        assert response.memories == []
        assert response.next_cursor == cursor
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_malformed_cursor_returns_bad_request(self, monkeypatch):
        import api.lifecycle as lc
        from api.handlers import federation as handler
        from fastapi import HTTPException

        updated = datetime(2026, 4, 27, 12, 0, 0)
        rows = [
            _feed_row("aaa-low", updated),
            _feed_row("memabc", updated),
            _feed_row("zzz-high", updated),
        ]
        pool = _FeedPool(rows)
        monkeypatch.setattr(lc, "_pool", pool)

        with pytest.raises(HTTPException) as exc:
            await handler.federation_feed(
                None,
                None,
                since="2026-04-27T12:00:00Z",
                namespace=None,
                category=None,
                limit=10,
            )

        assert exc.value.status_code == 400
        assert exc.value.detail == "invalid federation cursor"
        assert pool.conn.calls == []

    @pytest.mark.asyncio
    async def test_empty_malformed_cursor_response_returns_bad_request(self, monkeypatch):
        import api.lifecycle as lc
        from api.handlers import federation as handler
        from fastapi import HTTPException

        updated = datetime(2026, 4, 27, 12, 0, 0)
        pool = _FeedPool([
            _feed_row("before-boundary", updated - timedelta(seconds=1)),
        ])
        monkeypatch.setattr(lc, "_pool", pool)

        with pytest.raises(HTTPException) as exc:
            await handler.federation_feed(
                None,
                None,
                since="2026-04-27T12:00:00Z",
                namespace=None,
                category=None,
                limit=10,
            )

        assert exc.value.status_code == 400
        assert exc.value.detail == "invalid federation cursor"
        assert pool.conn.calls == []

    @pytest.mark.asyncio
    async def test_feed_sql_uses_updated_id_tie_breaker(self, monkeypatch):
        from api import federation as fed
        import api.lifecycle as lc
        from api.handlers import federation as handler

        pool = _FeedPool([])
        monkeypatch.setattr(lc, "_pool", pool)
        cursor = fed._encode_feed_cursor(
            datetime(2026, 4, 27, 12, 0, 0),
            "memabc",
        )

        await handler.federation_feed(
            None,
            None,
            since=cursor,
            namespace=None,
            category=None,
            limit=10,
        )

        sql = " ".join(pool.conn.queries[-1].split())
        assert "(m.updated > $1 OR (m.updated = $1 AND m.id > $2))" in sql
        assert "ORDER BY m.updated ASC, m.id ASC" in sql


# ── Peer name validation (format checked at DB layer) ────────────────────────


class TestPeerNameFormat:
    """The DB CHECK constraint enforces format. Here we just document it."""

    def test_valid_peer_names(self):
        import re
        pat = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
        for name in ("alpha", "peer-alpha", "peer-1", "a1"):
            assert pat.match(name), f"expected {name} valid"

    def test_invalid_peer_names(self):
        import re
        pat = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
        for name in ("A", "peer_alpha", "peer.alpha", "-alpha", "alpha-", "x"):
            assert not pat.match(name), f"expected {name} invalid"


# ── Integration ──────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    "MNEMOS_TEST_DB" not in os.environ,
    reason="set MNEMOS_TEST_DB=postgres://... to run integration tests",
)
class TestFederationIntegration:
    @pytest.mark.asyncio
    async def test_peer_crud(self):
        import asyncpg
        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO federation_peers (name, base_url, auth_token)
                VALUES ($1, $2, $3)
                RETURNING id, name, enabled, total_pulled
                """,
                "peer-test", "https://test.example.invalid", "token",
            )
            assert row["name"] == "peer-test"
            assert row["enabled"] is True
            assert row["total_pulled"] == 0
            await conn.execute(
                "DELETE FROM federation_peers WHERE id = $1", row["id"]
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_memories_federation_source_column_exists(self):
        import asyncpg
        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            row = await conn.fetchrow(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'memories' AND column_name = 'federation_source'
                """
            )
            assert row is not None, "migration not applied"
        finally:
            await conn.close()

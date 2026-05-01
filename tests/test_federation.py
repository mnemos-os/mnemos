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

    async def fetchrow(self, query: str, *args):
        self.queries.append(query)
        self.calls.append(args)
        rows = self.rows
        if "m.id = $1" in query:
            rows = [r for r in rows if r["id"] == args[0]]
        next_arg = 1
        if "m.namespace = ANY" in query:
            namespaces = args[next_arg]
            next_arg += 1
            rows = [r for r in rows if r["namespace"] in namespaces]
        if "m.category = ANY" in query:
            categories = args[next_arg]
            rows = [r for r in rows if r["category"] in categories]
        return rows[0] if rows else None


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
        from mnemos.domain import federation
        for name in (
            "sync_peer",
            "federation_worker_loop",
            "FEDERATION_ID_PREFIX",
            "FEDERATION_BATCH_LIMIT",
        ):
            assert hasattr(federation, name), f"mnemos.domain.federation missing: {name}"

    def test_federation_handler_router(self):
        from mnemos.api.routes import federation as handler
        assert hasattr(handler, "router")
        assert handler.router.prefix == "/v1/federation"

    def test_federation_models(self):
        from mnemos.domain.models import (
            FederationPeerCreateRequest,
        )
        req = FederationPeerCreateRequest(
            name="peer-alpha",
            base_url="https://alpha.example.com",
            auth_token="x" * 40,
        )
        assert req.sync_interval_secs == 300

    def test_router_registered_in_app(self):
        import mnemos.api.main as api_server
        paths = {r.path for r in api_server.app.routes}
        fed_paths = [p for p in paths if p.startswith("/v1/federation")]
        # peers CRUD (5) + sync (1) + log (1) + status (1) + feed (1) = 9 at minimum
        assert len(fed_paths) >= 5, f"expected federation routes, got: {fed_paths}"

    def test_feed_route_exists(self):
        import mnemos.api.main as api_server
        paths = {r.path for r in api_server.app.routes}
        assert "/v1/federation/feed" in paths

    def test_memory_route_exists(self):
        import mnemos.api.main as api_server
        paths = {r.path for r in api_server.app.routes}
        assert "/v1/federation/memory/{memory_id}" in paths


# ── Identifier convention ────────────────────────────────────────────────────


class TestFederationIdConvention:
    def test_prefix_constant(self):
        from mnemos.domain.federation import FEDERATION_ID_PREFIX
        assert FEDERATION_ID_PREFIX == "fed:"

    def test_local_id_format_example(self):
        # Docs promise: fed:{peer_name}:{remote_id}
        from mnemos.domain.federation import FEDERATION_ID_PREFIX
        peer = "alpha"
        remote_id = "mem_abc123"
        local = f"{FEDERATION_ID_PREFIX}{peer}:{remote_id}"
        assert local == "fed:alpha:mem_abc123"


# ── Feed cursor stability ───────────────────────────────────────────────────


class TestFederationFeedCursor:
    @pytest.mark.asyncio
    async def test_same_timestamp_tie_pages_without_losing_rows(self, monkeypatch):
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler
        from mnemos.domain import federation as fed

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
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

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
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler
        from mnemos.domain import federation as fed

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
        from fastapi import HTTPException

        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

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
        from fastapi import HTTPException

        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

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
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler
        from mnemos.domain import federation as fed

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


class TestFederationMemoryEndpoint:
    @pytest.mark.asyncio
    async def test_returns_visible_memory_by_id(self, monkeypatch):
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        row = _feed_row("mem_visible", datetime(2026, 4, 27, 12, 0, 0))
        row["namespace"] = "shared"
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        response = await handler.federation_memory(
            "mem_visible",
            None,
            namespace="shared",
            category=None,
        )

        assert response.id == "mem_visible"
        assert response.content == "content mem_visible"
        sql = " ".join(pool.conn.queries[-1].split())
        assert "m.federation_source IS NULL" in sql
        assert "(m.permission_mode % 10) >= 4" in sql
        assert "m.id = $1" in sql
        assert "m.namespace = ANY($2)" in sql

    @pytest.mark.asyncio
    async def test_returns_404_when_memory_filtered_out(self, monkeypatch):
        from fastapi import HTTPException

        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        row = _feed_row("mem_hidden", datetime(2026, 4, 27, 12, 0, 0))
        row["namespace"] = "private"
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        with pytest.raises(HTTPException) as exc:
            await handler.federation_memory(
                "mem_hidden",
                None,
                namespace="shared",
                category=None,
            )

        assert exc.value.status_code == 404


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


class TestStoreMemoriesConcurrency:
    """v4.2.0a8 round-3: codex Finding 1 — concurrent INSERT race.

    Two federation consumers can deliver the same created event during
    the partial-fleet rollout window (legacy durable + queue-mode
    durable both alive). Pre-fix, both would pass the SELECT, both
    attempt INSERT, and the loser would raise UniqueViolationError
    out of _store_memories — leaving the message unacked and forcing
    JetStream to redeliver indefinitely.

    Post-fix, the loser catches UniqueViolationError, re-fetches
    existing, and falls through to the update-when-newer branch.
    """

    @pytest.mark.asyncio
    async def test_unique_violation_on_insert_falls_through_cleanly(self):
        import asyncpg

        from mnemos.domain.federation import _store_memories

        # Hand-rolled fake connection that simulates the race:
        #   - SELECT returns None (row not present at start of handler)
        #   - INSERT raises UniqueViolationError (other consumer won)
        #   - Re-fetch SELECT returns existing row with older
        #     federation_remote_updated
        #   - UPDATE succeeds (we're newer)
        executes: list[tuple] = []
        fetchrows: list[tuple] = []
        select_results = [
            None,  # initial check: no row
            {"federation_remote_updated": datetime(2026, 5, 1, tzinfo=timezone.utc)},  # post-conflict refetch
        ]

        class _FakeConn:
            async def fetchrow(self, sql, *args):
                fetchrows.append((sql, args))
                return select_results.pop(0)

            async def execute(self, sql, *args):
                executes.append((sql, args))
                if sql.strip().startswith("INSERT"):
                    raise asyncpg.UniqueViolationError(
                        "duplicate key value violates unique constraint memories_pkey"
                    )
                return "UPDATE 1"

        # Real feed rows ship ISO strings; _feed_row uses datetime
        # objects for the feed-pagination tests. Build a payload that
        # matches the on-the-wire format _store_memories actually parses.
        feed = [{
            "id": "mem_race",
            "content": "content mem_race",
            "category": "facts",
            "verbatim_content": "content mem_race",
            "namespace": "default",
            "metadata": {"source": "test"},
            "quality_rating": 75,
            "updated": "2026-05-01T01:00:00Z",
            "created": "2026-05-01T01:00:00Z",
        }]

        new_n, upd_n = await _store_memories(_FakeConn(), "pythia", feed)

        assert new_n == 0, "INSERT lost the race; no row counted as new"
        assert upd_n == 1, "post-conflict refetch + update-when-newer must apply"
        # First execute call was the INSERT that raised; second was the UPDATE.
        assert len(executes) == 2
        assert executes[0][0].strip().startswith("INSERT")
        assert executes[1][0].strip().startswith("UPDATE")
        assert len(fetchrows) == 2  # initial check + post-conflict refetch

    @pytest.mark.asyncio
    async def test_stale_update_loses_race_via_where_clause(self):
        """v4.2.0a8 round-4: codex Finding 4 — the UPDATE itself must
        be atomic against concurrent delivery.

        Scenario: two consumers see baseline T0 in their initial
        SELECT. Consumer A has remote_updated=T2 (newer), Consumer B
        has remote_updated=T1 (newer than T0 but older than T2).
        Both pass the Python-side freshness check on the snapshot
        they read. A commits first (row → T2). B commits second.
        Without a WHERE-clause guard B's UPDATE rolls the row back
        to T1.

        Post-fix the UPDATE includes
        ``AND (federation_remote_updated IS NULL OR
                federation_remote_updated < $9::timestamptz)``
        which means B's UPDATE matches 0 rows once A has committed.
        upd_n is NOT incremented.
        """
        from mnemos.domain.federation import _store_memories

        execute_calls: list[tuple] = []

        class _FakeConn:
            async def fetchrow(self, sql, *args):
                # Existing row: T0 baseline (older than both A and B).
                return {
                    "federation_remote_updated": datetime(
                        2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc
                    )
                }

            async def execute(self, sql, *args):
                execute_calls.append((sql, args))
                # Simulate that consumer A committed first: B's UPDATE
                # WHERE clause matches 0 rows now.
                return "UPDATE 0"

        # Consumer B with the OLDER remote_updated (T1).
        feed = [{
            "id": "mem_race",
            "content": "stale content",
            "category": "facts",
            "verbatim_content": "stale content",
            "namespace": "default",
            "metadata": {"source": "B"},
            "quality_rating": 75,
            "updated": "2026-05-01T01:00:00Z",  # T1
            "created": "2026-05-01T01:00:00Z",
        }]

        new_n, upd_n = await _store_memories(_FakeConn(), "pythia", feed)

        assert new_n == 0
        assert upd_n == 0, (
            "stale UPDATE must NOT be counted: WHERE-clause filter "
            "fired (UPDATE 0) because the concurrent newer event "
            "already committed; counting upd_n=1 would falsely claim "
            "we applied a delta we did not"
        )
        # Verify the WHERE clause is in the SQL — defense against
        # someone refactoring the guard out without realizing this
        # invariant.
        assert len(execute_calls) == 1
        update_sql = execute_calls[0][0]
        assert "federation_remote_updated < $9::timestamptz" in update_sql, (
            "the UPDATE must carry a freshness WHERE-clause guard"
        )


class TestFederationFeedPreferCompressed:
    """v4.2.0a14 round-2: codex round-10 finding — prefer_compressed
    must REPLACE the raw payload, not add to it. Pre-round-2 the
    branch only appended ``compressed_content`` as an extra field
    while still selecting raw m.content + m.verbatim_content, so an
    opt-in peer received BOTH (wire bytes UP, not down).

    These tests pin the post-fix shape:
      * prefer_compressed=true + variant present → content carries
        the compressed text; compressed_content non-None;
        verbatim_content NULL.
      * prefer_compressed=true + variant absent → content is raw;
        compressed_content None; verbatim_content present.
      * prefer_compressed=false (default) → unchanged behavior.
    """

    @pytest.mark.asyncio
    async def test_compressed_branch_replaces_raw_content(self, monkeypatch):
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        updated = datetime(2026, 5, 1, 0, 0, 0)
        # Simulate the LEFT JOIN result: a row that has a compressed
        # variant. The handler's SQL uses COALESCE so the row's
        # ``content`` comes back as the compressed text + the
        # separate compressed_content field also populated.
        row = _feed_row("00000000-0000-0000-0000-000000000010", updated)
        row["content"] = "<<COMPRESSED PAYLOAD>>"  # COALESCE result
        row["compressed_content"] = "<<COMPRESSED PAYLOAD>>"
        row["verbatim_content"] = None  # CASE WHEN NULLed it
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        resp = await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None,
            limit=10,
            prefer_compressed=True,
        )

        # SQL shape verification: handler must have built a
        # CASE-WHEN byte-gate query against the LEFT JOIN. Pre-
        # round-2 the gate was a plain COALESCE; codex round-11
        # tightened it to a per-row octet_length comparison so
        # the prefer_compressed path can never produce LARGER
        # payloads than the legacy raw path.
        seen = pool.conn.queries[-1]
        assert "LEFT JOIN memory_compressed_variants" in seen
        assert "octet_length(v.compressed_content) < octet_length(m.content)" in seen, (
            "prefer_compressed branch must gate variant use on a "
            f"byte comparison; got SQL: {seen[:300]}"
        )
        # verbatim_content gets NULLed only inside the "use variant" branch
        assert (
            "octet_length(v.compressed_content) < octet_length(m.content) THEN NULL"
            in seen
        ), "verbatim_content must be NULLed when (and only when) the variant is selected"

        # Wire shape verification: the MemoryItem should carry the
        # compressed payload as its content + compressed_content
        # populated + verbatim_content None.
        assert len(resp.memories) == 1
        m = resp.memories[0]
        assert m.content == "<<COMPRESSED PAYLOAD>>"
        assert m.compressed_content == "<<COMPRESSED PAYLOAD>>"
        assert m.verbatim_content is None

    @pytest.mark.asyncio
    async def test_compressed_branch_falls_through_when_no_variant(self, monkeypatch):
        """When prefer_compressed=true but the LEFT JOIN finds no
        variant for a row, the COALESCE picks raw m.content and
        compressed_content stays None. Operators get the fallback
        behavior they'd see with prefer_compressed=false."""
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        updated = datetime(2026, 5, 1, 0, 0, 0)
        row = _feed_row("00000000-0000-0000-0000-000000000011", updated)
        # COALESCE picks raw, NULL on the v.* side
        row["compressed_content"] = None
        # verbatim_content stays present because CASE picks the raw
        # value when v.compressed_content IS NULL.
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        resp = await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None,
            limit=10,
            prefer_compressed=True,
        )

        m = resp.memories[0]
        assert m.content == row["content"]  # raw
        assert m.compressed_content is None
        assert m.verbatim_content == row["verbatim_content"]

    @pytest.mark.asyncio
    async def test_byte_gate_predicate_in_sql(self, monkeypatch):
        """Codex round-11 audit: the prefer_compressed branch must
        guarantee wire bytes never go UP. The SQL CASE predicate
        only swaps to the variant when octet_length(variant) <
        octet_length(raw). Pin that the predicate is in the SQL so
        a future refactor can't drop the gate without tripping this
        test.
        """
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        updated = datetime(2026, 5, 1, 0, 0, 0)
        row = _feed_row("00000000-0000-0000-0000-000000000020", updated)
        row["compressed_content"] = "<<COMPRESSED>>"
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None,
            limit=10,
            prefer_compressed=True,
        )

        seen = pool.conn.queries[-1]
        assert "octet_length(v.compressed_content) < octet_length(m.content)" in seen, (
            "prefer_compressed must gate the variant on a byte-comparison "
            "so it can NEVER make payloads larger; got SQL:\n"
            f"{seen[:600]}"
        )
        # And the predicate must be applied to all three fields
        # (content / compressed_content / verbatim_content) so a
        # variant that fails the gate falls through consistently
        # across the row.
        assert seen.count("v.compressed_content IS NOT NULL") >= 3, (
            "byte gate must wrap content + compressed_content + "
            "verbatim_content so the row stays consistent when the "
            "variant is rejected"
        )

    @pytest.mark.asyncio
    async def test_default_off_preserves_legacy_shape(self, monkeypatch):
        """prefer_compressed=False (default) must produce identical
        SQL + MemoryItem shape to v4.2.0a13 — no LEFT JOIN, no
        COALESCE, raw content + verbatim_content + compressed_content
        as None."""
        import mnemos.core.lifecycle as lc
        from mnemos.api.routes import federation as handler

        updated = datetime(2026, 5, 1, 0, 0, 0)
        row = _feed_row("00000000-0000-0000-0000-000000000012", updated)
        pool = _FeedPool([row])
        monkeypatch.setattr(lc, "_pool", pool)

        # Explicit False — the function signature uses
        # ``prefer_compressed: bool = Query(False, ...)`` and a
        # direct call without the kwarg passes the Query MARKER
        # object (truthy), not False. Other test sites in this
        # module hit the same convention by passing all params
        # explicitly.
        resp = await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None,
            limit=10,
            prefer_compressed=False,
        )

        seen = pool.conn.queries[-1]
        assert "LEFT JOIN memory_compressed_variants" not in seen, (
            "default branch must NOT join memory_compressed_variants"
        )
        assert "COALESCE(v.compressed_content" not in seen
        m = resp.memories[0]
        assert m.compressed_content is None

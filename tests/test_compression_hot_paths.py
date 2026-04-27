"""Compression artifacts in hot retrieval paths.

Codex memory-OS audit 019dbd11 flagged: "compression remains mostly
out of the runtime retrieval path; both search and rehydrate
explicitly defer real compression." This commit wires the v3.1
contest winner into:
  - /v1/chat/completions context injection (_search_mnemos_context)
  - /v1/memories/rehydrate

Both now LEFT JOIN memory_compressed_variants and prefer the winner's
compressed_content over raw content.

Tests verify:
  - gateway SQL JOINs the variants table and COALESCEs
  - rehydrate SQL JOINs the variants table and flags variant_used
  - rehydrate response reports compression_applied=True and a
    compression_ratio < 1.0 when variant content is present
  - rehydrate response reports compression_applied=False when
    every row returned only raw content
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

from api.auth import UserContext


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self._rows


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


# ─── gateway _search_mnemos_context ─────────────────────────────────────────


def test_gateway_joins_variants_table(monkeypatch):
    from api.handlers import openai_compat

    conn = _Conn()
    _install(monkeypatch, conn)
    asyncio.run(openai_compat._search_mnemos_context("hello", _alice()))

    sql = conn.fetches[-1][0]
    assert "LEFT JOIN memory_compressed_variants" in sql
    assert "COALESCE(v.compressed_content" in sql
    assert "m.content" in sql  # fallback tier in the COALESCE chain


def test_gateway_uses_variant_content_when_available(monkeypatch):
    from api.handlers import openai_compat

    # The SQL COALESCEs (v.compressed_content, m.content) into a
    # column aliased as `content`. The mock returns what the
    # SQL WOULD produce for two rows — one with variant, one without.
    conn = _Conn(rows=[
        {"id": "mem_1", "category": "solutions",
         "content": "short variant form"},  # imagine COALESCE picked variant
        {"id": "mem_2", "category": "patterns",
         "content": "very long raw content " * 40},
    ])
    _install(monkeypatch, conn)

    out = asyncio.run(openai_compat._search_mnemos_context("query", _alice(), limit=5))
    assert len(out) == 2
    # Handler passes through whatever the SQL returned under `content`.
    # The compression choice happens in the SQL COALESCE, which the
    # JOIN test above validates.
    assert out[0]["content"] == "short variant form"


# ─── rehydrate ──────────────────────────────────────────────────────────────


def test_rehydrate_joins_variants_and_tracks_variant_used(monkeypatch):
    """SQL must LEFT JOIN memory_compressed_variants and expose
    `variant_used` so the handler can report compression_applied."""
    from api.handlers import memories
    from api.models import RehydrationRequest

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="hello", limit=10)
    asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert conn.fetches, "expected a fetch for rehydrate"
    sql = conn.fetches[-1][0]
    assert "LEFT JOIN memory_compressed_variants" in sql
    assert "variant_used" in sql
    assert "v.compressed_content AS compressed_content" in sql


def test_rehydrate_compression_applied_true_when_variant_present(monkeypatch):
    """If any row comes back with variant_used=True, the response
    reports compression_applied=True and a compression_ratio<1.0."""
    from api.handlers import memories
    from api.models import RehydrationRequest

    conn = _Conn(rows=[
        {
            "id": "mem_1", "category": "solutions",
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "quality_rating": 80,
            "raw_content": "original content that is quite long " * 5,
            "compressed_content": "compressed short",  # shorter than raw
            "variant_used": True,
            "rank": 0.9,
        },
    ])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="q", limit=10)
    resp = asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert resp.compression_applied is True
    assert resp.compression_ratio < 1.0
    assert resp.memories_included == 1


def test_rehydrate_compression_applied_false_when_no_variants(monkeypatch):
    """No variant rows -> compression_applied=False, ratio=1.0."""
    from api.handlers import memories
    from api.models import RehydrationRequest

    conn = _Conn(rows=[
        {
            "id": "mem_1", "category": "general",
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "quality_rating": 70,
            "raw_content": "raw unchanged content",
            "compressed_content": None,
            "variant_used": False,
            "rank": 0.5,
        },
    ])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="q", limit=10)
    resp = asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert resp.compression_applied is False
    assert resp.compression_ratio == 1.0

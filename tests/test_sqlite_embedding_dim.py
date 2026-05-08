"""Regression coverage for configurable SQLite embedding dim.

The 768→512 hard-coded vec0 column dimension was a blocker for the Cix
Sky1 NPU substrate (bge-small-zh-v1.5 produces 512-dim vectors). The
fix makes the dim configurable via MNEMOS_EMBEDDING_DIM and a fallback
of 768 (nomic-embed-text default) when no settings are wired.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from mnemos.persistence.sqlite import SqliteBackend


def _make_settings(dim):
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=dim))


@pytest.mark.parametrize("dim", [512, 768, 1024, 1536, 3072])
def test_resolve_embedding_dim_passes_through_supported_dims(tmp_path, dim):
    backend = SqliteBackend(tmp_path / "x.sqlite3", _make_settings(dim))
    assert backend._resolve_embedding_dim() == dim


def test_resolve_embedding_dim_defaults_to_768_when_settings_none(tmp_path):
    backend = SqliteBackend(tmp_path / "x.sqlite3", None)  # type: ignore[arg-type]
    assert backend._resolve_embedding_dim() == 768


def test_resolve_embedding_dim_defaults_to_768_when_settings_lacks_database(tmp_path):
    settings = SimpleNamespace()  # no .database attribute
    backend = SqliteBackend(tmp_path / "x.sqlite3", settings)
    assert backend._resolve_embedding_dim() == 768


def test_resolve_embedding_dim_defaults_when_database_lacks_field(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace())  # no .embedding_dim
    backend = SqliteBackend(tmp_path / "x.sqlite3", settings)
    assert backend._resolve_embedding_dim() == 768


@pytest.mark.parametrize("bad_value", [0, -1, 8193, 16385, 99999, "768", 768.0, None])
def test_resolve_embedding_dim_rejects_out_of_range_or_wrong_type(tmp_path, bad_value, caplog):
    backend = SqliteBackend(tmp_path / "x.sqlite3", _make_settings(bad_value))
    with caplog.at_level(logging.WARNING):
        result = backend._resolve_embedding_dim()
    assert result == 768
    assert any("MNEMOS_EMBEDDING_DIM" in r.message for r in caplog.records)


def test_resolve_embedding_dim_accepts_8192_ceiling(tmp_path):
    """8192 is the sqlite-vec SQLITE_VEC_VEC0_MAX_DIMENSIONS upstream cap."""
    backend = SqliteBackend(tmp_path / "x.sqlite3", _make_settings(8192))
    assert backend._resolve_embedding_dim() == 8192


@pytest.mark.asyncio
async def test_existing_vec_table_dim_returns_none_when_no_table(tmp_path):
    backend = SqliteBackend(tmp_path / "x.sqlite3", _make_settings(512))
    await backend.open()
    try:
        # On a fresh DB, the virtual table does not exist yet at the moment
        # we check sqlite_master before it's created. After open() the table
        # may or may not exist depending on whether sqlite-vec loaded.
        async with backend.transactional() as tx:
            existing = await backend._existing_vec_table_dim(tx.conn)
        # Either None (no table — sqlite-vec not loaded) or 512 (the resolved
        # dim — sqlite-vec loaded and CREATE succeeded). Both are valid.
        assert existing in (None, 512)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_existing_vec_table_dim_parses_ddl_correctly(tmp_path):
    """Manually create a memory_embedding_vec-shaped table and verify dim parse.

    SQLite strips trailing line comments from sqlite_master.sql, so the
    float[N] marker has to live in actual DDL syntax. A DEFAULT value is
    preserved verbatim, which is enough to exercise the regex parser
    without depending on sqlite-vec being loadable.
    """
    import aiosqlite

    db_path = tmp_path / "manual.sqlite3"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE memory_embedding_vec (id INT, marker TEXT DEFAULT 'float[1024]')"
        )
        await conn.commit()

    backend = SqliteBackend(db_path, _make_settings(1024))
    await backend.open()
    try:
        async with backend.transactional() as tx:
            existing = await backend._existing_vec_table_dim(tx.conn)
        assert existing == 1024
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_vec_virtual_table_uses_resolved_dim(tmp_path):
    """Smoke-test that the resolved dim flows into the CREATE VIRTUAL TABLE DDL.

    sqlite-vec may or may not be loadable in the test environment; we only
    verify the DDL is reached without exception, not that the vec0 module
    is actually present.
    """
    backend = SqliteBackend(tmp_path / "x.sqlite3", _make_settings(512))
    await backend.open()
    try:
        assert backend._resolve_embedding_dim() == 512
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_vec0_dim_mismatch_raises_with_migration_instructions(tmp_path):
    """If memory_embedding_vec exists at dim X and settings want Y, raise loudly.

    Silent disable + fallback would let new-dim queries score against old-dim
    rows in memory_embeddings (cosine garbage). The right behavior is to
    refuse to start so the operator runs the documented migration.
    """
    import aiosqlite

    db_path = tmp_path / "mismatch.sqlite3"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE memory_embedding_vec (id INT, marker TEXT DEFAULT 'float[768]')"
        )
        await conn.commit()

    backend = SqliteBackend(db_path, _make_settings(512))
    with pytest.raises(RuntimeError) as exc_info:
        await backend.open()
    msg = str(exc_info.value)
    assert "vec0 dimension mismatch" in msg
    assert "768" in msg and "512" in msg
    assert "DROP TABLE memory_embedding_vec" in msg
    assert "re-embed" in msg
    await backend.close()


@pytest.mark.asyncio
async def test_fallback_dim_mismatch_raises_with_migration_instructions(tmp_path):
    """If memory_embeddings has rows at dim X and settings want Y, raise loudly.

    This guards the no-sqlite-vec deployment path where vec0 isn't available
    and search uses the fallback shadow table. Stale-dim rows + new-dim
    queries = garbage cosine scores; refuse to start.
    """
    import aiosqlite

    db_path = tmp_path / "fb_mismatch.sqlite3"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE memory_embeddings ("
            "memory_id TEXT PRIMARY KEY, "
            "embedding TEXT NOT NULL, "
            "updated_at TEXT)"
        )
        # Stash a 5-element JSON array (dim=5 to keep the fixture small)
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('mem1', '[0.1, 0.2, 0.3, 0.4, 0.5]', '2026-05-06')"
        )
        await conn.commit()

    backend = SqliteBackend(db_path, _make_settings(512))
    with pytest.raises(RuntimeError) as exc_info:
        await backend.open()
    msg = str(exc_info.value)
    assert "fallback embedding dimension mismatch" in msg
    assert "5" in msg and "512" in msg
    assert "DELETE FROM memory_embeddings" in msg
    await backend.close()


@pytest.mark.asyncio
async def test_fallback_mixed_dim_rows_fail_closed(tmp_path):
    """Mixed-dim corruption: some rows match configured dim, some don't.

    Round-3 codex finding: a single-row sample could miss this. The fix scans
    all rows; even if 99 rows match and 1 doesn't, startup must fail.
    """
    import aiosqlite

    db_path = tmp_path / "mixed_fb.sqlite3"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE memory_embeddings ("
            "memory_id TEXT PRIMARY KEY, "
            "embedding TEXT NOT NULL, "
            "updated_at TEXT)"
        )
        # 2 rows at the configured dim (3) — small fixture for fast test.
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('match1', '[0.1, 0.2, 0.3]', '2026-05-06')"
        )
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('match2', '[0.4, 0.5, 0.6]', '2026-05-06')"
        )
        # 1 stale row at dim=5.
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('stale1', '[0.7, 0.8, 0.9, 1.0, 1.1]', '2026-05-05')"
        )
        await conn.commit()

    backend = SqliteBackend(db_path, _make_settings(3))
    with pytest.raises(RuntimeError) as exc_info:
        await backend.open()
    msg = str(exc_info.value)
    assert "fallback embedding dimension mismatch" in msg
    # Histogram surfaces the bad dim + count, not just one sample.
    assert "dim=5 x1" in msg
    assert "DELETE FROM memory_embeddings" in msg
    await backend.close()


@pytest.mark.asyncio
async def test_scan_fallback_dims_returns_histogram(tmp_path):
    """The scan helper returns {dim: count} so the guard can describe shape."""
    import aiosqlite

    db_path = tmp_path / "histogram.sqlite3"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE memory_embeddings ("
            "memory_id TEXT PRIMARY KEY, "
            "embedding TEXT NOT NULL, "
            "updated_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('a', '[0.1, 0.2]', '2026-05-06')"
        )
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('b', '[0.3, 0.4]', '2026-05-06')"
        )
        await conn.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) "
            "VALUES ('c', '[0.5, 0.6, 0.7]', '2026-05-06')"
        )
        await conn.commit()

    # Use a backend pointed at the prepared DB but don't trip the guard yet —
    # call _scan_fallback_embedding_dims directly via a fresh connection.
    backend = SqliteBackend(db_path, _make_settings(2))
    # Open in a way that bypasses the guard for this isolated helper test —
    # make a side connection.
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = lambda c, r: {col[0]: r[i] for i, col in enumerate(c.description)}
        histogram = await backend._scan_fallback_embedding_dims(conn)
    assert histogram == {2: 2, 3: 1}


@pytest.mark.asyncio
async def test_no_dim_mismatch_when_table_empty_or_absent(tmp_path):
    """Fresh DB or empty fallback table should not raise; should open cleanly."""
    backend = SqliteBackend(tmp_path / "fresh.sqlite3", _make_settings(512))
    # Fresh path — no tables exist yet at the moment _create_vec_virtual_table
    # runs. After open() the migration creates them at the requested dim
    # (no rows in memory_embeddings, so the fallback check returns None).
    await backend.open()
    try:
        assert backend._resolve_embedding_dim() == 512
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_runtime_upsert_rejects_wrong_dim_vector(tmp_path):
    """upsert_memory_embedding must fail loudly on wrong-dim vectors at runtime."""
    backend = SqliteBackend(tmp_path / "rt.sqlite3", _make_settings(512))
    await backend.open()
    try:
        # Repository got the expected dim wired by open().
        assert backend._memories._expected_embedding_dim == 512
        async with backend.transactional() as tx:
            with pytest.raises(ValueError) as exc_info:
                await backend._memories.upsert_memory_embedding(
                    tx, "mem_test", [0.1] * 768  # wrong dim — 768 vs configured 512
                )
            msg = str(exc_info.value)
            assert "embedding dim mismatch" in msg
            assert "768" in msg and "512" in msg
            assert "upsert_memory_embedding" in msg
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_runtime_search_rejects_wrong_dim_vector(tmp_path):
    """semantic_search must fail loudly on wrong-dim vectors at runtime.

    The dim check fires before SQL is built so the VisibilityFilter shape
    is irrelevant; we use the simplest constructable form.
    """
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    backend = SqliteBackend(tmp_path / "rts.sqlite3", _make_settings(512))
    await backend.open()
    try:
        async with backend.transactional() as tx:
            with pytest.raises(ValueError) as exc_info:
                await backend._memories.semantic_search(
                    tx,
                    embedding=[0.1] * 1024,  # wrong dim — 1024 vs configured 512
                    limit=10,
                    visibility=VisibilityFilter(
                        scope=VisibilityScope.ROOT_BYPASS,
                        user_id=None,
                        group_ids=(),
                        namespace="default",
                    ),
                )
            msg = str(exc_info.value)
            assert "embedding dim mismatch" in msg
            assert "1024" in msg and "512" in msg
            assert "semantic_search" in msg
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_runtime_dim_check_disabled_when_repo_unwired(tmp_path):
    """Bypassing the backend init (constructing repo directly) skips the check.

    Documents the explicit None-disables behavior so test code that uses the
    repository in isolation doesn't have to pass dummy embeddings.
    """
    from mnemos.persistence.sqlite import SqliteMemoryRepository

    repo = SqliteMemoryRepository()
    assert repo._expected_embedding_dim is None
    # No exception even with weird-shape input.
    repo._require_dim([0.1] * 99, "test_op")
    repo._require_dim([], "test_empty")

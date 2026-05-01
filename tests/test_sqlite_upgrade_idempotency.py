"""Regression coverage for SQLite upgrade migration idempotency.

Round-2 codex review of v4.2.0a6 flagged that ``ALTER TABLE ADD
COLUMN reject_reason`` is non-idempotent: re-running the migration
raises ``OperationalError: duplicate column name`` which the loader
previously surfaced. The fix tolerates that specific error so
upgrade-from-old-schema and fresh-install paths both work.
"""
from __future__ import annotations


import pytest

from mnemos.persistence.sqlite import SqliteBackend


@pytest.mark.asyncio
async def test_fresh_install_has_reject_reason_column(tmp_path):
    # Fresh install — the column lands via the CREATE TABLE in
    # migrations.sql. (Old-database upgrade is covered by the
    # double-apply test below: the duplicate-column-error tolerance
    # in the loader is what makes upgrades idempotent.)
    db_path = tmp_path / "fresh.sqlite3"
    backend = SqliteBackend(db_path, None)  # type: ignore[arg-type]
    await backend.open()
    try:
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _fetch_all
            cols = await _fetch_all(
                tx.conn,
                "PRAGMA table_info(memory_compression_candidates)",
                (),
            )
            col_names = {r["name"] for r in cols}
            assert "reject_reason" in col_names
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_double_apply_migrations_is_idempotent(tmp_path):
    # Open + close + open: migrations run twice on the same file.
    # The second pass must not raise ``duplicate column name``.
    db_path = tmp_path / "double-apply.sqlite3"
    b1 = SqliteBackend(db_path, None)  # type: ignore[arg-type]
    await b1.open()
    await b1.close()
    b2 = SqliteBackend(db_path, None)  # type: ignore[arg-type]
    await b2.open()  # would have raised on the duplicate-column path
    await b2.close()

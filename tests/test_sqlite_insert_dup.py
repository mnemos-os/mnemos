from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.persistence.base import DuplicateMemoryError
from mnemos.persistence.sqlite import SqliteBackend


pytestmark = pytest.mark.asyncio


async def test_sqlite_insert_memory_duplicate_id_raises(tmp_path):
    backend = SqliteBackend(tmp_path / "dup.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            kwargs = dict(
                memory_id="mem_dup",
                content="one",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id="owner",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="one",
                created=None,
                updated=None,
            )
            await backend.memories.insert_memory(tx, **kwargs)
            with pytest.raises(DuplicateMemoryError):
                await backend.memories.insert_memory(tx, **kwargs)
    finally:
        await backend.close()

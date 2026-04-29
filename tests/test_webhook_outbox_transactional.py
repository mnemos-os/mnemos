"""Memory create and webhook outbox enqueue share one transaction.

The v4.0 outbox claim: a memory.created webhook delivery row is
inserted in the same database transaction as the memory itself —
neither commits without the other. Slice 1d migrated the create
handler to dispatch through ``backend.memories.insert_memory`` +
``backend.webhooks.dispatch_event`` inside one
``backend.transactional()`` block, and HTTP delivery scheduling
fires only after the transaction commits.

These tests replace the legacy asyncpg-shaped conn mocks with a
fake backend that tracks commits / rollbacks. The atomicity
contract is asserted at the backend boundary: when dispatch_event
raises, the create handler propagates a 500 AND the
``transactional()`` context counted a rollback rather than a commit.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories
from mnemos.domain.models import MemoryCreateRequest

from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _memory_row(memory_id: str = "mem_test") -> dict:
    return {
        "id": memory_id,
        "content": "remember this",
        "category": "facts",
        "subcategory": None,
        "metadata": {},
        "quality_rating": 75,
        "verbatim_content": "remember this",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "compressed_content": None,
        "created": "2026-04-29T12:00:00",
        "updated": "2026-04-29T12:00:00",
    }


async def test_memory_create_commits_memory_and_webhook_delivery(monkeypatch):
    """Successful create: insert_memory + dispatch_event both fire in
    the same transactional() block, the txn commits, and HTTP delivery
    is scheduled for the returned delivery_ids."""
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", _memory_row())
    backend.webhooks.configure_delivery_ids(["delivery_1"])

    scheduled: list[str] = []
    monkeypatch.setattr(
        memories,
        "_schedule_outbox_deliveries",
        lambda ids: scheduled.extend(ids),
    )

    response = await memories.create_memory(
        MemoryCreateRequest(content="remember this", category="facts"),
        user=_user(),
    )

    assert response.id == "mem_test"
    insert_calls = [c for c in backend.memories.calls if c[0] == "insert_memory"]
    assert len(insert_calls) == 1
    dispatch_calls = backend.webhooks.calls
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][1]["event_type"] == "memory.created"
    assert backend.commits == 1
    assert backend.rollbacks == 0
    # Delivery scheduling fires AFTER the commit, so the captured ids
    # equal what dispatch_event returned.
    assert scheduled == ["delivery_1"]


async def test_webhook_delivery_failure_rolls_back_memory_insert(monkeypatch):
    """Failure in dispatch_event tears down the transaction. The
    backend records a rollback (not a commit), and the handler
    surfaces 500 — preserving the v4.0 outbox-atomicity contract."""
    backend = install_fake_backend(monkeypatch)
    backend.webhooks.configure_raise(RuntimeError("delivery insert failed"))

    scheduled: list[str] = []
    monkeypatch.setattr(
        memories,
        "_schedule_outbox_deliveries",
        lambda ids: scheduled.extend(ids),
    )

    with pytest.raises(HTTPException) as exc:
        await memories.create_memory(
            MemoryCreateRequest(content="remember this", category="facts"),
            user=_user(),
        )

    assert exc.value.status_code == 500
    # insert_memory still ran (it's in the same tx that rolled back) —
    # what matters is the txn rolled back rather than committed.
    assert backend.commits == 0
    assert backend.rollbacks == 1
    # Nothing scheduled when the txn rolled back.
    assert scheduled == []

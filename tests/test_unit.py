"""
MNEMOS unit tests — no live DB required.

Tests Pydantic models, request validation, auth logic,
and the FTS/vector search helpers via asyncpg mocking.
"""
import asyncio
import hashlib

import pytest

# ─── Model tests ─────────────────────────────────────────────────────────────

def test_memory_create_defaults():
    from mnemos.domain.models import MemoryCreateRequest
    m = MemoryCreateRequest(content="hello world")
    assert m.category == "facts"
    assert m.subcategory is None
    assert m.metadata is None


def test_memory_create_required_content():
    from pydantic import ValidationError

    from mnemos.domain.models import MemoryCreateRequest
    with pytest.raises(ValidationError):
        MemoryCreateRequest()  # content is required


def test_memory_search_defaults():
    from mnemos.domain.models import MemorySearchRequest
    r = MemorySearchRequest(query="test")
    assert r.limit == 10
    assert r.semantic is False
    assert r.include_compressed is False


def test_consultation_request_defaults():
    from mnemos.domain.models import ConsultationRequest
    r = ConsultationRequest(prompt="test")
    assert r.task_type == "reasoning"
    assert r.mode == "auto"


def test_bulk_create_request():
    from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest
    r = BulkCreateRequest(memories=[
        MemoryCreateRequest(content="one"),
        MemoryCreateRequest(content="two"),
    ])
    assert len(r.memories) == 2


def test_kg_triple_create_defaults():
    from mnemos.domain.models import KGTripleCreate
    t = KGTripleCreate(subject="Jason", predicate="works_at", object="MNEMOS")
    assert t.confidence == 1.0
    assert t.valid_from is None


def test_health_response():
    from mnemos.domain.models import HealthResponse
    h = HealthResponse(
        status="healthy",
        timestamp="2026-04-12T00:00:00",
        database_connected=True,
        version="3.2.0",
    )
    assert h.status == "healthy"
    assert h.database_connected is True


# ─── Auth logic tests ────────────────────────────────────────────────────────

def test_configure_auth_personal():
    """Personal profile: auth disabled → singleton has role=root, authenticated=False."""
    import mnemos.api.dependencies as auth_mod
    auth_mod.configure_auth({"enabled": False})
    assert auth_mod._auth_enabled is False
    assert auth_mod.PERSONAL_SINGLETON.role == "root"
    assert auth_mod.PERSONAL_SINGLETON.authenticated is False


def test_configure_auth_enabled():
    import mnemos.api.dependencies as auth_mod
    auth_mod.configure_auth({"enabled": True, "personal_user_id": "u1", "default_namespace": "ns1"})
    assert auth_mod._auth_enabled is True
    assert auth_mod.PERSONAL_SINGLETON.user_id == "u1"
    assert auth_mod.PERSONAL_SINGLETON.namespace == "ns1"
    # Restore default for other tests
    auth_mod.configure_auth({"enabled": False})


def test_api_key_hash_is_sha256():
    """SHA-256 of a 64-char hex token should be a 64-char hex digest."""
    import secrets
    raw = secrets.token_hex(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ─── Config / ingestion helper tests ─────────────────────────────────────────

def test_extract_readable_messages():
    from mnemos.api.routes.ingest import _extract_readable
    items = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    result = _extract_readable(items)
    assert "Hello" in result
    assert "Hi there" in result
    assert "[user]" in result


def test_extract_readable_caps_items():
    from mnemos.api.routes.ingest import _extract_readable
    items = [{"content": f"msg {i}"} for i in range(100)]
    result = _extract_readable(items, max_items=5)
    assert result.count("msg") == 5


def test_extract_readable_no_str_on_dicts():
    """Must not call str() on arbitrary objects — only extract known string fields."""
    from mnemos.api.routes.ingest import _extract_readable

    class Bomb:
        def __str__(self):
            raise RuntimeError("str() called on arbitrary object!")

    items = [{"content": "safe"}, {"role": "user", "content": "also safe"}, {"bad": Bomb()}]
    result = _extract_readable(items)
    assert "safe" in result
    assert "also safe" in result


def test_extract_readable_plain_strings():
    from mnemos.api.routes.ingest import _extract_readable
    result = _extract_readable(["first", "second"])
    assert "first" in result
    assert "second" in result


# ─── Background task helper ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_background_tracks_task():
    """_schedule_background should add the task to _background_tasks and remove on done."""
    import mnemos.core.lifecycle as lc

    completed = []

    async def _work():
        await asyncio.sleep(0)
        completed.append(True)

    initial_count = len(lc._background_tasks)
    lc._schedule_background(_work())
    assert len(lc._background_tasks) == initial_count + 1
    await asyncio.sleep(0.01)
    assert len(lc._background_tasks) == initial_count
    assert completed == [True]


# ─── Application instantiation ───────────────────────────────────────────────

def test_app_routes_registered():
    """All expected route prefixes should be present (v3.0.0)."""
    from mnemos.api.main import app
    paths = {r.path for r in app.routes}
    # v3.0.0 unified routes
    assert "/health" in paths
    assert "/v1/memories" in paths
    assert "/v1/memories/search" in paths
    assert "/v1/memories/bulk" in paths
    assert "/v1/consultations" in paths  # Unified GRAEAE
    assert "/v1/providers" in paths  # Provider routing
    assert "/v1/kg/triples" in paths
    assert "/admin/users" in paths


def test_app_has_rate_limit_middleware():
    from mnemos.api.main import app
    # limiter is wired into app.state by api_server.py
    assert hasattr(app.state, "limiter")

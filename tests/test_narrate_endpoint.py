"""GET /v1/memories/{id}/narrate — APOLLO dense → prose readback.

Covers the rule-based narration dispatcher and the HTTP handler's
branching on (variant present|absent) × (engine apollo|other) ×
(format prose|dense) × (tenancy root|non-root).

Helpers directly in mnemos.domain.compression.apollo get unit-tested separately;
this file validates the HTTP surface + handler logic.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.api.routes.narrate import narrate
from mnemos.domain.compression.apollo import (
    _narrate_fallback_form,
    looks_like_fallback,
    looks_like_portfolio,
    narrate_encoded,
)

# ── async-context test double (matches the fixture shape used by
# test_admin_user_namespace / test_admin_federation_role) ────────────────


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


def _mock_pool(monkeypatch, memory_row=None, variant_row=None):
    from mnemos.core import lifecycle

    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[memory_row, variant_row])
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncContext(mock_conn))
    monkeypatch.setattr(lifecycle, "_pool", mock_pool)
    return mock_pool, mock_conn


def _memory_row(memory_id="m1", content="raw prose content"):
    return {"id": memory_id, "content": content}


def _variant_row(engine_id="apollo", engine_version="0.2",
                 compressed_content="AAPL:100@150.25/175.50:tech"):
    return {
        "engine_id": engine_id,
        "engine_version": engine_version,
        "compressed_content": compressed_content,
    }


def _user(role="root", user_id="root", namespace="default"):
    u = MagicMock()
    u.role = role
    u.user_id = user_id
    u.namespace = namespace
    return u


# ── helper: dispatcher sniffs ──────────────────────────────────────────────


def test_looks_like_portfolio_matches_dense_form():
    assert looks_like_portfolio("AAPL:100@150.25/175.50:tech")
    assert looks_like_portfolio("AAPL:100@150.25/175.50:tech;MSFT:50@300/310:tech")


def test_looks_like_portfolio_rejects_non_dense_shapes():
    assert not looks_like_portfolio("")
    assert not looks_like_portfolio("summary=x;facts=[];entities=[];concepts=[]")
    assert not looks_like_portfolio("just some prose")


def test_looks_like_fallback_matches_fallback_shape():
    assert looks_like_fallback(
        "summary=alice joined acme;facts=[alice-joined-acme];"
        "entities=[alice|acme];concepts=[hire]"
    )


def test_looks_like_fallback_rejects_portfolio():
    assert not looks_like_fallback("AAPL:100@150.25/175.50:tech")


# ── helper: fallback narration ─────────────────────────────────────────────


def test_narrate_fallback_form_renders_all_sections():
    encoded = (
        "summary=alice joined acme;facts=[hired-engineer|signed-offer];"
        "entities=[alice|acme];concepts=[hire|onboarding]"
    )
    out = _narrate_fallback_form(encoded)
    # Summary first, then Facts/Entities/Concepts sections.
    assert "alice joined acme" in out
    assert "Facts: hired-engineer, signed-offer" in out
    assert "Entities: alice, acme" in out
    assert "Concepts: hire, onboarding" in out


def test_narrate_fallback_form_skips_empty_sections():
    encoded = "summary=alice joined acme;facts=[];entities=[];concepts=[]"
    out = _narrate_fallback_form(encoded)
    # Summary present; no empty "Facts: ." or similar.
    assert "alice joined acme" in out
    assert "Facts:" not in out
    assert "Entities:" not in out
    assert "Concepts:" not in out


def test_narrate_fallback_form_adds_trailing_period_when_missing():
    encoded = "summary=alice joined acme;facts=[];entities=[];concepts=[]"
    out = _narrate_fallback_form(encoded)
    assert out.startswith("alice joined acme.")


def test_narrate_fallback_form_preserves_existing_terminator():
    encoded = "summary=is she ok?;facts=[];entities=[];concepts=[]"
    out = _narrate_fallback_form(encoded)
    assert out.startswith("is she ok?")


# ── dispatcher: narrate_encoded ────────────────────────────────────────────


def test_narrate_encoded_dispatches_portfolio():
    out = narrate_encoded("AAPL:100@150.25/175.50:tech;MSFT:50@300/310:unclassified")
    # Portfolio narrator emits sentences per position.
    assert "AAPL" in out and "MSFT" in out
    assert "basis" in out.lower()


def test_narrate_encoded_dispatches_fallback():
    out = narrate_encoded(
        "summary=weekly standup;facts=[bob-deployed-x];entities=[bob];concepts=[deploy]"
    )
    assert "weekly standup" in out
    assert "Facts: bob-deployed-x" in out


def test_narrate_encoded_unknown_shape_passes_through():
    # Shape that matches neither sniffer → return verbatim.
    unknown = "this is not a recognized dense form"
    assert narrate_encoded(unknown) == unknown


def test_narrate_encoded_empty_input_safe():
    assert narrate_encoded("") == ""
    assert narrate_encoded(None) == ""


# ── handler: HTTP branching ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_404_when_memory_missing(monkeypatch):
    _mock_pool(monkeypatch, memory_row=None, variant_row=None)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await narrate(memory_id="m1", format="prose", user=_user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_handler_raw_when_no_variant(monkeypatch):
    """No winning variant → return raw memory content, source='raw'."""
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(content="the unprocessed memory text"),
        variant_row=None,
    )
    resp = await narrate(memory_id="m1", format="prose", user=_user())
    assert resp.source == "raw"
    assert resp.content == "the unprocessed memory text"
    assert resp.format == "prose"
    assert resp.engine_id is None


@pytest.mark.asyncio
async def test_handler_apollo_portfolio_narrated(monkeypatch):
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=_variant_row(
            engine_id="apollo",
            compressed_content="AAPL:100@150.25/175.50:tech",
        ),
    )
    resp = await narrate(memory_id="m1", format="prose", user=_user())
    assert resp.source == "narrated"
    assert resp.engine_id == "apollo"
    assert "AAPL" in resp.content
    assert "basis" in resp.content.lower()


@pytest.mark.asyncio
async def test_handler_apollo_fallback_narrated(monkeypatch):
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=_variant_row(
            engine_id="apollo",
            compressed_content=(
                "summary=alice joined acme;facts=[signed-offer];"
                "entities=[alice|acme];concepts=[hire]"
            ),
        ),
    )
    resp = await narrate(memory_id="m1", format="prose", user=_user())
    assert resp.source == "narrated"
    assert "alice joined acme" in resp.content
    assert "Facts: signed-offer" in resp.content


@pytest.mark.asyncio
async def test_handler_non_apollo_variant_passthrough(monkeypatch):
    """Non-APOLLO output is already prose — don't narrate."""
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=_variant_row(
            engine_id="artemis",
            engine_version="1.0",
            compressed_content="Short extractive prose output.",
        ),
    )
    resp = await narrate(memory_id="m1", format="prose", user=_user())
    assert resp.source == "variant_passthrough"
    assert resp.engine_id == "artemis"
    assert resp.content == "Short extractive prose output."


@pytest.mark.asyncio
async def test_handler_dense_format_returns_variant_verbatim(monkeypatch):
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=_variant_row(
            engine_id="apollo",
            compressed_content="AAPL:100@150.25/175.50:tech",
        ),
    )
    resp = await narrate(memory_id="m1", format="dense", user=_user())
    assert resp.source == "variant_dense"
    assert resp.format == "dense"
    assert resp.content == "AAPL:100@150.25/175.50:tech"


@pytest.mark.asyncio
async def test_handler_dense_format_falls_back_to_raw_when_no_variant(monkeypatch):
    """`format=dense` with no variant returns raw memory content —
    always-safe-to-call contract."""
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(content="raw body"),
        variant_row=None,
    )
    resp = await narrate(memory_id="m1", format="dense", user=_user())
    assert resp.source == "raw"
    assert resp.content == "raw body"


@pytest.mark.asyncio
async def test_handler_unknown_apollo_shape_passes_through(monkeypatch):
    """Defense-in-depth: an APOLLO variant whose encoded form doesn't
    match any known schema sniff should render verbatim rather than
    404'ing or raising."""
    _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=_variant_row(
            engine_id="apollo",
            compressed_content="future-schema-payload-not-yet-released",
        ),
    )
    resp = await narrate(memory_id="m1", format="prose", user=_user())
    assert resp.source == "narrated"
    assert resp.content == "future-schema-payload-not-yet-released"


# ── tenancy: non-root uses owner+namespace filter ─────────────────────────


@pytest.mark.asyncio
async def test_handler_non_root_uses_namespace_scoped_query(monkeypatch):
    """Verify the handler sends an owner_id + namespace filter on the
    memory lookup for non-root callers. The mock records the SQL
    invoked so we can assert the gate is in place."""
    _, conn = _mock_pool(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=None,
    )
    user = _user(role="user", user_id="alice", namespace="tenant-a")
    await narrate(memory_id="m1", format="prose", user=user)

    # First fetchrow call is the memory lookup.
    call = conn.fetchrow.await_args_list[0]
    sql = call.args[0]
    assert "owner_id" in sql, "non-root memory lookup must filter by owner_id"
    assert "namespace" in sql, "non-root memory lookup must filter by namespace"
    assert "alice" in call.args, "owner_id value must be bound"
    assert "tenant-a" in call.args, "namespace value must be bound"

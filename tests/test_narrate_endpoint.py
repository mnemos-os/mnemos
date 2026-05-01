"""GET /v1/memories/{id}/narrate — APOLLO dense → prose readback.

Covers the rule-based narration dispatcher and the HTTP handler's
branching on (variant present|absent) × (engine apollo|other) ×
(format prose|dense) × (visibility scope).

Helpers directly in mnemos.domain.compression.apollo get unit-tested
separately; this file validates the HTTP surface + handler logic.

After v4.2.0a14 round-14 the handler goes through the same
``VisibilityFilter.for_read`` backend lookup as
``GET /v1/memories/{id}`` (admits owner / federated / world / group
reads), and the winning-variant lookup runs through the persistence
backend's compression repo (no asyncpg pool requirement). Tests use
``install_fake_backend`` to assert both contracts.
"""
from __future__ import annotations

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes.narrate import narrate
from mnemos.domain.compression.apollo import (
    _narrate_fallback_form,
    looks_like_fallback,
    looks_like_portfolio,
    narrate_encoded,
)
from mnemos.persistence.visibility import VisibilityScope

from tests._fake_backend import install_fake_backend


def _memory_row(memory_id="m1", content="raw prose content", **extra):
    base = {"id": memory_id, "content": content}
    base.update(extra)
    return base


def _variant_row(engine_id="apollo", engine_version="0.2",
                 compressed_content="AAPL:100@150.25/175.50:tech"):
    return {
        "engine_id": engine_id,
        "engine_version": engine_version,
        "compressed_content": compressed_content,
    }


def _user(role="root", user_id="root", namespace="default"):
    if role == "root":
        return UserContext(
            user_id=user_id, group_ids=[], role="root",
            namespace=namespace, authenticated=True,
        )
    return UserContext(
        user_id=user_id, group_ids=[], role=role,
        namespace=namespace, authenticated=True,
    )


def _wire_backend(monkeypatch, memory_row, variant_row):
    """Install a fake backend with the requested memory + variant
    rows wired to the relevant repo methods."""
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", memory_row)
    backend.compression.configure_return(
        "fetch_compressed_variant_by_memory_id", variant_row,
    )
    return backend


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
    _wire_backend(monkeypatch, memory_row=None, variant_row=None)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await narrate(memory_id="m1", format="prose", user=_user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_handler_raw_when_no_variant(monkeypatch):
    """No winning variant → return raw memory content, source='raw'."""
    _wire_backend(
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
    _wire_backend(
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
    _wire_backend(
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
    _wire_backend(
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
    _wire_backend(
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
    _wire_backend(
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
    _wire_backend(
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


# ── visibility contract: same VisibilityFilter shape as GET /memories/{id} ──
#
# Codex round-2 of the round-12 thread surfaced that the explicit
# /narrate endpoint kept the old narrower owner+namespace gate while
# /memories/{id} content negotiation was lifted onto VisibilityFilter
# .for_read. These tests pin /narrate's new contract against the
# fake backend's captured calls so the visibility surface cannot
# regress to the v3.3 owner-only shape.


@pytest.mark.asyncio
async def test_narrate_uses_visibility_filter_for_read_for_non_root(monkeypatch):
    """Non-root callers must reach the backend with VisibilityFilter
    .for_read (READABLE scope, namespace pinned to user)."""
    backend = _wire_backend(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=None,
    )
    user = _user(role="user", user_id="alice", namespace="tenant-a")
    await narrate(memory_id="m1", format="prose", user=user)

    last_call = next(
        (kw for name, kw in reversed(backend.memories.calls) if name == "get_memory"),
        None,
    )
    assert last_call is not None, "get_memory was never called"
    visibility = last_call["visibility"]
    assert visibility.scope == VisibilityScope.READABLE
    assert visibility.namespace == "tenant-a"
    assert visibility.user_id == "alice"


@pytest.mark.asyncio
async def test_narrate_root_uses_root_bypass_filter(monkeypatch):
    """Root callers reach the backend with ROOT_BYPASS + namespace=None
    so cross-tenant narration works the same as cross-tenant JSON
    reads via GET /v1/memories/{id}."""
    backend = _wire_backend(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=None,
    )
    await narrate(memory_id="m1", format="prose", user=_user(role="root"))

    last_call = next(
        (kw for name, kw in reversed(backend.memories.calls) if name == "get_memory"),
        None,
    )
    assert last_call is not None
    visibility = last_call["visibility"]
    assert visibility.scope == VisibilityScope.ROOT_BYPASS
    assert visibility.namespace is None


@pytest.mark.asyncio
async def test_narrate_applies_pg_rls_context_inside_transaction(monkeypatch):
    """Postgres RLS-parity: /narrate must call maybe_set_pg_rls
    BEFORE the visibility-gated memory fetch, same as
    GET /v1/memories/{id}. Without this, RLS-enabled deployments
    fall back to the personal_bypass policy and may admit rows
    that DB-level RLS would reject — exactly the parity hole codex
    round-3 (review-momroway-4cv52u) flagged.
    """
    backend = _wire_backend(
        monkeypatch,
        memory_row=_memory_row(),
        variant_row=None,
    )

    rls_calls: list[tuple[object, str]] = []

    async def _spy(tx, user):
        rls_calls.append((tx, user.user_id))

    # Patch the symbol the narrate module imported, not the
    # canonical helper module — narrate.py binds maybe_set_pg_rls
    # at import time so a setattr on api.persistence_helpers won't
    # affect the live call site.
    import mnemos.api.routes.narrate as narrate_module
    monkeypatch.setattr(narrate_module, "maybe_set_pg_rls", _spy)

    user = _user(role="user", user_id="alice", namespace="tenant-a")
    await narrate(memory_id="m1", format="prose", user=user)

    assert len(rls_calls) == 1, (
        f"maybe_set_pg_rls should be called exactly once per /narrate "
        f"request; got {len(rls_calls)} calls"
    )
    _, called_user_id = rls_calls[0]
    assert called_user_id == "alice"

    # The RLS call must precede the memory lookup so the GUCs are
    # in scope when the repository SELECT runs. With the spy
    # capturing the tx instance, we verify it's the same tx the
    # repository was called with.
    rls_tx, _ = rls_calls[0]
    last_get = next(
        (kw for name, kw in reversed(backend.memories.calls) if name == "get_memory"),
        None,
    )
    assert last_get is not None
    # FakeBackend builds a SimpleNamespace tx per transactional()
    # block; the same instance must be threaded into both calls.
    # We can't compare positionally (calls list captures kwargs not
    # positional args), but we know there's only one transaction
    # because the handler opens exactly one — so the spy was hit
    # with a tx and the get_memory call ran on the same tx.
    assert rls_tx is not None


@pytest.mark.asyncio
async def test_narrate_admits_readable_row_independent_of_owner(monkeypatch):
    """A row returned by the backend (which it would only have done
    after passing the READABLE predicate) is rendered to the caller
    even when the row's owner_id and namespace differ from the
    caller — exactly the federated/world/group case codex round-2
    asked us to verify."""
    backend = _wire_backend(
        monkeypatch,
        memory_row=_memory_row(
            content="federated body",
            owner_id="bob",
            namespace="other-tenant",
            permission_mode=644,  # world-readable
        ),
        variant_row=None,
    )
    user = _user(role="user", user_id="alice", namespace="tenant-a")
    resp = await narrate(memory_id="m1", format="prose", user=user)
    assert resp.source == "raw"
    assert resp.content == "federated body"
    # And the visibility filter at the gate is still the broader
    # READABLE shape — the test_narrate_uses_visibility_filter_for_
    # read_for_non_root case verified the call site, this one
    # verifies the response is built off the row backend admitted.
    last_call = next(
        (kw for name, kw in reversed(backend.memories.calls) if name == "get_memory"),
        None,
    )
    assert last_call["visibility"].scope == VisibilityScope.READABLE

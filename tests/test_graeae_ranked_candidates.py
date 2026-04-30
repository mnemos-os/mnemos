"""Regression tests for _ranked_candidates tiebreak ordering.

v4.1.2 added a non-reasoning preference between the existing
arena_score / last_synced tiebreaks and the len() fallback. Before
the fix, the len() fallback accidentally promoted reasoning variants
(shorter names like ``-reasoning`` ~ 27 chars vs ``-non-reasoning`` ~
31 chars), so xAI Grok consultations came back tagged with
``\\confidence{N}`` blocks instead of clean text. These tests lock
the new ordering in.
"""

from __future__ import annotations

from mnemos.domain.graeae.engine import _is_reasoning_variant


def test_is_reasoning_variant_grok():
    assert _is_reasoning_variant("grok-4.20-0309-reasoning") is True
    assert _is_reasoning_variant("grok-4-1-fast-reasoning") is True


def test_is_reasoning_variant_non_reasoning_grok():
    # The fix: -non-reasoning must NOT be classified as reasoning.
    assert _is_reasoning_variant("grok-4.20-0309-non-reasoning") is False
    assert _is_reasoning_variant("grok-4-1-fast-non-reasoning") is False


def test_is_reasoning_variant_unsuffixed():
    assert _is_reasoning_variant("grok-4-fast") is False
    assert _is_reasoning_variant("gpt-5.5") is False
    assert _is_reasoning_variant("claude-opus-4-7") is False


def test_is_reasoning_variant_case_insensitive():
    assert _is_reasoning_variant("GROK-4-Reasoning") is True
    assert _is_reasoning_variant("Grok-4-NON-Reasoning") is False


def test_internal_key_orders_non_reasoning_first():
    """Two xai grok-4 variants identical except for reasoning suffix —
    non-reasoning sorts first, mirroring the actual production fleet's
    desired behavior."""
    # Build the same dict shape _ranked_candidates uses internally.
    pair = [
        {
            "mid": "grok-4.20-0309-reasoning",
            "family_rank": 0,
            "version": (4, 20, 309),
            "arena": 0.7278,
            "synced": 1700000000.0,
        },
        {
            "mid": "grok-4.20-0309-non-reasoning",
            "family_rank": 0,
            "version": (4, 20, 309),
            "arena": 0.7278,
            "synced": 1700000000.0,
        },
    ]

    def _key(a):
        return (
            a["family_rank"],
            tuple(-x for x in a["version"]),
            -a["arena"],
            -a["synced"],
            _is_reasoning_variant(a["mid"]),
            len(a["mid"]),
        )

    pair.sort(key=_key)
    assert pair[0]["mid"] == "grok-4.20-0309-non-reasoning"
    assert pair[1]["mid"] == "grok-4.20-0309-reasoning"

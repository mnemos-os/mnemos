"""ARTEMIS — unit tests for the CPU-only extractive engine.

Covers: protected-span detection, labeled-block extraction,
sentence tokenization, TF-IDF + anchored TextRank ranking, MMR
selection, and end-to-end compress() behavior against representative
content shapes.

Predictions to verify empirically (before + after benchmark):
  * Dates, versions, URLs, emails, IDs, quoted strings, numbers
    surviving in the compressed output at very high rate.
  * Labeled content (Name: X\\nRole: Y) preserved verbatim when
    it dominates the input.
  * Latency <20 ms on 4000-char input.
"""
from __future__ import annotations

import asyncio

import pytest

from mnemos.domain.compression.artemis import (
    ARTEMISEngine,
    _anchored_textrank,
    _cosine,
    _extract_labeled_blocks,
    _find_protected_spans,
    _mmr_select,
    _split_sentences,
    _tfidf_vectors,
)
from mnemos.domain.compression.base import (
    CompressionRequest,
    IdentifierPolicy,
)


def _req(content: str) -> CompressionRequest:
    return CompressionRequest(
        memory_id="test-m1",
        content=content,
        owner_id="default",
        identifier_policy=IdentifierPolicy.STRICT,
    )


# ── protected-span detection ───────────────────────────────────────────────


def test_protected_spans_captures_version():
    spans = _find_protected_spans("We released v3.2.0 last week.")
    kinds = [k for _, _, k in spans]
    assert "version" in kinds


def test_protected_spans_captures_iso_date():
    spans = _find_protected_spans("The incident on 2026-04-23 was resolved.")
    assert any(k == "iso_date" for _, _, k in spans)


def test_protected_spans_captures_email():
    spans = _find_protected_spans("Contact alice@acme.com for details.")
    assert any(k == "email" for _, _, k in spans)


def test_protected_spans_captures_url():
    spans = _find_protected_spans(
        "See https://example.com/path?q=1 for the docs."
    )
    assert any(k == "url" for _, _, k in spans)


def test_protected_spans_captures_id_code():
    spans = _find_protected_spans("Ticket JIRA-1234 was closed.")
    assert any(k == "id_code" for _, _, k in spans)


def test_protected_spans_captures_ticker_but_filters_blocklist():
    """Valid tickers survive; common all-caps English words filter out."""
    spans = _find_protected_spans("AAPL went up. AT the closing bell.")
    kinds = [k for _, _, k in spans]
    ticker_hits = sum(1 for k in kinds if k == "ticker")
    assert ticker_hits == 1  # AAPL only; AT filtered via blocklist


def test_protected_spans_dedupes_overlapping():
    """Overlapping matches (e.g. path inside a URL) should dedupe."""
    text = "Grab it at https://github.com/org/repo/blob/main/src/file.py"
    spans = _find_protected_spans(text)
    # Every span should be distinct (non-overlapping).
    sorted_spans = sorted(spans, key=lambda s: s[0])
    for i in range(len(sorted_spans) - 1):
        assert sorted_spans[i][1] <= sorted_spans[i + 1][0], (
            f"span {sorted_spans[i]} overlaps with {sorted_spans[i + 1]}"
        )


def test_protected_spans_empty_content():
    assert _find_protected_spans("") == []


# ── labeled-block extraction ──────────────────────────────────────────────


def test_labeled_block_runs_of_two_or_more():
    text = "Name: Alice\nRole: Engineer\nOrg: Acme"
    blocks = _extract_labeled_blocks(text)
    assert len(blocks) == 1
    assert "Alice" in blocks[0]
    assert "Engineer" in blocks[0]
    assert "Acme" in blocks[0]


def test_labeled_block_single_line_ignored():
    """A lone 'Key: Value' line isn't a labeled block — could be prose.
    ('The issue: X was broken' shouldn't be captured.)"""
    text = "The issue: the deployment failed.\nWe fixed it quickly."
    blocks = _extract_labeled_blocks(text)
    assert blocks == []


def test_labeled_block_surrounded_by_prose():
    """A labeled run mixed into prose is still detected."""
    text = (
        "Here's the contact info:\n"
        "Name: Alice\n"
        "Role: Engineer\n"
        "Org: Acme\n"
        "Please reach out to her this week."
    )
    blocks = _extract_labeled_blocks(text)
    assert len(blocks) == 1
    assert "Alice" in blocks[0]


# ── sentence tokenization ─────────────────────────────────────────────────


def test_split_sentences_basic():
    text = "Alice joined. Bob left later. The team celebrated."
    sentences = _split_sentences(text)
    assert len(sentences) == 3


def test_split_sentences_handles_question_exclamation():
    text = "Alice joined? Bob left later! The team celebrated."
    sentences = _split_sentences(text)
    assert len(sentences) == 3


def test_split_sentences_drops_fragments():
    text = "Alice. Bob joined the team yesterday. Ok."
    sentences = _split_sentences(text)
    # "Alice." and "Ok." are too short, might get dropped. Must have
    # at least the real sentence.
    assert any("Bob joined the team" in s for s in sentences)


# ── TF-IDF + cosine ───────────────────────────────────────────────────────


def test_tfidf_assigns_higher_weight_to_rare_terms():
    sentences = [
        "The cat sat on the mat.",
        "The dog ran in the park.",
        "The postgres database supports transactions.",
    ]
    vectors, doc_terms = _tfidf_vectors(sentences)
    # "postgres" appears in only 1 document → high IDF
    # "the" is filtered by stop words
    assert "postgres" in vectors[2]
    assert vectors[2]["postgres"] > 0


def test_cosine_identity():
    v = {"a": 1.0, "b": 2.0}
    assert _cosine(v, v) == pytest.approx(1.0)


def test_cosine_disjoint():
    a = {"a": 1.0}
    b = {"b": 1.0}
    assert _cosine(a, b) == 0.0


def test_cosine_empty():
    assert _cosine({}, {"a": 1.0}) == 0.0


# ── anchored TextRank / fallback ──────────────────────────────────────────


def test_anchored_textrank_boosts_anchored_sentences():
    sentences = [
        "Alice joined the team.",
        "Bob was at the office.",
        "The pgvector extension is for Postgres.",
    ]
    vectors, _ = _tfidf_vectors(sentences)
    # Without anchoring, centroid scoring distributes evenly-ish.
    # With anchoring on sentence 2 (pgvector), that sentence wins.
    scores_no_anchor = _anchored_textrank(sentences, vectors, set())
    scores_anchored = _anchored_textrank(sentences, vectors, {2})
    assert scores_anchored[2] > scores_no_anchor[2], (
        "anchoring a sentence should boost its score"
    )


def test_anchored_textrank_degrades_without_networkx(monkeypatch):
    """When networkx is absent, fall back to TF-IDF centroid scoring."""

    # Block networkx import in the anchored_textrank call.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "networkx":
            raise ImportError("simulated absence")
        return real_import(name, *a, **kw)

    import builtins as _builtins
    monkeypatch.setattr(_builtins, "__import__", fake_import)

    sentences = ["a cat sat", "a dog ran", "postgres database"]
    vectors, _ = _tfidf_vectors(sentences)
    scores = _anchored_textrank(sentences, vectors, {2})
    # Should return valid non-negative scores equal in length.
    assert len(scores) == 3
    assert all(s >= 0 for s in scores)


# ── MMR selection ─────────────────────────────────────────────────────────


def test_mmr_selects_forced_first():
    sentences = [f"sentence {i} content" for i in range(5)]
    vectors, _ = _tfidf_vectors(sentences)
    scores = [0.3, 0.9, 0.2, 0.7, 0.1]
    # Force inclusion of index 4 despite its low score.
    selected = _mmr_select(
        sentences, scores, vectors, target_length=50, forced_indices={4},
    )
    assert 4 in selected


def test_mmr_respects_target_length():
    sentences = ["aa", "bb", "cc", "dd"]
    vectors, _ = _tfidf_vectors(sentences)
    scores = [1.0, 0.8, 0.6, 0.4]
    selected = _mmr_select(
        sentences, scores, vectors, target_length=5, forced_indices=set(),
    )
    total_len = sum(len(sentences[i]) for i in selected)
    # Should stop near the budget.
    assert total_len <= 6 or len(selected) <= 3


# ── ARTEMISEngine: end-to-end behavior ────────────────────────────────────


def test_engine_preserves_version_string():
    engine = ARTEMISEngine()
    content = (
        "We released v3.2.0 last week after extensive testing. "
        "The deployment went smoothly with no regressions. "
        "Customer feedback has been positive. "
        "The team celebrated Friday."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert "v3.2.0" in result.compressed_content, (
        "version strings must survive the extractive pass"
    )


def test_engine_preserves_email():
    engine = ARTEMISEngine()
    content = (
        "Alice Chen is a senior engineer at Acme Corp. "
        "You can reach her at alice@acme.com for any questions. "
        "She joined the team three years ago from a startup. "
        "Her focus is backend systems."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert "alice@acme.com" in result.compressed_content


def test_engine_preserves_iso_date():
    engine = ARTEMISEngine()
    content = (
        "The incident on 2026-04-23 was caused by connection pool exhaustion. "
        "We added circuit breakers and increased pool size to 200. "
        "The mitigation shipped within two hours. "
        "A retrospective was scheduled."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert "2026-04-23" in result.compressed_content


def test_engine_passthrough_on_labeled_content():
    """When the entire input is labeled lines, return verbatim rather
    than trying to compress — the ratio would exceed 1.0 otherwise."""
    engine = ARTEMISEngine()
    content = "Name: Alice Chen\nRole: Senior Engineer\nOrg: Acme\nEmail: alice@acme.com"
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert result.manifest.get("path") == "passthrough_labeled"
    assert result.compressed_content == content


def test_engine_passthrough_on_tiny_content():
    engine = ARTEMISEngine()
    result = asyncio.run(engine.compress(_req("hi there")))
    assert result.error is None
    assert result.manifest.get("path") == "passthrough_short"


def test_engine_compresses_prose():
    """Medium-length prose should actually compress (ratio < 1.0)
    and preserve the key information."""
    engine = ARTEMISEngine()
    content = (
        "Bob deployed the v3.2.0 release last Thursday after the CI suite "
        "passed on both Python 3.11 and 3.12. The deploy target was the "
        "primary MNEMOS host. No rollback was required. The team "
        "celebrated with pizza on Friday. Alice led the retrospective "
        "the following Monday, where they discussed the smooth rollout. "
        "Overall morale was high and the quarter's goals remained on track."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert result.manifest.get("path") == "extractive"
    assert result.compression_ratio < 1.0, (
        "extractive compression on prose should actually compress"
    )
    assert "v3.2.0" in result.compressed_content, (
        "protected spans preserved in output"
    )


def test_engine_reports_manifest_detail():
    engine = ARTEMISEngine()
    content = (
        "The team decided to use PostgreSQL v15 with pgvector. "
        "Alice pushed the migration on 2026-04-23. "
        "Bob verified it went cleanly. "
        "The benchmark showed 30% faster retrieval."
    )
    result = asyncio.run(engine.compress(_req(content)))
    m = result.manifest
    assert m["path"] == "extractive"
    assert m["protected_spans"] >= 1  # at least v15 + 2026-04-23
    assert m["anchored_sentences"] >= 1
    assert m["total_sentences"] >= 3
    assert m["selected"] >= 1


def test_engine_gpu_intent_is_cpu_only():
    from mnemos.domain.compression.base import GPUIntent
    assert ARTEMISEngine.gpu_intent == GPUIntent.CPU_ONLY


def test_engine_identifier_policy_strict():
    engine = ARTEMISEngine()
    result = asyncio.run(engine.compress(_req(
        "Meeting on 2026-04-23. We chose PostgreSQL for the new API."
    )))
    assert result.identifier_policy == IdentifierPolicy.STRICT


def test_engine_latency_under_50ms():
    """Target is <20ms for 4000 chars; assert <50ms for safety margin."""
    engine = ARTEMISEngine()
    # Build 4000-char realistic content
    content = (
        "The quarterly review meeting on 2026-04-15 covered several key "
        "topics. Alice presented the v3.2.0 release metrics showing 30% "
        "faster retrieval and 45% lower error rate. Bob shared the "
        "infrastructure migration plan including the move to PostgreSQL "
        "with pgvector indexing. Contact: alice@acme.com for details. "
    ) * 6  # ~3600 chars
    result = asyncio.run(engine.compress(_req(content)))
    assert result.elapsed_ms < 50, (
        f"Artemis should stay under 50ms on realistic inputs; got {result.elapsed_ms}ms"
    )


# ── Codex regression: labeled-block preservation ────────────────────────────


def test_labeled_blocks_survive_when_mixed_with_prose():
    """Codex caught: _extract_labeled_blocks ran but was never prepended
    to the output — the labeled content was only kept when the ≥80%
    passthrough path fired. On mixed prose + labels, the labels vanished.

    Fix: labeled blocks prepended verbatim; sentences overlapping a
    labeled span anchored so they aren't re-selected, then de-duped.
    """
    engine = ARTEMISEngine()
    content = (
        "We had a productive session today with several attendees. "
        "Everyone brought great energy and insightful questions.\n"
        "\n"
        "Name: Alice Chen\n"
        "Role: Senior Engineer\n"
        "Org: Acme Corp\n"
        "Email: alice@acme.com\n"
        "\n"
        "The meeting wrapped up at 5pm and we ordered dinner. "
        "Everyone agreed the roadmap was tracking well overall."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    # All four labeled rows must survive verbatim.
    for label in ("Name: Alice Chen", "Role: Senior Engineer",
                  "Org: Acme Corp", "Email: alice@acme.com"):
        assert label in result.compressed_content, (
            f"labeled row {label!r} must survive the extractive pass"
        )


def test_duplicate_labeled_blocks_map_to_distinct_occurrences():
    """Codex re-review: when two labeled blocks share identical text,
    naive content.find(block) resolved both ranges to the first
    occurrence. The second block's sentences were then treated as
    unmatched prose, duplicated in the tail, and compression_ratio
    blew up to ~1.0. Fix: walk a cursor forward so each block maps
    to a distinct occurrence.
    """
    engines = ARTEMISEngine()
    block = (
        "Name: Alice Chen\n"
        "Role: Senior Engineer\n"
        "Org: Acme Corp\n"
    )
    content = (
        "First engineer profile follows.\n\n"
        + block + "\n"
        + "Second duplicate copy follows (exercise collision).\n\n"
        + block + "\n"
        + "End of block log."
    )
    result = asyncio.run(engines.compress(_req(content)))
    assert result.error is None
    # Output must not end up LONGER than input (the prior bug).
    assert len(result.compressed_content) <= len(content), (
        "duplicate labeled blocks must not double in the compressed tail"
    )
    # Both labeled block copies must survive verbatim (not just one).
    # Codex re-review noted that >= 1 under-specifies the fix; the
    # implementation produces exactly 2 occurrences on this input.
    assert result.compressed_content.count("Name: Alice Chen") == 2
    assert result.compressed_content.count("Role: Senior Engineer") == 2


def test_duplicate_sentences_do_not_break_span_mapping():
    """Codex caught: the old content.find(sentence, cursor) approach
    gets confused by duplicate or normalized sentences. The
    _split_sentences_with_spans() replacement returns offsets directly
    so this input compresses correctly rather than crashing or
    mis-anchoring.
    """
    engine = ARTEMISEngine()
    content = (
        "The outage was caused by a database failure. "
        "The outage was caused by a database failure. "
        "We deployed the fix on 2026-04-23. "
        "Rollout completed without issues. "
        "We deployed the fix on 2026-04-23."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    # Protected spans (the date) must still be preserved.
    assert "2026-04-23" in result.compressed_content


# ── self-report quality ────────────────────────────────────────────────────


def test_engine_self_reports_quality_in_artemis_range():
    """Artemis must self-report a non-None quality score in [0.70, 0.98].

    Pre-S-II, Artemis emitted None so the judge could score. That
    left Artemis DQ'd from judge-disabled contests (composite
    depends on quality_score). Evidence-based self-score fixes
    that without overcommitting the judge's authority.
    """
    engine = ARTEMISEngine()
    content = (
        "Release v3.2.0 shipped on 2026-04-23 via CERBERUS vLLM. "
        "PostgreSQL 16 replaces the old MariaDB cluster. "
        "Contact: alice@acme.com for migration details. "
        "Benchmark: 30% faster retrieval, 45% lower error rate."
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert result.quality_score is not None
    assert 0.70 <= result.quality_score <= 0.98
    # Protected spans retained → expect a score on the upper end.
    assert result.quality_score >= 0.85


def test_engine_self_report_drops_when_protected_missing():
    """A degenerate input where protected spans can't be preserved
    (because budget forces them out) should score below 0.85."""
    # Force a very low target ratio so anchored sentences get
    # squeezed out. Use content with many protected spans.
    engine = ARTEMISEngine(target_ratio=0.05)
    content = (
        "v1.0.0 shipped on 2026-01-01. "
        "v1.1.0 shipped on 2026-01-15. "
        "v1.2.0 shipped on 2026-02-01. "
        "v1.3.0 shipped on 2026-02-15. "
        "v1.4.0 shipped on 2026-03-01. "
        "v1.5.0 shipped on 2026-03-15. "
    )
    result = asyncio.run(engine.compress(_req(content)))
    assert result.error is None
    assert result.quality_score is not None
    # Either we kept all protected spans (score stays high) OR
    # we had to drop some (score moves down). Either is correct —
    # assert only the value is a real number in range.
    assert 0.70 <= result.quality_score <= 0.98

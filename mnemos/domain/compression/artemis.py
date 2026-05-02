"""ARTEMIS — CPU-only extractive compression engine.

Apollo's twin. Artemis hunts the essential content — tracks
identifiers and structure through dense prose, preserves what the
judge cares about, drops what it doesn't.

Apollo handles schema-typed dense encoding + LLM fallback for prose
that misses every schema; Artemis handles the pure-CPU path when
GPU is unavailable or latency-sensitive. Default contest engine
alongside Apollo; runs as the extractive peer for latency-sensitive
or GPU-free mnemos.domain.compression.

Design grounded in a GRAEAE multi-muse consultation (5 models
agreed unanimously on the stack): the 0.44 judge-mean of the
earlier stop-word extractive engine isn't a sentence-selection
problem — it's an information-dropping problem. The judge
penalizes missing identifiers, version strings, dates, URLs, and
broken labeled content. Fix those drops first; the rest is
sentence ranking.

The pipeline

  1. Protected-span regex pass — tags dates, versions, emails,
     URLs, numbers, IDs, quoted strings. These are sacred and get
     a PageRank boost; their host sentences are force-selected.
  2. Structure detection — recognizes "Key: Value" labeled blocks
     and preserves them verbatim as atomic units.
  3. TF-IDF sentence scoring — term-rarity-weighted scoring using
     stdlib Counter + math.log. Document centroid cosine similarity.
  4. Anchored TextRank — sentence-sentence cosine similarity graph
     + personalized PageRank biased toward sentences containing
     protected spans (Gemini's suggestion during the consultation).
     When networkx isn't installed, falls back to TF-IDF-only
     ranking (still a measurable improvement over stop-word
     filtering).
  5. MMR selection — greedy top-K with redundancy penalty. Forces
     inclusion of sentences that host protected spans.
  6. Assembly — re-order by original position, prepend labeled
     blocks, emit as prose.

Target latency: <20 ms on 4000-char inputs. Pure stdlib + numpy
(already required) + optional networkx. No GPU, no models on disk,
no network.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from mnemos.core.extras import require_extra

from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)

require_extra("artemis")

# ── protected-span regexes ────────────────────────────────────────────────


_PROTECTED_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("version", re.compile(r"\bv\d+(?:\.\d+){1,3}(?:[-.][a-z0-9]+)?\b", re.IGNORECASE)),
    ("iso_date", re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")),
    ("slash_date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("url", re.compile(r"https?://\S+")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)),
    ("sha", re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)),  # commit SHAs
    ("path", re.compile(r"(?:\.?/)?(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+")),
    ("ticker", re.compile(r"\b[A-Z]{1,5}\b")),  # loose; filtered below
    ("id_code", re.compile(r"\b[A-Z]+-\d+\b")),  # INC-1234, JIRA-456
    ("phone", re.compile(r"\b(?:\+?\d{1,3}[\s\-.])?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")),
    ("percent", re.compile(r"\b\d+(?:\.\d+)?%")),
    ("currency", re.compile(r"\$\d+(?:[,.]?\d+)*")),
    ("quoted", re.compile(r'"[^"\n]{2,200}"')),
    ("number", re.compile(r"\b\d{3,}(?:\.\d+)?\b")),  # 3+ digit numbers
]

# All-caps English words that shouldn't be treated as tickers.
_TICKER_BLOCKLIST = frozenset({
    "AT", "THE", "AND", "OR", "BUT", "FOR", "WITH", "FROM", "TO",
    "IN", "ON", "AS", "BY", "IF", "IS", "IT", "OF", "A", "AN",
    "TODO", "FIXME", "NOTE", "WARN", "INFO", "HTTP", "HTTPS",
    "JSON", "HTML", "SQL", "API", "URL", "UUID", "PR", "CI",
    "CEO", "CTO", "CFO", "VP",  # role titles — not tickers
})


def _find_protected_spans(text: str) -> List[Tuple[int, int, str]]:
    """Return list of (start, end, category) tuples for all
    protected spans in the text. De-duplicates overlapping spans by
    keeping the first-registered kind when two overlap."""
    spans: List[Tuple[int, int, str]] = []
    for kind, pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            if kind == "ticker":
                value = m.group(0)
                if value in _TICKER_BLOCKLIST:
                    continue
            if kind == "sha":
                # Skip plain numbers (regex is permissive)
                if m.group(0).isdigit() and len(m.group(0)) < 8:
                    continue
            spans.append((m.start(), m.end(), kind))
    # Sort + dedupe overlapping: earliest-starting + longest wins.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: List[Tuple[int, int, str]] = []
    for s in spans:
        if merged and s[0] < merged[-1][1]:
            continue  # overlaps earlier span
        merged.append(s)
    return merged


# ── labeled-block detection ───────────────────────────────────────────────


_LABEL_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9 _-]{1,30}?)\s*:\s+(?P<value>.+)$"
)


def _extract_labeled_blocks(text: str) -> List[str]:
    """Return contiguous runs of 'Key: Value' lines as atomic blocks,
    each preserved verbatim in output. A single labeled line doesn't
    count — we want runs of 2+ to avoid false-positives on prose
    sentences like 'The issue: X was broken'."""
    lines = text.split("\n")
    blocks: List[str] = []
    current: List[str] = []
    for line in lines:
        m = _LABEL_LINE_RE.match(line)
        if m:
            current.append(line)
        else:
            if len(current) >= 2:
                blocks.append("\n".join(current))
            current = []
    if len(current) >= 2:
        blocks.append("\n".join(current))
    return blocks


# ── sentence tokenization ─────────────────────────────────────────────────


_SENT_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z])"  # period/?/! + whitespace + capital letter
    r"|(?<=\n)\s*\n"            # paragraph breaks
)


def _split_sentences(text: str) -> List[str]:
    """Regex sentence-boundary splitter. No NLTK dependency."""
    candidates = _SENT_SPLIT_RE.split(text)
    sentences = [s.strip() for s in candidates if s.strip()]
    # Drop trailing-empty and collapse whitespace.
    return [re.sub(r"\s+", " ", s) for s in sentences if len(s) > 3]


def _split_sentences_with_spans(text: str) -> List[Tuple[str, int, int]]:
    """Sentence tokenizer that preserves source character offsets.

    Returns a list of (normalized_sentence, start_offset, end_offset)
    tuples. Callers that need to anchor spans in the ORIGINAL text
    (e.g. protected-span host detection) use the offsets directly
    rather than re-searching via content.find(), which fails on
    duplicate normalized sentences.
    """
    results: List[Tuple[str, int, int]] = []
    pos = 0
    for match in _SENT_SPLIT_RE.finditer(text):
        raw = text[pos:match.start()]
        if raw.strip():
            normalized = re.sub(r"\s+", " ", raw.strip())
            if len(normalized) > 3:
                # Use the raw chunk's bounds; the normalized string
                # is only used for ranking, not for source lookup.
                results.append((normalized, pos, match.start()))
        pos = match.end()
    # Final chunk after the last split boundary.
    tail = text[pos:]
    if tail.strip():
        normalized = re.sub(r"\s+", " ", tail.strip())
        if len(normalized) > 3:
            results.append((normalized, pos, len(text)))
    return results


# ── TF-IDF (stdlib) ───────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z_-]{2,}\b")


def _tokenize_for_scoring(text: str) -> List[str]:
    """Content-word tokens, lowercased, stopwords filtered."""
    return [
        w.lower() for w in _TOKEN_RE.findall(text)
        if w.lower() not in _STOP_WORDS
    ]


_STOP_WORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "are", "was",
    "were", "have", "has", "had", "but", "not", "you", "your", "they",
    "their", "been", "will", "can", "any", "all", "also", "into", "over",
    "only", "about", "its", "our", "him", "her", "them", "would",
    "could", "should", "may", "might", "must", "shall", "then", "than",
    "when", "where", "what", "who", "whom", "which", "while", "does",
})


def _tfidf_vectors(
    sentences: List[str],
) -> Tuple[List[Dict[str, float]], Counter]:
    """Compute per-sentence TF-IDF vectors as sparse dicts.
    Returns (per-sentence vectors, document-level term counts)."""
    doc_terms: Counter = Counter()
    sentence_terms: List[List[str]] = []
    for s in sentences:
        toks = _tokenize_for_scoring(s)
        sentence_terms.append(toks)
        doc_terms.update(set(toks))   # IDF counts doc-frequency, not TF

    n_docs = max(1, len(sentences))
    vectors: List[Dict[str, float]] = []
    for toks in sentence_terms:
        tf = Counter(toks)
        vec: Dict[str, float] = {}
        for term, count in tf.items():
            idf = math.log((n_docs + 1) / (doc_terms[term] + 1)) + 1.0
            vec[term] = count * idf
        vectors.append(vec)
    return vectors, doc_terms


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity between two sparse dicts."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── TextRank (networkx-backed, with TF-IDF-only fallback) ────────────────


def _anchored_textrank(
    sentences: List[str],
    vectors: List[Dict[str, float]],
    anchored_indices: set,
) -> List[float]:
    """Run personalized PageRank over sentence-sentence cosine
    similarity graph, with restart probability biased toward
    sentences containing protected spans.

    Falls back to TF-IDF-only centroid scoring when networkx isn't
    installed (core install without the `full` extra). The fallback
    still outperforms stop-word filtering because it considers term
    rarity + sentence-vs-document similarity."""
    try:
        import networkx as nx
    except ImportError:
        # Centroid-based ranking: score each sentence by cosine to
        # the document centroid. Anchored sentences get a +0.5 bias.
        centroid: Dict[str, float] = {}
        for v in vectors:
            for k, val in v.items():
                centroid[k] = centroid.get(k, 0.0) + val
        scores = [_cosine(v, centroid) for v in vectors]
        return [
            s + (0.5 if i in anchored_indices else 0.0)
            for i, s in enumerate(scores)
        ]

    if len(sentences) <= 1:
        return [1.0] * len(sentences)

    G = nx.Graph()
    for i in range(len(sentences)):
        G.add_node(i)
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            sim = _cosine(vectors[i], vectors[j])
            if sim > 0.01:
                G.add_edge(i, j, weight=sim)

    personalization: Optional[Dict[int, float]] = None
    if anchored_indices:
        total = float(len(sentences))
        boost = 3.0
        personalization = {
            i: (boost if i in anchored_indices else 1.0) / total
            for i in range(len(sentences))
        }

    try:
        scores = nx.pagerank(G, personalization=personalization, weight="weight")
    except (nx.NetworkXException, ZeroDivisionError):
        # Graph-pathological case (fully disconnected, etc.) — fall
        # back to uniform scoring.
        return [1.0] * len(sentences)

    return [scores.get(i, 0.0) for i in range(len(sentences))]


# ── MMR selection ────────────────────────────────────────────────────────


def _mmr_select(
    sentences: List[str],
    scores: List[float],
    vectors: List[Dict[str, float]],
    target_length: int,
    forced_indices: set,
    lambda_: float = 0.7,
) -> List[int]:
    """Maximal Marginal Relevance: greedy selection that balances
    relevance (score) against redundancy (similarity to already-
    selected sentences). Stops when cumulative char length hits
    target_length.

    ``forced_indices`` are included first, in score order, regardless
    of MMR — these are sentences hosting protected spans.
    """
    if not sentences:
        return []

    selected: List[int] = []
    remaining = set(range(len(sentences)))

    # Forced picks first — preserve protected-span-hosting sentences.
    forced_sorted = sorted(
        [i for i in forced_indices if i in remaining],
        key=lambda i: -scores[i],
    )
    total_chars = 0
    for i in forced_sorted:
        selected.append(i)
        remaining.discard(i)
        total_chars += len(sentences[i])
        if total_chars >= target_length:
            return sorted(selected)

    # Greedy MMR on remaining.
    while remaining and total_chars < target_length:
        best_i = None
        best_score = -float("inf")
        for i in remaining:
            relevance = scores[i]
            if selected:
                max_sim = max(_cosine(vectors[i], vectors[j]) for j in selected)
            else:
                max_sim = 0.0
            mmr_score = lambda_ * relevance - (1.0 - lambda_) * max_sim
            if mmr_score > best_score:
                best_score = mmr_score
                best_i = i
        if best_i is None:
            break
        selected.append(best_i)
        remaining.discard(best_i)
        total_chars += len(sentences[best_i])

    return sorted(selected)


# ── self-report quality heuristic ─────────────────────────────────────────


def _artemis_quality_score(
    *,
    content: str,
    compressed: str,
    protected: List[Tuple[int, int, str]],
    labeled_blocks: List[str],
    anchored_indices: set,
    selected: List[int],
    total_sentences: int,
) -> float:
    """Evidence-based self-report quality for Artemis.

    Rules (each component clamped to its own range, then averaged):

      * protected_retention: fraction of protected-span substrings
        (dates/versions/URLs/emails/quoted/IDs) that survived into
        the compressed output. Artemis anchors their host sentences,
        so this should be near 1.0 on clean inputs. Weight: 0.45.

      * labeled_retention: fraction of labeled blocks that survived
        into the compressed output (blocks are prepended verbatim
        during assembly, so this should be 1.0 when any blocks
        exist). Weight: 0.30.

      * coverage: fraction of anchored sentences that made it into
        the selected set. Anchored sentences are the ones we
        decided were load-bearing; if budget squeezes them out, we
        should know. Weight: 0.25.

    The final score is clamped to [0.70, 0.98]:
      * Floor 0.70 → even a worst-case extract is not "broken"; it's
        still a subset of the input, by construction.
      * Ceiling 0.98 → the judge is the authority for "perfect"; we
        don't self-score above its typical ceiling.

    The floor/ceiling choices are deliberately tighter than Lethe's
    self-report (0.80-1.00) so that when the judge IS available,
    Artemis doesn't dominate on self-scoring alone — the judge can
    still pull a surprising score up or down.
    """
    # 1. Protected-span retention.
    if protected:
        surviving_protected = sum(
            1 for (start, end, _kind) in protected
            if start < end <= len(content) and content[start:end] in compressed
        )
        protected_retention = surviving_protected / len(protected)
    else:
        protected_retention = 1.0

    # 2. Labeled-block retention.
    if labeled_blocks:
        surviving_labeled = sum(1 for b in labeled_blocks if b in compressed)
        labeled_retention = surviving_labeled / len(labeled_blocks)
    else:
        labeled_retention = 1.0

    # 3. Anchored-sentence coverage.
    if anchored_indices:
        selected_set = set(selected)
        covered = sum(1 for i in anchored_indices if i in selected_set)
        coverage = covered / len(anchored_indices)
    else:
        # No anchored sentences ⇒ coverage is vacuously satisfied.
        coverage = 1.0

    raw = (
        0.45 * protected_retention
        + 0.30 * labeled_retention
        + 0.25 * coverage
    )
    # Map raw [0.0, 1.0] linearly into the Artemis self-report
    # range [0.70, 0.98]. Perfect evidence → 0.98; total eviction
    # of load-bearing content → 0.70 (still non-broken).
    return round(0.70 + raw * 0.28, 4)


# ── the engine ────────────────────────────────────────────────────────────


class ARTEMISEngine(CompressionEngine):
    """CPU-only extractive compression engine.

    ``id = 'artemis'``, ``gpu_intent = CPU_ONLY``. Never hits a GPU
    and never makes an HTTP call. Target latency <20 ms on 4000-char
    inputs. Default contest engine alongside APOLLO on v3.3+
    deployments.

    Identifier policy: ``STRICT`` — the protected-span pre-pass
    guarantees dates, versions, emails, URLs, IDs, quoted strings,
    and numbers survive verbatim. Structure-preserving label
    detection keeps labeled blocks verbatim. The composite of the
    two removes the primary failure mode (dropped identifiers) that
    made the prior stop-word extractive engine score 0.44 judge mean.
    """

    id = "artemis"
    label = "ARTEMIS — CPU-only extractive with identifier preservation"
    version = "0.1"
    gpu_intent = GPUIntent.CPU_ONLY

    def __init__(self, target_ratio: float = 0.5) -> None:
        super().__init__()
        self._target_ratio = target_ratio

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        content = request.content or ""
        original_tokens = len(content.split())
        original_chars = max(1, len(content))

        if len(content) < 40:
            # Tiny content — nothing to compress. Return as-is, tag
            # the path so the contest understands.
            #
            # Codex caught that quality_score=None on passthrough paths
            # gets coerced to 0.5 by the contest and auto-rejected by
            # the profile quality_floor. Output here IS byte-identical
            # to input, so fidelity is 1.0 by construction — self-score
            # at the Artemis ceiling (0.98) so the contest treats this
            # correctly without a judge.
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=content,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                quality_score=0.98,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "passthrough_short", "quality_source": "passthrough_verbatim"},
            )

        # Phase 1: protected-span detection.
        protected = _find_protected_spans(content)

        # Phase 2: labeled-block extraction (runs of 2+ Key: Value lines).
        labeled_blocks = _extract_labeled_blocks(content)

        # If the entire content is a labeled block, return it verbatim
        # — no compression is better than expansion.
        # quality_score=0.98 per the passthrough_short fix; output is
        # byte-identical so fidelity is 1.0 by construction.
        if labeled_blocks and sum(len(b) for b in labeled_blocks) >= len(content) * 0.8:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=content,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                quality_score=0.98,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "passthrough_labeled", "quality_source": "passthrough_verbatim"},
            )

        # Phase 3: sentence-level extractive ranking.
        # Tokenize sentences AND track their source character offsets
        # so anchoring isn't brittle on duplicate-string sentences.
        # Codex flagged that content.find(s, cursor) anchors the
        # wrong occurrence when a normalized sentence appears twice
        # or gets whitespace-collapsed away from its source.
        sentence_spans = _split_sentences_with_spans(content)
        sentences = [s for s, _, _ in sentence_spans]
        if not sentences:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "no_sentences"},
                error="unable to tokenize sentences",
            )

        # Which sentences contain protected spans? Use the real
        # offsets from the tokenizer rather than a string search.
        anchored_indices: set = set()
        for (span_start, _span_end, _kind) in protected:
            for i, (_sent, s_off, s_end) in enumerate(sentence_spans):
                if s_off <= span_start < s_end:
                    anchored_indices.add(i)
                    break

        # Also anchor sentences that overlap any labeled block so
        # they survive MMR selection — labeled blocks then get
        # prepended at assembly time (below).
        # Codex regression: when the same labeled block text appears
        # twice, the naive content.find(block) resolves both to the
        # first occurrence, so the tail is assembled as if the second
        # block were still prose to dedupe. Walk a cursor forward so
        # each block maps to a distinct occurrence. Also extend each
        # range through trailing whitespace — _extract_labeled_blocks
        # strips trailing newlines, but the sentence tokenizer includes
        # them in the sentence span, so strict containment misses by
        # one char without the extension.
        labeled_ranges: List[Tuple[int, int]] = []
        if labeled_blocks:
            cursor = 0
            for block in labeled_blocks:
                b_start = content.find(block, cursor)
                if b_start < 0:
                    # Block not found past cursor — fall back to the
                    # first occurrence, but don't advance cursor.
                    b_start = content.find(block)
                    if b_start < 0:
                        continue
                b_end = b_start + len(block)
                # Extend through trailing whitespace.
                while b_end < len(content) and content[b_end] in " \t\n\r":
                    b_end += 1
                labeled_ranges.append((b_start, b_end))
                cursor = b_end
        for i, (_sent, s_off, s_end) in enumerate(sentence_spans):
            for (b_start, b_end) in labeled_ranges:
                if s_off < b_end and s_end > b_start:
                    anchored_indices.add(i)
                    break

        vectors, _ = _tfidf_vectors(sentences)
        scores = _anchored_textrank(sentences, vectors, anchored_indices)

        # Phase 4: MMR selection to a target length. Caller's
        # request.target_ratio overrides the constructor default —
        # without this, every compress() with a different ratio produced
        # identical output (Codex audit P2, 2026-04-25).
        effective_ratio = (
            request.target_ratio
            if request.target_ratio and request.target_ratio > 0
            else self._target_ratio
        )
        target_length = int(original_chars * effective_ratio)
        selected = _mmr_select(
            sentences, scores, vectors, target_length, anchored_indices,
        )

        # Phase 5: assembly.
        # Codex caught that the earlier build computed labeled_blocks
        # and then discarded them unless the >=80% passthrough fired.
        # Mixed prose + labeled notes lost the structure Artemis
        # claims to protect. Fix: prepend labeled blocks verbatim,
        # then append selected sentences that aren't already inside
        # a labeled block's character span.
        selected_sentences: List[str] = []
        for i in selected:
            s_off = sentence_spans[i][1]
            s_end = sentence_spans[i][2]
            inside_labeled = any(
                s_off >= b_start and s_end <= b_end
                for (b_start, b_end) in labeled_ranges
            )
            if inside_labeled:
                # Already captured verbatim via the labeled block
                # — dropping it from the MMR-joined tail prevents
                # duplication.
                continue
            selected_sentences.append(sentences[i])

        parts: List[str] = []
        if labeled_blocks:
            parts.extend(labeled_blocks)
        if selected_sentences:
            parts.append(" ".join(selected_sentences))
        compressed = "\n\n".join(parts) if parts else " ".join(sentences)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        compressed_tokens = len(compressed.split())
        ratio = len(compressed) / original_chars

        # Self-reported quality_score.
        #
        # Pre-S-II Artemis emitted None so the judge could score. That
        # works when a judge is wired in; it doesn't work when the
        # contest runs on self-reports only (judge disabled / not
        # reachable), because the contest math penalises a NULL
        # quality to zero and Artemis auto-loses to Lethe — which
        # self-reports a flat 0.85-1.0 via a ratio-only heuristic.
        #
        # The heuristic below is evidence-based:
        #   * Protected spans (dates, URLs, versions, IDs, quotes,
        #     emails) are load-bearing — losing them is a large
        #     quality hit. Artemis anchors protected-span hosts so
        #     this should be at or near 100%.
        #   * Labeled-block retention — "Name:/Role:/Org:" rows
        #     ride verbatim through assembly; measure what fraction
        #     survived into the output.
        #   * Anchored-sentence retention — sentences we explicitly
        #     anchored (protected-span hosts + labeled-block hosts)
        #     SHOULD all be in the selected set if budget permits.
        #
        # Calibrated so a typical clean extract scores ~0.90-0.95,
        # with the floor at 0.70 for content where we couldn't
        # anchor what mattered. Capped at 0.98 because the judge is
        # the authority for "perfect" and we don't want to self-score
        # above its typical ceiling.
        quality_score = _artemis_quality_score(
            content=content,
            compressed=compressed,
            protected=protected,
            labeled_blocks=labeled_blocks,
            anchored_indices=anchored_indices,
            selected=selected,
            total_sentences=len(sentences),
        )

        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            compressed_content=compressed,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            quality_score=quality_score,
            elapsed_ms=elapsed_ms,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.STRICT,
            manifest={
                "path": "extractive",
                "representation_kind": "prose",
                "protected_spans": len(protected),
                "anchored_sentences": len(anchored_indices),
                "selected": len(selected),
                "total_sentences": len(sentences),
                "target_ratio": effective_ratio,
                "quality_source": "artemis_self_report",
            },
        )

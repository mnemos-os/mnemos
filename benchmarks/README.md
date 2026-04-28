# Compression benchmarks

Real-inference benchmarks of the current compression stack
(ARTEMIS + APOLLO, with optional judge-LLM and cross-encoder
scorer). Purpose: answer the empirical question "is
APOLLO effective, and under what conditions?" with enough data
to be more than directional.

## Corpus

`compression_corpus_v3_3.jsonl` — 50 memories, stratified across
seven categories:

| Category | N | What it tests |
|---|---|---|
| portfolio | 8 | APOLLO PortfolioSchema schema-match |
| decision | 8 | APOLLO DecisionSchema match |
| person | 8 | APOLLO PersonSchema (labeled + loose forms) |
| event | 8 | APOLLO EventSchema (date + type) |
| technical | 10 | APOLLO LLM fallback (prose) |
| fact | 6 | APOLLO LLM fallback vs ARTEMIS extractive compression |
| short | 4 | min-content-length gate behavior |

Hand-curated to give each engine's default-case a fair sample.
Small enough to run through the contest in a few minutes with a
reasonable GPU; large enough that per-category aggregates are
more than anecdotal.

## Running a benchmark

```
# Point at a live inference endpoint.
python3 scripts/benchmark_compression_corpus.py \
    --corpus benchmarks/compression_corpus_v3_3.jsonl \
    --gpu-url http://localhost:8080 \
    --judge-model gemma4-consult \
    --judge-mode ensemble \
    --output /tmp/bench-$(date +%Y%m%d-%H%M%S).jsonl
```

`--judge-mode ensemble` runs LLM primary + cross-encoder secondary
so the report can analyze cross-judge correlation on the same
corpus. Requires `pip install -e '.[full]'` for
sentence-transformers.

## Reading the report

The harness auto-prints a report at end of run. To re-print from
a saved JSONL:

```
python3 scripts/benchmark_compression_corpus.py \
    --report /tmp/bench-20260424-120000.jsonl
```

Report sections:

- **Overall win distribution** — % of memories each engine won.
- **Win distribution by category** — per-category winners so you
  can see "APOLLO wins portfolio but loses decision" patterns.
- **Judge fidelity distribution** — per-engine mean/median/p5/p95.
  A judge that's harsh on ARTEMIS (p5 < 0.70 quality floor) is
  what lets APOLLO's LLM-fallback wins materialize.
- **Per-engine latency** — for Gemma-class models the floor is
  roughly microseconds to milliseconds for ARTEMIS and APOLLO schema paths,
  and ~1s+ for APOLLO fallback when it calls the GPU endpoint.
- **APOLLO execution-path breakdown** — schema-matches by schema,
  fallback invocations, parse errors.
- **Cross-judge correlation** (ensemble mode only) — Spearman +
  Pearson between the LLM judge and cross-encoder per memory.
  High correlation (> 0.8) is evidence that the cheap
  cross-encoder could eventually replace the LLM judge on the
  fast path.
- **APOLLO judge-vs-contest signal** — APOLLO outputs the judge
  rates high that still lost the contest. This measures the
  structure-reward gap in the composite_score math.

## What the benchmark DOESN'T measure

- Downstream consumption. APOLLO's whole value proposition is
  that its dense form is better for LLM-to-LLM wire use than
  prose. The benchmark measures whether the judge likes APOLLO's
  output; it does NOT measure whether a downstream LLM actually
  performs better when fed APOLLO's dense form vs ARTEMIS's extract.
  That requires a separate task-based evaluation (retrieval QA,
  agent tool-use accuracy, etc.).
- Real-corpus distribution. 50 hand-curated memories is enough to
  see engine-by-engine tendencies; not enough to predict what
  PYTHIA's real 6k-memory corpus will look like. That's a separate
  test once PYTHIA is upgradeable to v3.3.
- BPE token economy. Ratios are character-based. For downstream
  LLM context budgeting the unit that matters is BPE tokens from
  the consuming model's tokenizer.

All three are on the v3.4+ follow-up list.

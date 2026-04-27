#!/usr/bin/env python3
"""Compression-contest benchmark harness (v3.3 S-II).

Runs ARTEMIS + APOLLO on a corpus of memories against a
live GPU inference endpoint. Captures per-engine judge scores,
composite scores, winners, and latencies to JSONL for offline
analysis.

Purpose: answer the empirical question "is APOLLO effective, and
under what conditions?" — which is NOT something you can read off
a 7-memory hand-curated smoke run. Uses a stratified corpus
fixture (benchmarks/compression_corpus_v3_3.jsonl) covering
portfolio / decision / person / event / technical / fact / short
categories so per-category win rates are meaningful.

Usage

    # Run benchmark against live inference endpoint:
    python3 scripts/benchmark_compression_corpus.py \
        --corpus benchmarks/compression_corpus_v3_3.jsonl \
        --gpu-url http://localhost:8080 \
        --judge-model gemma4-consult \
        --output /tmp/bench-results.jsonl

    # Generate report from a completed run:
    python3 scripts/benchmark_compression_corpus.py \
        --report /tmp/bench-results.jsonl

    # Run with ensemble judge (LLM primary + cross-encoder secondary):
    python3 scripts/benchmark_compression_corpus.py \
        --corpus benchmarks/compression_corpus_v3_3.jsonl \
        --gpu-url http://localhost:8080 \
        --judge-model gemma4-consult \
        --judge-mode ensemble \
        --output /tmp/bench-ensemble.jsonl

Output JSONL schema

    {
      "memory_id": "bench-portfolio-001",
      "category": "portfolio",
      "content_chars": N,
      "content_tokens": N,
      "total_ms": 6000,
      "winner_engine": "apollo",
      "winner_composite": 0.67,
      "candidates": [
        {
          "engine_id": "artemis",
          "quality_score": 0.85,
          "compression_ratio": 0.40,
          "elapsed_ms": 2,
          "gpu_used": false,
          "reject_reason": null,
          "composite_score": 0.51,
          "judge_fidelity": 0.85,
          "engine_self_reported": 0.90,
          "judge_reasoning": "...",
          "schema_id": null
        },
        ...
      ]
    }

Not a unit test. Not part of pytest. Requires a live endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

# Repo-root path so the script runs whether via `-m` or direct file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compression.apollo import APOLLOEngine
from compression.artemis import ARTEMISEngine
from compression.base import CompressionRequest, IdentifierPolicy
from compression.contest import run_contest
from compression.judge import (
    CrossEncoderJudge,
    EnsembleJudge,
    LLMJudge,
    NullJudge,
)


# ── helpers ───────────────────────────────────────────────────────────────


def probe_endpoint(gpu_url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(
            f"{gpu_url.rstrip('/')}/v1/models", timeout=timeout,
        ) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


def load_corpus(path: str) -> List[dict]:
    memories = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            memories.append(json.loads(line))
    return memories


# ── one contest run per memory ─────────────────────────────────────────────


async def bench_one(memory: dict, engines, judge) -> dict:
    content = memory["content"]
    req = CompressionRequest(
        memory_id=memory["id"],
        content=content,
        owner_id="bench",
        task_type=memory.get("category", "facts"),
        identifier_policy=IdentifierPolicy.STRICT,
    )
    started = time.perf_counter()
    outcome = await run_contest(engines, req, judge=judge)
    total_ms = int((time.perf_counter() - started) * 1000)

    candidates_dump = []
    for cand in outcome.candidates:
        r = cand.result
        m = r.manifest or {}
        reasoning = m.get("judge_reasoning") or ""
        # Ensemble-mode parse: extract secondary scores from the bracket prefix.
        secondary_scores = _parse_secondary_scores(reasoning)
        clean_reasoning = re.sub(r"^\[secondaries: [^\]]*\]\s*", "", reasoning)
        candidates_dump.append({
            "engine_id": r.engine_id,
            "engine_version": r.engine_version,
            "quality_score": r.quality_score,
            "compression_ratio": r.compression_ratio,
            "elapsed_ms": r.elapsed_ms,
            "gpu_used": r.gpu_used,
            "reject_reason": cand.reject_reason,
            "composite_score": cand.composite_score,
            "speed_factor": cand.speed_factor,
            "judge_fidelity": r.quality_score if r.judge_model else None,
            "judge_model": r.judge_model,
            "engine_self_reported": m.get("engine_quality_score"),
            "judge_reasoning": clean_reasoning[:200],
            "secondary_scores": secondary_scores,
            "schema_id": m.get("schema_id"),
            "path": m.get("path"),
        })

    winner_engine = outcome.winner.result.engine_id if outcome.winner else None
    winner_composite = outcome.winner.composite_score if outcome.winner else None

    return {
        "memory_id": memory["id"],
        "category": memory.get("category", "unknown"),
        "content_chars": len(content),
        "content_tokens": len(content.split()),
        "total_ms": total_ms,
        "winner_engine": winner_engine,
        "winner_composite": winner_composite,
        "candidates": candidates_dump,
    }


def _parse_secondary_scores(reasoning: str) -> Dict[str, float]:
    """Extract 'name=0.820' pairs from an ensemble reasoning prefix."""
    m = re.match(r"^\[secondaries: ([^\]]*)\]", reasoning)
    if not m:
        return {}
    scores: Dict[str, float] = {}
    for pair in m.group(1).split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, val = pair.split("=", 1)
        try:
            scores[name.strip()] = float(val.strip())
        except ValueError:
            continue
    return scores


# ── report mode (read JSONL, print aggregates) ────────────────────────────


def print_report(path: str) -> None:
    results: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            results.append(json.loads(line))

    if not results:
        print("No results in", path)
        return

    print(f"\n{'=' * 72}")
    print(f"BENCHMARK REPORT  —  {len(results)} memories")
    print("=" * 72)

    # ── Overall win distribution ────────────────────────────────────────────
    win_counts: Dict[str, int] = {}
    no_winner = 0
    for r in results:
        w = r["winner_engine"]
        if w is None:
            no_winner += 1
        else:
            win_counts[w] = win_counts.get(w, 0) + 1
    print("\nOVERALL WIN DISTRIBUTION")
    total = len(results)
    for engine in sorted(win_counts, key=lambda e: -win_counts[e]):
        n = win_counts[engine]
        bar = "█" * int(40 * n / total)
        print(f"  {engine:>12}: {n:>3} ({100 * n / total:>5.1f}%) {bar}")
    if no_winner:
        bar = "░" * int(40 * no_winner / total)
        print(f"  {'(no winner)':>12}: {no_winner:>3} ({100 * no_winner / total:>5.1f}%) {bar}")

    # ── Per-category wins ────────────────────────────────────────────────────
    print("\nWIN DISTRIBUTION BY CATEGORY")
    by_cat: Dict[str, Dict[str, int]] = {}
    for r in results:
        cat = r["category"]
        w = r["winner_engine"] or "(none)"
        by_cat.setdefault(cat, {}).setdefault(w, 0)
        by_cat[cat][w] += 1
    for cat in sorted(by_cat):
        cnt = by_cat[cat]
        total_cat = sum(cnt.values())
        parts = ", ".join(
            f"{e}={n}" for e, n in sorted(cnt.items(), key=lambda x: -x[1])
        )
        print(f"  {cat:>12} ({total_cat}): {parts}")

    # ── Per-engine judge score distribution ──────────────────────────────────
    print("\nJUDGE FIDELITY DISTRIBUTION (successful candidates only)")
    by_engine: Dict[str, List[float]] = {}
    for r in results:
        for c in r["candidates"]:
            fid = c.get("judge_fidelity")
            if fid is None or c.get("reject_reason") == "error":
                continue
            by_engine.setdefault(c["engine_id"], []).append(fid)
    for engine in sorted(by_engine):
        vals = by_engine[engine]
        if not vals:
            continue
        mean = statistics.mean(vals)
        med = statistics.median(vals)
        p5 = _percentile(vals, 0.05)
        p95 = _percentile(vals, 0.95)
        print(
            f"  {engine:>12}: n={len(vals):>3}  "
            f"mean={mean:.3f}  med={med:.3f}  p5={p5:.3f}  p95={p95:.3f}"
        )

    # ── Per-engine latency ───────────────────────────────────────────────────
    print("\nPER-ENGINE LATENCY (ms)")
    lat_by_engine: Dict[str, List[int]] = {}
    for r in results:
        for c in r["candidates"]:
            elapsed = c.get("elapsed_ms")
            if elapsed is None:
                continue
            lat_by_engine.setdefault(c["engine_id"], []).append(elapsed)
    for engine in sorted(lat_by_engine):
        vals = lat_by_engine[engine]
        mean = statistics.mean(vals)
        med = statistics.median(vals)
        p95 = _percentile(vals, 0.95)
        max_ = max(vals)
        print(
            f"  {engine:>12}: n={len(vals):>3}  "
            f"mean={mean:>5.0f}  med={med:>5.0f}  p95={p95:>5.0f}  max={max_:>5.0f}"
        )

    # ── APOLLO schema-match breakdown ────────────────────────────────────────
    print("\nAPOLLO EXECUTION PATH BREAKDOWN")
    apollo_paths: Dict[str, int] = {}
    apollo_schemas: Dict[str, int] = {}
    apollo_errors: Dict[str, int] = {}
    for r in results:
        for c in r["candidates"]:
            if c["engine_id"] != "apollo":
                continue
            path = c.get("path") or "unknown"
            apollo_paths[path] = apollo_paths.get(path, 0) + 1
            if c.get("schema_id"):
                s = c["schema_id"]
                apollo_schemas[s] = apollo_schemas.get(s, 0) + 1
            if c.get("reject_reason") == "error" or c.get("quality_score") is None:
                err = str(c.get("reject_reason") or "no_output")
                apollo_errors[err] = apollo_errors.get(err, 0) + 1
    for path, n in sorted(apollo_paths.items(), key=lambda x: -x[1]):
        print(f"  path={path}: {n}")
    if apollo_schemas:
        print("  schema matches:")
        for s, n in sorted(apollo_schemas.items(), key=lambda x: -x[1]):
            print(f"    {s}: {n}")
    if apollo_errors:
        print("  errors:")
        for e, n in sorted(apollo_errors.items(), key=lambda x: -x[1]):
            print(f"    {e}: {n}")

    # ── Cross-judge correlation (ensemble only) ──────────────────────────────
    print("\nCROSS-JUDGE CORRELATION (ensemble mode only — LLM vs cross-encoder)")
    pairs: List[tuple] = []
    for r in results:
        for c in r["candidates"]:
            primary = c.get("judge_fidelity")
            secs = c.get("secondary_scores") or {}
            if primary is None or not secs:
                continue
            for name, val in secs.items():
                pairs.append((name, primary, val))
    if not pairs:
        print("  (no ensemble data — run with --judge-mode ensemble)")
    else:
        secondaries = sorted(set(n for n, _, _ in pairs))
        for sec_name in secondaries:
            primary_vals = [p for n, p, _ in pairs if n == sec_name]
            sec_vals = [s for n, _, s in pairs if n == sec_name]
            if len(primary_vals) < 2:
                continue
            spearman = _spearman(primary_vals, sec_vals)
            pearson = _pearson(primary_vals, sec_vals)
            print(
                f"  primary vs {sec_name}: n={len(primary_vals)}  "
                f"spearman={spearman:.3f}  pearson={pearson:.3f}"
            )

    # ── APOLLO: judge says good, contest says lose? ──────────────────────────
    print("\nAPOLLO JUDGE-vs-CONTEST SIGNAL")
    apollo_judge_high_contest_lost = 0
    apollo_won = 0
    apollo_judge_high_count = 0
    for r in results:
        apollo_cand = next((c for c in r["candidates"] if c["engine_id"] == "apollo"), None)
        if apollo_cand is None:
            continue
        if apollo_cand.get("judge_fidelity") is not None and apollo_cand["judge_fidelity"] >= 0.85:
            apollo_judge_high_count += 1
            if r["winner_engine"] != "apollo":
                apollo_judge_high_contest_lost += 1
        if r["winner_engine"] == "apollo":
            apollo_won += 1
    print(f"  APOLLO judge-fidelity >= 0.85: {apollo_judge_high_count}")
    print(f"  APOLLO won the contest:         {apollo_won}")
    print(f"  APOLLO judge-high but lost:     {apollo_judge_high_contest_lost}")
    if apollo_judge_high_count:
        lost_pct = 100 * apollo_judge_high_contest_lost / apollo_judge_high_count
        print(
            f"  → of APOLLO's judge-high outputs, {lost_pct:.1f}% lost the contest "
            f"(the composite-score / structure gap the contest math has no axis for)"
        )


def _percentile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs_sorted = sorted(xs)
    idx = max(0, min(len(xs_sorted) - 1, int(len(xs_sorted) * p)))
    return xs_sorted[idx]


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pearson(xs: List[float], ys: List[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    num = sum(a * b for a, b in zip(dx, dy))
    denx = sum(a * a for a in dx) ** 0.5
    deny = sum(b * b for b in dy) ** 0.5
    return num / (denx * deny) if denx and deny else 0.0


def _spearman(xs: List[float], ys: List[float]) -> float:
    def rank(vs):
        pairs = sorted(enumerate(vs), key=lambda p: p[1])
        ranks = [0.0] * len(vs)
        for r, (orig_i, _) in enumerate(pairs):
            ranks[orig_i] = r + 1.0
        return ranks

    return _pearson(rank(xs), rank(ys))


# ── main ───────────────────────────────────────────────────────────────────


async def _run(args) -> int:
    if args.report:
        print_report(args.report)
        return 0

    if not args.corpus:
        print("ERROR: --corpus required (or --report for report mode)", file=sys.stderr)
        return 2

    corpus = load_corpus(args.corpus)
    print(f"[info] corpus: {len(corpus)} memories from {args.corpus}", file=sys.stderr)

    if not args.skip_probe:
        probe = probe_endpoint(args.gpu_url)
        if probe is None:
            print(
                f"ERROR: could not reach {args.gpu_url}/v1/models. Use --skip-probe to override.",
                file=sys.stderr,
            )
            return 2
        models = probe.get("data") or probe.get("models") or []
        names = [m.get("id") or m.get("name") for m in models]
        print(f"[info] endpoint up; models: {names}", file=sys.stderr)

    # Default contest stack: Artemis + Apollo.
    engines = [
        ARTEMISEngine(),
        APOLLOEngine(enable_llm_fallback=True, gpu_url=args.gpu_url),
    ]

    if not args.enable_judge:
        judge = NullJudge()
    elif args.judge_mode == "cross":
        judge = CrossEncoderJudge(args.cross_encoder_model)
    elif args.judge_mode == "ensemble":
        judge = EnsembleJudge(
            primary=LLMJudge(model_id=args.judge_model, gpu_url=args.gpu_url),
            secondaries=[CrossEncoderJudge(args.cross_encoder_model)],
        )
    else:
        judge = LLMJudge(model_id=args.judge_model, gpu_url=args.gpu_url)
    print(
        f"[info] judge: {type(judge).__name__} "
        f"(mode={args.judge_mode}, model={args.judge_model!r})",
        file=sys.stderr,
    )

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        for i, mem in enumerate(corpus):
            started = time.perf_counter()
            result = await bench_one(mem, engines, judge)
            out.write(json.dumps(result) + "\n")
            out.flush()
            per = time.perf_counter() - started
            winner_str = result["winner_engine"] or "(none)"
            print(
                f"[{i + 1:>3}/{len(corpus)}] {mem['id']:>25}  "
                f"{mem['category']:>10}  winner={winner_str:>10}  "
                f"{per:.1f}s",
                file=sys.stderr,
            )
    finally:
        if args.output:
            out.close()

    # Auto-report if we wrote a file.
    if args.output:
        print("\n" + "=" * 72, file=sys.stderr)
        print(f"Results written to {args.output}", file=sys.stderr)
        print("Generating report...\n", file=sys.stderr)
        print_report(args.output)

    # Close clients.
    for eng in engines:
        if hasattr(eng, "close"):
            try:
                await eng.close()
            except Exception:
                pass
    if hasattr(judge, "close"):
        try:
            await judge.close()
        except Exception:
            pass
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--corpus", help="Path to JSONL corpus fixture.")
    p.add_argument("--report", help="Path to JSONL results — print report only, no run.")
    p.add_argument(
        "--gpu-url",
        default=os.getenv("GPU_PROVIDER_HOST_FULL", "http://localhost:8080"),
    )
    p.add_argument(
        "--judge-model",
        default=os.getenv("MNEMOS_JUDGE_MODEL", "gemma4-consult"),
    )
    p.add_argument(
        "--judge-mode",
        default="llm",
        choices=["llm", "cross", "ensemble"],
        help="Judge implementation (default: llm).",
    )
    p.add_argument(
        "--cross-encoder-model",
        default="cross-encoder/ms-marco-MiniLM-L-12-v2",
    )
    p.add_argument(
        "--enable-judge", action="store_true", default=True,
    )
    p.add_argument(
        "--no-enable-judge", dest="enable_judge", action="store_false",
    )
    p.add_argument("--output", help="JSONL output file. Stdout if omitted.")
    p.add_argument("--skip-probe", action="store_true")
    args = p.parse_args()

    try:
        rc = asyncio.run(_run(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

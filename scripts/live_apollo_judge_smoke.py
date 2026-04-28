#!/usr/bin/env python3
"""Live APOLLO + Judge smoke harness (v3.3 S-II).

Exercises the compression contest against a real GPU inference
endpoint. Runs ARTEMIS + APOLLO (the v3.3 going-forward stack) on
a small hand-curated memory sample covering every schema +
LLM-fallback + prose, with judge-LLM enabled. Captures per-engine
latencies, judge scores, winners, and prints a comparison table.

Purpose: answer "does the going-forward stack actually work on
real hardware, and how long does each stage take?" on a per-host
basis. Compare output across CERBERUS (RTX 4500 ADA, 24 GB) and
TYPHON (RTX 5060, 8 GB) — or wherever you point it.

Not a unit test. Not part of the CI suite (no MNEMOS_LIVE_E2E
gate needed — this script is explicitly manual). Requires:

  * An OpenAI-compat inference endpoint serving a Gemma4-class
    model (ollama, llama.cpp's llama-server, vLLM, or compatible).
  * Network reachability from the host running this script.
  * ``pip install -e '.[dev]'`` from the repo root.

Usage

  $ python3 scripts/live_apollo_judge_smoke.py \\
        --gpu-url http://localhost:8080 \\
        --judge-model gemma4-consult

  Output: one row per memory in a grid, plus a summary with
  aggregate timings.

The script DOES NOT touch the database — it runs run_contest()
directly against in-memory CompressionRequest objects. No queue
rows, no persist_contest, no DB connection. This keeps it a pure
compute + inference test.
"""
from __future__ import annotations

import argparse
import asyncio
import json

# Repo-root path dance so the script runs with `-m` OR as a direct file.
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemos.domain.compression.apollo import APOLLOEngine
from mnemos.domain.compression.artemis import ARTEMISEngine
from mnemos.domain.compression.base import CompressionRequest, IdentifierPolicy
from mnemos.domain.compression.contest import run_contest
from mnemos.domain.compression.judge import LLMJudge, NullJudge

# ── test corpus (hand-curated, covers each schema + fallback + prose) ──

TEST_MEMORIES: List[tuple[str, str]] = [
    (
        "portfolio_canonical",
        "Portfolio holdings: AAPL 100 shares at 150.25, now 175.50. "
        "MSFT 50 shares at 300, now 310. GOOG 20 at 120, now 135.",
    ),
    (
        "decision_simple",
        "We decided to use postgres because of the transaction guarantees, "
        "over sqlite and mongodb.",
    ),
    (
        "person_labeled",
        "Name: Alice Chen\n"
        "Role: Senior Software Engineer\n"
        "Org: Acme Corp\n"
        "Email: alice@acme.com",
    ),
    (
        "event_incident",
        "Incident on 2026-04-23. scope: compression-worker. "
        "description: stranded-running queue rows recovered via sweep.",
    ),
    (
        "fact_prose_generic",
        "Bob deployed the v3.2 release last Thursday after the CI suite "
        "passed on both Python 3.11 and 3.12. The deploy target was the "
        "primary MNEMOS host. No rollback was required.",
    ),
    (
        "short_templated",
        "git commit: fix typo in README",
    ),
    (
        "technical_howto",
        "To configure pgvector, first install the extension on your "
        "Postgres 15+ instance, then create a memories table with an "
        "embedding column typed vector(768). Index with HNSW for "
        "approximate nearest-neighbor search under 50ms.",
    ),
]


# ── probe helpers ──────────────────────────────────────────────────────


def probe_endpoint(gpu_url: str, timeout: float = 5.0) -> Optional[dict]:
    """GET {gpu_url}/v1/models. Returns parsed JSON or None on failure."""
    url = f"{gpu_url.rstrip('/')}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


# ── run ────────────────────────────────────────────────────────────────


@dataclass
class RunResult:
    label: str
    winner_engine: Optional[str]
    winner_score: Optional[float]
    engine_scores: dict   # {engine_id: (quality_after_judge, elapsed_ms, gpu_used)}
    judge_scores: dict    # {engine_id: fidelity_from_judge} — None if not scored
    total_ms: int


async def run_one(
    label: str, content: str,
    engines, judge,
) -> RunResult:
    req = CompressionRequest(
        memory_id=f"smoke:{label}",
        content=content,
        owner_id="smoke-test",
        task_type="facts",
        identifier_policy=IdentifierPolicy.STRICT,
    )
    started = time.perf_counter()
    outcome = await run_contest(engines, req, judge=judge)
    total_ms = int((time.perf_counter() - started) * 1000)

    engine_scores = {}
    judge_scores = {}
    for cand in outcome.candidates:
        r = cand.result
        engine_scores[r.engine_id] = (
            r.quality_score,
            r.elapsed_ms,
            r.gpu_used,
            cand.reject_reason,
        )
        # If the judge ran, the original engine-reported score was
        # preserved on the manifest before replacement.
        engine_self = (r.manifest or {}).get("engine_quality_score")
        judge_scores[r.engine_id] = {
            "judge_fidelity": (
                r.quality_score if r.judge_model else None
            ),
            "engine_self_reported": (
                engine_self if engine_self is not None else
                (r.quality_score if not r.judge_model else None)
            ),
            "judge_reasoning": (r.manifest or {}).get("judge_reasoning"),
        }

    winner_engine = outcome.winner.result.engine_id if outcome.winner else None
    winner_score = (
        outcome.winner.composite_score if outcome.winner else None
    )
    return RunResult(
        label=label,
        winner_engine=winner_engine,
        winner_score=winner_score,
        engine_scores=engine_scores,
        judge_scores=judge_scores,
        total_ms=total_ms,
    )


def print_row(r: RunResult) -> None:
    print(f"\n── {r.label}  ({r.total_ms}ms total) ─────────────────────")
    print(f"   winner: {r.winner_engine}  composite={r.winner_score!r}")
    for engine_id, (q, elapsed, gpu_used, reject) in r.engine_scores.items():
        js = r.judge_scores.get(engine_id, {})
        judge_f = js.get("judge_fidelity")
        engine_self = js.get("engine_self_reported")
        reason = js.get("judge_reasoning")
        gpu_flag = "gpu" if gpu_used else "cpu"
        bits = [f"{engine_id:>11}", f"{elapsed}ms", gpu_flag]
        if judge_f is not None:
            bits.append(f"judge={judge_f:.3f}")
            if engine_self is not None:
                bits.append(f"self={engine_self:.3f}")
        elif q is not None:
            bits.append(f"q={q:.3f}")
        if reject:
            bits.append(f"rejected={reject}")
        print("     " + "  ".join(bits))
        if reason:
            print(f"       judge: {reason[:80]}")


def print_summary(results: List[RunResult]) -> None:
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    wins: dict = {}
    total_time = 0
    for r in results:
        if r.winner_engine:
            wins[r.winner_engine] = wins.get(r.winner_engine, 0) + 1
        total_time += r.total_ms
    print(f"  Memories:       {len(results)}")
    print(f"  Total time:     {total_time}ms ({total_time / max(1,len(results)):.0f}ms avg)")
    print(f"  Wins by engine:")
    for engine in sorted(wins, key=lambda e: -wins[e]):
        print(f"    {engine:>11}: {wins[engine]}")
    no_winner = sum(1 for r in results if r.winner_engine is None)
    if no_winner:
        print(f"    (no winner): {no_winner}")


# ── main ───────────────────────────────────────────────────────────────


async def _main(args) -> int:
    # Probe endpoint first — fail fast if it's not reachable.
    probe = probe_endpoint(args.gpu_url)
    if probe is None and not args.skip_probe:
        print(
            f"ERROR: could not reach {args.gpu_url}/v1/models. "
            f"Is the inference server running? Pass --skip-probe to override.",
            file=sys.stderr,
        )
        return 2
    if probe:
        models = probe.get("data") or probe.get("models") or []
        names = [m.get("id") or m.get("name") for m in models]
        print(f"[ok] endpoint reachable; models: {names}")

    # Construct engines. APOLLO's enable_llm_fallback drives whether
    # it hits the endpoint on schema-less content.
    engines = [
        ARTEMISEngine(),
        APOLLOEngine(
            enable_llm_fallback=True,
            gpu_url=args.gpu_url,
        ),
    ]
    judge = (
        LLMJudge(
            model_id=args.judge_model,
            gpu_url=args.gpu_url,
        )
        if args.enable_judge
        else NullJudge()
    )

    print(f"[info] judge: {type(judge).__name__} (model={args.judge_model!r})")
    print(f"[info] corpus: {len(TEST_MEMORIES)} memories")

    results: List[RunResult] = []
    for label, content in TEST_MEMORIES:
        r = await run_one(label, content, engines, judge)
        print_row(r)
        results.append(r)

    print_summary(results)

    # Close clients — polite but not required for script exit.
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
    p.add_argument(
        "--gpu-url",
        default=os.getenv("GPU_PROVIDER_HOST_FULL", "http://localhost:8080"),
        help="OpenAI-compat inference endpoint (default: http://localhost:8080 "
             "or $GPU_PROVIDER_HOST_FULL).",
    )
    p.add_argument(
        "--judge-model",
        default=os.getenv("MNEMOS_JUDGE_MODEL", "gemma4-consult"),
        help="Model id stamped onto judge scores (default: gemma4-consult).",
    )
    p.add_argument(
        "--enable-judge",
        action="store_true",
        default=True,
        help="Run the LLMJudge (default: true).",
    )
    p.add_argument(
        "--no-enable-judge",
        dest="enable_judge",
        action="store_false",
        help="Disable judge; use engine self-reported scores only.",
    )
    p.add_argument(
        "--skip-probe",
        action="store_true",
        help="Skip the initial /v1/models reachability probe.",
    )
    args = p.parse_args()

    try:
        rc = asyncio.run(_main(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

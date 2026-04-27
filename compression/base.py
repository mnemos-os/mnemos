"""
CompressionEngine — plugin ABC for the MNEMOS v3.1 compression platform.

The ABC adapts design patterns from OpenClaw's CompactionProvider
(Apache-2.0, https://github.com/openclaw/openclaw; `src/agents/compaction.ts`):

  * `id` / `label` contract on the engine instance
  * budget-aware chunk-ratio constants (BASE_CHUNK_RATIO, MIN_CHUNK_RATIO,
    SAFETY_MARGIN, SUMMARIZATION_OVERHEAD_TOKENS)
  * identifier-preservation policy (strict / off / custom) for UUIDs,
    hashes, URLs, filenames, and code-identifier-shaped tokens
  * `previous_summary` chaining for multi-round reprocess
  * plugin-ABC shape with a single `compress` method

No code is shared with OpenClaw; only the patterns are credited. MNEMOS
compression is per-memory rather than per-conversation, and the contest
is orchestrated by compression.contest.run_contest (called from
compression.worker_contest.process_contest_queue), not by the engine
itself.

Engine instances declare their GPU intent at class level. The contest
runner uses that to route work through compression/gpu_batcher.py with
a circuit breaker and a mandatory CPU fallback path for every
gpu_optional engine.

Built-in engines: APOLLO (gpu_optional, schema-aware) and ARTEMIS
(cpu_only, extractive with identifier preservation).

Operators register additional engines at startup by adding them to
the contest engine list in distillation_worker.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


# Budget-aware defaults adapted from OpenClaw CompactionProvider.
# Individual engines may override these on a per-instance basis; the
# contest runner passes the effective target_ratio via the request.
BASE_CHUNK_RATIO: float = 0.4              # default: keep 40% of tokens
# MIN_CHUNK_RATIO floor: catch empty / degenerate output, NOT aggressive
# dense encoding. APOLLO's schema path produces ~99% reduction by design
# (500-char portfolio → 5-char "PORTFOLIO:name=..." slot), and a 0.15
# floor miscategorised those as broken (live PYTHIA contest 2026-04-24
# had APOLLO 0-wins / 60 judged because composite was always 0).
# Use a tighter floor that still catches actual empties (ratio ≈ 0
# when compressed_tokens ≤ 1) without punishing legitimate dense
# encoding. Engines that produce empty output will still bottom out
# via the compressed_tokens check and _pow_guard's base≤0 path.
MIN_CHUNK_RATIO: float = 0.001             # floor: catch empties only
SAFETY_MARGIN: float = 1.2                  # budget multiplier for over-production
SUMMARIZATION_OVERHEAD_TOKENS: int = 4096   # reservation for summarization prompt


class GPUIntent(str, Enum):
    """Declares an engine's GPU requirement.

    The gpu_batcher uses this to route work:

      * cpu_only:     never dispatched to GPU; no fallback needed.
      * gpu_optional: prefers GPU when available; MUST degrade to a CPU
                      path on circuit-breaker open or timeout.
      * gpu_required: skipped with reject_reason='disabled' when the
                      GPU endpoint is unreachable; never silently
                      degrades to CPU.
    """

    CPU_ONLY = "cpu_only"
    GPU_OPTIONAL = "gpu_optional"
    GPU_REQUIRED = "gpu_required"


class IdentifierPolicy(str, Enum):
    """Identifier-preservation policy (pattern from OpenClaw).

      * strict: UUIDs, hex hashes, URLs, filenames, and code-identifier-
                shaped tokens MUST be preserved verbatim in the output.
                Engines that cannot guarantee this MUST NOT claim it;
                the manager's judge applies a stiff quality penalty to
                output that drops flagged identifiers.
      * off:    no identifier preservation; aggressive semantic
                compression may paraphrase identifiers.
      * custom: engine-specific policy; the engine documents its rules
                in CompressionResult.manifest for audit.
    """

    STRICT = "strict"
    OFF = "off"
    CUSTOM = "custom"


@dataclass
class CompressionRequest:
    """Input to a CompressionEngine.compress() call.

    The contest worker constructs one request per memory per contest
    round and fans it out to every eligible engine via asyncio.gather.
    """

    memory_id: str
    content: str
    owner_id: str = "default"
    task_type: Optional[str] = None                         # 'reasoning', 'architecture_design', etc.
    target_ratio: float = BASE_CHUNK_RATIO
    identifier_policy: IdentifierPolicy = IdentifierPolicy.STRICT
    previous_summary: Optional[str] = None                  # multi-round reprocess chain
    scoring_profile: str = "balanced"                       # balanced | quality_first | speed_first | custom
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompressionResult:
    """Output of a CompressionEngine.compress() call.

    Engines populate the fields they can measure themselves. The manager
    computes speed_factor (normalized within the contest) and
    composite_score (scoring-profile-weighted) after collecting every
    engine's result, then writes winner + losers into
    memory_compression_candidates with reject_reason on losers.

    speed_factor and composite_score are deliberately NOT on this
    dataclass because they're contest-wide quantities, not per-engine
    properties.

    Engines signal recoverable failure by returning a result with
    `error` set rather than raising; the manager records those as
    reject_reason='error' and continues with other engines.
    """

    engine_id: str
    engine_version: str
    original_tokens: int
    compressed_tokens: Optional[int] = None
    compressed_content: Optional[str] = None
    compression_ratio: Optional[float] = None       # compressed / original
    quality_score: Optional[float] = None           # 0..1 self-reported or judge-assigned
    elapsed_ms: int = 0
    judge_model: Optional[str] = None
    gpu_used: bool = False
    identifier_policy: IdentifierPolicy = IdentifierPolicy.STRICT
    manifest: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def succeeded(self) -> bool:
        """True if the engine produced usable output.

        Used by the manager as the first filter before scoring. An
        engine that returns error=None but compressed_content=None is
        treated as no_output, not error.
        """
        return (
            self.error is None
            and self.compressed_content is not None
            and self.compression_ratio is not None
        )


class CompressionEngine(ABC):
    """Plugin ABC for MNEMOS compression engines.

    Subclasses set the class-level attributes `id`, `label`, `version`,
    and `gpu_intent`, and implement one async `compress()` method.
    The ABC's __init__ enforces that id/label are populated.

    The contest protocol:

      1. The contest worker receives a memory_compression_queue row.
      2. It builds a CompressionRequest and calls `supports(request)`
         on every registered engine; ineligible engines are skipped
         with reject_reason='disabled' logged to the candidate table.
      3. The remaining engines run concurrently via asyncio.gather.
      4. The contest runner collects CompressionResults, computes
         speed_factor and composite_score per the scoring_profile,
         applies the quality floor to disqualify damaged candidates,
         and picks the highest-scoring survivor.
      5. Winner is written to memory_compressed_variants; every
         candidate (winner + losers) lands in
         memory_compression_candidates with reject_reason set for
         losers and is_winner=TRUE on exactly one row per contest.
    """

    # Subclasses MUST override these.
    id: str = ""
    label: str = ""
    version: str = "1"
    gpu_intent: GPUIntent = GPUIntent.CPU_ONLY

    def __init__(self) -> None:
        if not self.id or not self.label:
            raise NotImplementedError(
                f"{type(self).__name__} must set class-level `id` and `label`"
            )

    @abstractmethod
    async def compress(self, request: CompressionRequest) -> CompressionResult:
        """Compress a single memory.

        The engine MUST:

          * Populate engine_id and engine_version on the result.
          * Measure its own elapsed_ms (via time.perf_counter or equivalent).
          * Set gpu_used=True only if the attempt actually reached GPU.
          * Return a CompressionResult with `error` set rather than raising
            on recoverable failure — the manager treats those as contest
            losers with reject_reason='error' and continues scoring the
            survivors.
          * Leave quality_score=None to opt into judge-model scoring by
            the manager, OR provide a self-assessed score in [0, 1].
        """
        raise NotImplementedError

    def supports(self, request: CompressionRequest) -> bool:
        """Return True if this engine is eligible for the given request.

        Default: all engines are eligible for all memories.

        Schema-aware engines (APOLLO's portfolio/decision/person/event
        schemas) SHOULD override to return False when the content does
        not match any known schema, so the manager can skip them rather
        than running them as guaranteed losers.
        """
        return True


__all__ = [
    "BASE_CHUNK_RATIO",
    "MIN_CHUNK_RATIO",
    "SAFETY_MARGIN",
    "SUMMARIZATION_OVERHEAD_TOKENS",
    "GPUIntent",
    "IdentifierPolicy",
    "CompressionRequest",
    "CompressionResult",
    "CompressionEngine",
]

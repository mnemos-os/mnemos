"""Competitive-selection orchestrator for the v3.1 compression platform.

One "contest" is one memory run through every eligible engine, with
the winner picked by composite score. Scoring is pure-Python and
side-effect-free; this module does not touch the database. DB
persistence lives in the distillation worker, which calls
persist_contest() (compression/contest_store.py) after run_contest()
returns.

Composite score (per ScoringProfile):

    composite = (q_w * quality_score)
              * (r_w * ratio_term)
              * (s_w * speed_factor)

    ratio_term  rewards reduction within [MIN_CHUNK_RATIO, 1.0). Below
                  the floor (degenerate/empty output) scores zero;
                  >= 1.0 (no compression or expanded) also zero.
                  Inside the band, ratio_term = 1 - ratio.
    speed_factor log-space compression of fastest_elapsed_ms /
                  this_elapsed_ms, bounded [SPEED_FACTOR_FLOOR, 1.0].
                  Fastest engine: 1.0. 10x slower: 0.5. 100x slower
                  or worse: SPEED_FACTOR_FLOOR. The log transform
                  prevents multiplicative speed-dominance: a 10x-
                  slower but meaningfully higher-quality engine can
                  still win under quality_first weighting, instead
                  of being crushed by a raw 0.1 linear factor.
    quality_floor applied as a pre-filter — candidates below the floor
                  are disqualified with reject_reason='quality_floor'
                  before scoring

Built-in profiles:

    balanced        q_w=1.0 r_w=1.0 s_w=1.0  floor=0.70
    quality_first   q_w=2.0 r_w=1.0 s_w=0.5  floor=0.80
    speed_first     q_w=0.8 r_w=1.0 s_w=2.0  floor=0.60

Custom profiles load from ~/.mnemos/compression_scoring.toml under a
[custom] table. Unknown profile names fall back to 'balanced' with a
warning.
"""

from __future__ import annotations

import asyncio
import logging
import math

# Python 3.11+ floor per pyproject.toml; tomllib is stdlib.
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .apollo import narrate_encoded
from .base import (
    MIN_CHUNK_RATIO,
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
)
from .judge import Judge

logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_PATH = Path.home() / ".mnemos" / "compression_scoring.toml"


# ---- scoring-profile validation bounds (v3.1.1) ----------------------------
#
# Weights multiply the per-engine sub-scores. A negative weight would
# invert the reward (worse = higher score); a huge weight would let one
# dimension swamp the others. 10x was chosen as the upper bound because
# the spread between the built-in profiles is already 4x (speed_first
# speed_weight=2.0 vs quality_first speed_weight=0.5), and 10x leaves
# room for aggressive custom profiles without letting operators
# accidentally configure a degenerate scoring function.
_WEIGHT_MIN: float = 0.0
_WEIGHT_MAX: float = 10.0

# quality_floor rejects every candidate with quality_score below the
# floor. 1.0 means "reject everything"; 0.0 means "accept anything with
# output." 0.99 upper bound prevents the mistake of setting it to 1.0
# and being surprised that no contest ever has a winner.
_QUALITY_FLOOR_MIN: float = 0.0
_QUALITY_FLOOR_MAX: float = 0.99


def _clamp(value: float, *, lo: float, hi: float, field_name: str, profile_name: str) -> float:
    """Clamp `value` into [lo, hi]. Log a warning if the value had to
    be coerced — silently clamping is rude to operators who may have
    typoed a config value.

    NaN and infinity are rejected explicitly — both values compare
    False against any numeric bound (so `<` / `>` would silently admit
    them), and propagating either into multiplicative scoring poisons
    composite_score for every candidate. We coerce to `lo` (the
    minimum) on NaN/Inf with a loud warning so the operator's misconfig
    is visible.
    """
    if math.isnan(value) or math.isinf(value):
        logger.warning(
            "scoring_profile[%s].%s = %r is not finite; coerced to %g",
            profile_name, field_name, value, lo,
        )
        return lo
    if value < lo:
        logger.warning(
            "scoring_profile[%s].%s = %g below allowed minimum %g; clamped to %g",
            profile_name, field_name, value, lo, lo,
        )
        return lo
    if value > hi:
        logger.warning(
            "scoring_profile[%s].%s = %g above allowed maximum %g; clamped to %g",
            profile_name, field_name, value, hi, hi,
        )
        return hi
    return value


def _validated_profile(
    *,
    name: str,
    quality_weight: float,
    ratio_weight: float,
    speed_weight: float,
    quality_floor: float,
) -> "ScoringProfile":
    """Construct a ScoringProfile with each field clamped to its valid
    range. A malformed custom profile still yields a usable profile
    rather than a runtime crash deep in the scoring loop."""
    return ScoringProfile(
        name=name,
        quality_weight=_clamp(
            quality_weight, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX,
            field_name="quality_weight", profile_name=name,
        ),
        ratio_weight=_clamp(
            ratio_weight, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX,
            field_name="ratio_weight", profile_name=name,
        ),
        speed_weight=_clamp(
            speed_weight, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX,
            field_name="speed_weight", profile_name=name,
        ),
        quality_floor=_clamp(
            quality_floor, lo=_QUALITY_FLOOR_MIN, hi=_QUALITY_FLOOR_MAX,
            field_name="quality_floor", profile_name=name,
        ),
    )


@dataclass(frozen=True)
class ScoringProfile:
    """Weights for the composite score.

    All weights are multiplicative. quality_floor is applied as a
    pre-filter — candidates with quality < floor are disqualified
    with reject_reason='quality_floor' and never scored.
    """

    name: str
    quality_weight: float
    ratio_weight: float
    speed_weight: float
    quality_floor: float


BUILT_IN_PROFILES: dict[str, ScoringProfile] = {
    "balanced": ScoringProfile(
        name="balanced",
        quality_weight=1.0,
        ratio_weight=1.0,
        speed_weight=1.0,
        quality_floor=0.70,
    ),
    "quality_first": ScoringProfile(
        name="quality_first",
        quality_weight=2.0,
        ratio_weight=1.0,
        speed_weight=0.5,
        quality_floor=0.80,
    ),
    "speed_first": ScoringProfile(
        name="speed_first",
        quality_weight=0.8,
        ratio_weight=1.0,
        speed_weight=2.0,
        quality_floor=0.60,
    ),
}


def load_scoring_profile(
    name: str = "balanced",
    config_path: Optional[Path] = None,
) -> ScoringProfile:
    """Resolve a profile name to a ScoringProfile.

    Built-in names ('balanced', 'quality_first', 'speed_first') return
    the pinned values. 'custom' loads from the TOML config file
    (default: ~/.mnemos/compression_scoring.toml); missing fields fall
    back to the 'balanced' values. Unknown names log a warning and
    return 'balanced'.
    """
    if name in BUILT_IN_PROFILES:
        return BUILT_IN_PROFILES[name]

    if name == "custom":
        path = config_path or _DEFAULT_CONFIG_PATH
        if not path.exists():
            logger.warning(
                "Scoring profile 'custom' requested but %s does not "
                "exist; falling back to 'balanced'",
                path,
            )
            return BUILT_IN_PROFILES["balanced"]
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            logger.warning(
                "Scoring profile config at %s is not valid TOML: %s — "
                "falling back to 'balanced'",
                path,
                exc,
            )
            return BUILT_IN_PROFILES["balanced"]

        custom = data.get("custom", {})
        base = BUILT_IN_PROFILES["balanced"]

        def _as_float(key: str, default: float) -> float:
            raw = custom.get(key, default)
            try:
                return float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "scoring_profile[custom].%s = %r is not a number; "
                    "falling back to balanced default %g",
                    key, raw, default,
                )
                return default

        return _validated_profile(
            name="custom",
            quality_weight=_as_float("quality_weight", base.quality_weight),
            ratio_weight=_as_float("ratio_weight", base.ratio_weight),
            speed_weight=_as_float("speed_weight", base.speed_weight),
            quality_floor=_as_float("quality_floor", base.quality_floor),
        )

    logger.warning("Unknown scoring profile %r; falling back to 'balanced'", name)
    return BUILT_IN_PROFILES["balanced"]


@dataclass
class ContestCandidate:
    """One engine's scored attempt in a contest round.

    speed_factor and composite_score are 0.0 for candidates that were
    rejected before scoring (disabled / error / no_output /
    quality_floor). Survivors that lose to the winner get
    reject_reason='inferior'; the winner has reject_reason=None.
    """

    result: CompressionResult
    speed_factor: float = 0.0
    composite_score: float = 0.0
    is_winner: bool = False
    reject_reason: Optional[str] = None


@dataclass
class ContestOutcome:
    """The full result of one contest round.

    Every engine considered appears in `candidates` — the ones skipped
    by supports() carry reject_reason='disabled' so the audit trail
    shows they were evaluated. `winner` is None if every candidate
    was disqualified.
    """

    contest_id: uuid.UUID
    memory_id: str
    owner_id: str
    scoring_profile: str
    candidates: List[ContestCandidate] = field(default_factory=list)
    winner: Optional[ContestCandidate] = None


# Log-space speed-factor floor (v3.1.1). A raw linear speed_factor
# (fastest / elapsed) gives a 10x-slower engine 0.1, which — multiplied
# into the composite — dominates even quality_first weighting. The log-
# space transform compresses that to 0.5 for 10x-slower and bottoms out
# at this floor for very slow engines. 0.1 lets a 1000x-slower engine
# still have some non-zero score, rather than getting zeroed out and
# classified 'inferior' just for being slow.
SPEED_FACTOR_FLOOR: float = 0.1


def _speed_factor(fastest_ms: float, elapsed_ms: float) -> float:
    """Compute speed_factor in log space, bounded [SPEED_FACTOR_FLOOR, 1.0].

    Inputs are elapsed milliseconds from the fastest contest participant
    (`fastest_ms`) and this engine (`elapsed_ms`). Both must be > 0 —
    callers with missing timing data should pass 1.0 directly (see the
    fallback branch at the call site).

    Transform: factor = 1.0 + log10(fastest_ms / elapsed_ms) / 2.0, then
    clamped to [SPEED_FACTOR_FLOOR, 1.0]. The /2 divisor spreads the
    penalty so that 100x-slower maps to 0 (which clamps to floor), 10x-
    slower to 0.5, 3.16x-slower to 0.75, and same-speed to 1.0.
    """
    if fastest_ms <= 0 or elapsed_ms <= 0:
        return 1.0
    ratio = fastest_ms / elapsed_ms
    if ratio >= 1.0:
        return 1.0  # this engine was the fastest (or tied)
    factor = 1.0 + math.log10(ratio) / 2.0
    if factor < SPEED_FACTOR_FLOOR:
        return SPEED_FACTOR_FLOOR
    return factor


def _ratio_term(ratio: Optional[float]) -> float:
    """Reward reduction within [MIN_CHUNK_RATIO, 1.0).

    Below MIN_CHUNK_RATIO the output is almost certainly degenerate
    (empty content, or a parse failure that produced ~nothing). Above
    or equal to 1.0 the engine did not compress or actively expanded.
    Both tails score zero. The operational sweet spot is a ratio in
    [0.2, 0.6] — ratio_term returns [0.4, 0.8] across that band.

    This floor catches a real regression found in live testing: when
    an LLM-assisted engine's importance-score response is unparseable,
    the adapter can silently return empty content with ratio=0.0. The
    naive "1 - ratio" would score that as maximum reward; with the
    floor applied, degenerate output lands with composite_score=0 and
    is classified 'inferior' (or lower) in the contest.
    """
    if ratio is None:
        return 0.0
    if ratio < MIN_CHUNK_RATIO or ratio >= 1.0:
        return 0.0
    return 1.0 - ratio


def _pow_guard(base: float, exp: float) -> float:
    """Return base ** exp with guards that keep composite_score sane:

      * Negative base -> 0 (no axis should produce a negative value
        in the [0, 1] normalized range we work in; negative is a bug
        upstream).
      * base == 0 and exp == 0 -> 0 (mathematical 0^0 is indeterminate;
        we treat it as "no contribution" to match the zero-kills-
        composite gate).
      * base == 0 and exp > 0  -> 0 (mathematical identity).
      * Otherwise pow(base, exp).
    """
    if base <= 0.0:
        return 0.0
    return base ** exp


def _classify_failure(result: CompressionResult) -> str:
    """Map a non-succeeded result to a DB-allowlisted reject_reason."""
    if result.error is not None:
        return "error"
    return "no_output"


async def _apply_judge_scores(
    raw_results: list[CompressionResult],
    request: CompressionRequest,
    judge: Judge,
) -> None:
    """Run ``judge`` against every succeeded result and overwrite
    ``quality_score`` with the judge's fidelity rating. Mutates
    ``raw_results`` in place.

    Preserves the engine's self-reported score under
    ``result.manifest['engine_quality_score']`` so the audit log
    records both readings. Stamps ``result.judge_model`` with the
    judge's id. Judge failure (returns None) leaves the result's
    score untouched — the contest never fails closed because the
    judge is down.

    For APOLLO candidates the dense form is narrated first (the
    judge compares against prose, not dense form); non-APOLLO
    engines ship prose output directly so no narration is needed.
    """
    for r in raw_results:
        if not r.succeeded() or r.compressed_content is None:
            continue

        # APOLLO outputs are dense — narrate to prose before scoring.
        # Other engines emit prose-shaped output already.
        if r.engine_id == "apollo":
            candidate_narrated = narrate_encoded(r.compressed_content)
        else:
            candidate_narrated = r.compressed_content

        try:
            score = await judge.score(
                original=request.content,
                candidate_encoded=r.compressed_content,
                candidate_narrated=candidate_narrated,
                candidate_engine_id=r.engine_id,
            )
        except Exception:  # noqa: BLE001 — judge MUST NOT raise upward
            logger.exception(
                "Judge %s raised scoring candidate engine=%s; falling "
                "back to engine-reported quality_score",
                type(judge).__name__, r.engine_id,
            )
            continue

        if score is None:
            # Judge returned None (unavailable, parse failure, circuit
            # open). Keep the engine's self-reported score.
            continue

        # Preserve the engine's self-reported score on the manifest so
        # the audit log carries both readings; then replace
        # quality_score with the judge's fidelity rating.
        r.manifest = dict(r.manifest or {})
        if r.quality_score is not None:
            r.manifest["engine_quality_score"] = r.quality_score
        r.manifest["judge_reasoning"] = score.reasoning
        r.quality_score = score.fidelity
        r.judge_model = score.model_id


async def run_contest(
    engines: Sequence[CompressionEngine],
    request: CompressionRequest,
    *,
    profile: Optional[ScoringProfile] = None,
    judge: Optional[Judge] = None,
) -> ContestOutcome:
    """Run every eligible engine on one memory and select a winner.

    The function:

      1. Splits engines into eligible (supports()=True) and skipped
         (supports()=False → reject_reason='disabled').
      2. Runs eligible engines concurrently via asyncio.gather, with
         return_exceptions=True so one engine's crash doesn't kill the
         contest.
      3. (v3.3 S-II, optional) If a ``judge`` is supplied, rates the
         fidelity of every succeeded candidate against the original
         memory. Replaces the engine's self-reported quality_score
         with the judge's fidelity score; stamps the candidate's
         ``judge_model`` with the judge's id; preserves the engine's
         self-reported score in the candidate manifest under
         ``engine_quality_score``. Judge failure falls back silently
         to the engine-reported score — the contest never fails
         closed because the judge is down.
      4. Normalizes speed_factor across completed results (fastest=1.0).
      5. Applies the quality floor, then scores survivors.
      6. Picks the max-composite survivor as winner; remaining
         survivors get reject_reason='inferior'.

    The ContestOutcome is pure data — the caller writes it to the
    v3.1 tables via persist_contest() (separate module).
    """

    prof = profile or load_scoring_profile(request.scoring_profile)
    contest_id = uuid.uuid4()
    outcome = ContestOutcome(
        contest_id=contest_id,
        memory_id=request.memory_id,
        owner_id=request.owner_id,
        scoring_profile=prof.name,
    )

    eligible: list[CompressionEngine] = []
    original_token_estimate = len(request.content.split())

    for eng in engines:
        try:
            is_eligible = eng.supports(request)
        except Exception:
            logger.exception("Engine %r raised in supports(); skipping", eng.id)
            is_eligible = False
        if is_eligible:
            eligible.append(eng)
        else:
            placeholder = CompressionResult(
                engine_id=eng.id,
                engine_version=eng.version,
                original_tokens=original_token_estimate,
                identifier_policy=request.identifier_policy,
            )
            outcome.candidates.append(
                ContestCandidate(
                    result=placeholder,
                    reject_reason="disabled",
                )
            )

    if not eligible:
        return outcome

    gathered = await asyncio.gather(
        *(eng.compress(request) for eng in eligible),
        return_exceptions=True,
    )

    raw_results: list[CompressionResult] = []
    for eng, res in zip(eligible, gathered):
        if isinstance(res, BaseException):
            # Only demote ordinary Exception subclasses to contest losers.
            # asyncio.CancelledError (BaseException subclass in 3.8+),
            # SystemExit, KeyboardInterrupt, GeneratorExit all bypass
            # Exception and must propagate so the event loop / process
            # manager can act on them. Swallowing them here would let a
            # shutdown signal be silently demoted to an "engine error"
            # row while the worker continues happily.
            if not isinstance(res, Exception):
                raise res
            # Preserve the engine's traceback in the log — logger.exception
            # would log the active exception context (empty here, since we
            # captured via return_exceptions=True). exc_info=res attaches
            # the original traceback so diagnostics aren't thinned out.
            logger.error(
                "Engine %r raised in compress(): %r", eng.id, res,
                exc_info=(type(res), res, res.__traceback__),
            )
            raw_results.append(
                CompressionResult(
                    engine_id=eng.id,
                    engine_version=eng.version,
                    original_tokens=original_token_estimate,
                    identifier_policy=request.identifier_policy,
                    error=f"{type(res).__name__}: {res}",
                )
            )
        else:
            raw_results.append(res)

    completed_times = [
        r.elapsed_ms for r in raw_results if r.succeeded() and r.elapsed_ms > 0
    ]
    fastest = min(completed_times) if completed_times else 0

    # v3.3 S-II: judge-LLM fidelity scoring. When supplied, run the
    # judge against every succeeded result BEFORE the quality floor is
    # applied. Judge failures fall back silently to the engine's
    # self-reported score; the engine score is preserved on the
    # result's manifest so the audit log carries both readings.
    if judge is not None:
        await _apply_judge_scores(raw_results, request, judge)

    survivors: list[ContestCandidate] = []
    for r in raw_results:
        if not r.succeeded():
            outcome.candidates.append(
                ContestCandidate(
                    result=r,
                    reject_reason=_classify_failure(r),
                )
            )
            continue

        q = r.quality_score if r.quality_score is not None else 0.5
        if q < prof.quality_floor:
            outcome.candidates.append(
                ContestCandidate(
                    result=r,
                    reject_reason="quality_floor",
                )
            )
            continue

        speed_factor = _speed_factor(fastest, r.elapsed_ms)
        ratio_term = _ratio_term(r.compression_ratio)

        # Exponentiated weights so profiles actually reshape the
        # decision boundary. The earlier multiplicative form
        # `(q_w * q) * (r_w * r) * (s_w * s)` folded into a shared
        # constant across candidates — within a contest every
        # candidate's composite got multiplied by the same
        # `q_w * r_w * s_w` factor, leaving winner ordering
        # identical across profiles. Codex flagged this in the
        # v3.3 review. The exponential form preserves the
        # "zero-in-any-axis zeros composite" property (needed for
        # the MIN_CHUNK_RATIO and speed_floor gates) while making
        # profile weights genuinely steer outcomes.
        composite = (
            _pow_guard(q, prof.quality_weight)
            * _pow_guard(ratio_term, prof.ratio_weight)
            * _pow_guard(speed_factor, prof.speed_weight)
        )
        survivors.append(
            ContestCandidate(
                result=r,
                speed_factor=speed_factor,
                composite_score=composite,
            )
        )

    # Winner eligibility also requires composite_score > 0. A zero
    # composite means "achieved nothing" — ratio_term killed it (ratio
    # at or below MIN_CHUNK_RATIO or >= 1.0) or the quality/speed
    # multipliers did. Calling such a candidate a winner triggers the
    # mcc_winner_has_output CHECK in the DB (persist coerces 0 to NULL
    # for audit clarity, and NULL composite on a winner row is invalid
    # by design). Found in the 49-memory CERBERUS drain on 2026-04-23
    # where a short memory produced ratio=1.0 -> composite=0.
    winning_pool = [c for c in survivors if c.composite_score > 0]
    for c in survivors:
        outcome.candidates.append(c)
    if winning_pool:
        winner = max(winning_pool, key=lambda c: c.composite_score)
        winner.is_winner = True
        outcome.winner = winner
        for c in winning_pool:
            if c is not winner:
                c.reject_reason = "inferior"
    # Survivors with composite == 0 fall through to 'inferior' too —
    # they were eligible (passed the quality floor, produced output)
    # but scored nothing.
    for c in survivors:
        if not c.is_winner and c.reject_reason is None:
            c.reject_reason = "inferior"

    return outcome


__all__ = [
    "BUILT_IN_PROFILES",
    "ContestCandidate",
    "ContestOutcome",
    "ScoringProfile",
    "load_scoring_profile",
    "run_contest",
]

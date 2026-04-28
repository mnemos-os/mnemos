"""Competitive-selection contest — scoring and orchestration tests.

Pure-logic checks for compression/contest.py. No DB, no real engines;
everything runs against deterministic mock engines in-process. DB
persistence (memory_compression_candidates, memory_compressed_variants)
is tested separately once the persist_contest helper lands.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from mnemos.domain.compression.base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
)
from mnemos.domain.compression.contest import (
    BUILT_IN_PROFILES,
    ContestOutcome,
    _ratio_term,
    load_scoring_profile,
    run_contest,
)

# ---- test fixtures ---------------------------------------------------------


class MockEngine(CompressionEngine):
    """Deterministic fake engine for contest tests."""

    def __init__(
        self,
        id_: str,
        *,
        quality: float = 0.9,
        ratio: float = 0.4,
        elapsed_ms: int = 50,
        error: str | None = None,
        supported: bool = True,
    ) -> None:
        self.id = id_
        self.label = f"Mock {id_}"
        self.version = "1"
        self.gpu_intent = GPUIntent.CPU_ONLY
        self._q = quality
        self._r = ratio
        self._e = elapsed_ms
        self._err = error
        self._sup = supported
        super().__init__()

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        if self._err is not None:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=100,
                elapsed_ms=self._e,
                error=self._err,
                identifier_policy=request.identifier_policy,
            )
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=100,
            compressed_tokens=int(100 * self._r),
            compressed_content="x" * int(100 * self._r),
            compression_ratio=self._r,
            quality_score=self._q,
            elapsed_ms=self._e,
            gpu_used=False,
            identifier_policy=request.identifier_policy,
        )

    def supports(self, request: CompressionRequest) -> bool:  # noqa: ARG002
        return self._sup


def _request(profile: str = "balanced") -> CompressionRequest:
    return CompressionRequest(
        memory_id="mem-1",
        content="hello world " * 50,
        scoring_profile=profile,
    )


# ---- built-in profiles -----------------------------------------------------


def test_builtin_profiles_pinned():
    # The DB CHECK constraint for scoring_profile lists these names;
    # a renamed built-in needs a migration.
    assert set(BUILT_IN_PROFILES) == {"balanced", "quality_first", "speed_first"}

    assert BUILT_IN_PROFILES["balanced"].quality_floor == 0.70
    assert BUILT_IN_PROFILES["quality_first"].quality_weight == 2.0
    assert BUILT_IN_PROFILES["quality_first"].quality_floor == 0.80
    assert BUILT_IN_PROFILES["speed_first"].speed_weight == 2.0
    assert BUILT_IN_PROFILES["speed_first"].quality_floor == 0.60


def test_load_unknown_profile_falls_back_to_balanced(caplog):
    with caplog.at_level("WARNING"):
        prof = load_scoring_profile("nonexistent")
    assert prof.name == "balanced"
    assert any("Unknown scoring profile" in rec.message for rec in caplog.records)


def test_load_custom_with_missing_config_falls_back_to_balanced():
    prof = load_scoring_profile("custom", config_path=Path("/no/such/file.toml"))
    assert prof.name == "balanced"


def test_load_custom_parses_toml():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(
            "[custom]\n"
            "quality_weight = 3.14\n"
            "ratio_weight = 0.5\n"
            "speed_weight = 0.1\n"
            "quality_floor = 0.42\n"
        )
        path = Path(fh.name)
    try:
        prof = load_scoring_profile("custom", config_path=path)
        assert prof.name == "custom"
        assert prof.quality_weight == 3.14
        assert prof.ratio_weight == 0.5
        assert prof.speed_weight == 0.1
        assert prof.quality_floor == 0.42
    finally:
        path.unlink()


def test_load_custom_partial_toml_fills_defaults_from_balanced():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nquality_weight = 5.0\n")
        path = Path(fh.name)
    try:
        prof = load_scoring_profile("custom", config_path=path)
        assert prof.quality_weight == 5.0
        # Fell back to balanced for unspecified fields
        assert prof.ratio_weight == BUILT_IN_PROFILES["balanced"].ratio_weight
        assert prof.speed_weight == BUILT_IN_PROFILES["balanced"].speed_weight
        assert prof.quality_floor == BUILT_IN_PROFILES["balanced"].quality_floor
    finally:
        path.unlink()


def test_load_custom_negative_weight_clamped_to_zero(caplog):
    """v3.1.1: out-of-range weights are clamped, not passed through."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nquality_weight = -5.0\n")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.quality_weight == 0.0
        assert any("below allowed minimum" in rec.message for rec in caplog.records)
    finally:
        path.unlink()


def test_load_custom_excessive_weight_clamped_to_max(caplog):
    """Weight above the 10.0 ceiling is clamped, with an operator-
    visible warning.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nspeed_weight = 1000.0\n")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.speed_weight == 10.0
        assert any("above allowed maximum" in rec.message for rec in caplog.records)
    finally:
        path.unlink()


def test_load_custom_quality_floor_at_one_clamped_to_point_99(caplog):
    """quality_floor = 1.0 would reject every candidate (no contest can
    ever have a winner). Clamped to 0.99 so operators aren't mystified.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nquality_floor = 1.0\n")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.quality_floor == 0.99
    finally:
        path.unlink()


def test_load_custom_nan_coerced_to_minimum(caplog):
    """NaN comparisons return False against any numeric bound, so a
    naive clamp would let NaN through and poison every composite score
    downstream. Validation must explicitly reject non-finite values.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nquality_weight = nan\n")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        # NaN coerced to the minimum (0.0 for weights).
        assert prof.quality_weight == 0.0
        assert any("not finite" in rec.message for rec in caplog.records)
    finally:
        path.unlink()


def test_load_custom_inf_coerced_to_minimum(caplog):
    """Same treatment for +inf / -inf — both poison multiplicative
    scoring. Coerce to minimum with a warning.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nspeed_weight = inf\n")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.speed_weight == 0.0
        assert any("not finite" in rec.message for rec in caplog.records)
    finally:
        path.unlink()


def test_load_custom_non_numeric_falls_back_to_balanced_default(caplog):
    """A non-numeric string for a weight should NOT crash; fall back
    to the balanced default with a warning.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write('[custom]\nratio_weight = "fast"\n')
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.ratio_weight == BUILT_IN_PROFILES["balanced"].ratio_weight
        assert any("is not a number" in rec.message for rec in caplog.records)
    finally:
        path.unlink()


def test_load_custom_invalid_toml_falls_back(caplog):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("this is not valid toml [[[")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.name == "balanced"
    finally:
        path.unlink()


# ---- ratio_term boundaries -------------------------------------------------


@pytest.mark.parametrize(
    "ratio, expected",
    [
        (None, 0.0),             # engine produced no output
        (1.0, 0.0),              # no compression
        (0.5, 0.5),
        (0.2, 0.8),              # inside the operational band
        (1.5, 0.0),              # expanded output (loser)
        # Degenerate-output floor (caught by the live-GPU test where a
        # bad importance-score response fell back to empty content
        # with ratio=0.0 and would have otherwise scored 1.0):
        (0.0, 0.0),
        # Floor set tight (0.001) so APOLLO's legitimate schema-dense
        # encoding (~99% reduction, ratio 0.002-0.01) is not
        # miscategorised as degenerate. Live PYTHIA contest
        # 2026-04-24 found APOLLO 0-wins because ratio=0.002 hit
        # the old 0.15 floor. Anything ≥ 0.001 is rewarded.
        (0.0005, 0.0),           # below floor: empty / parse failure
        (0.001, 0.999),          # AT floor: smallest real output
        (0.005, 0.995),          # APOLLO's typical dense-encoding ratio
        (0.14, 0.86),             # previously below old floor, now rewarded
        (0.15, 0.85),             # operational sweet spot
        (0.16, 0.84),             # just above (boundary sanity)
    ],
)
def test_ratio_term(ratio, expected):
    assert abs(_ratio_term(ratio) - expected) < 1e-9


# ---- contest orchestration -------------------------------------------------


def test_contest_classifies_every_engine():
    # One disabled, one errored, one below-floor, two survivors.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
        MockEngine("low_q", quality=0.50, ratio=0.2, elapsed_ms=50),
        MockEngine("broken", quality=0.9, ratio=0.5, elapsed_ms=20, error="network"),
        MockEngine("unsupp", supported=False),
    ]
    outcome = asyncio.run(run_contest(engines, _request()))

    reasons = {c.result.engine_id: c.reject_reason for c in outcome.candidates}
    assert reasons["unsupp"] == "disabled"
    assert reasons["broken"] == "error"
    assert reasons["low_q"] == "quality_floor"
    # One of the two survivors wins; the other is "inferior"
    survivor_reasons = {reasons["fast_good"], reasons["slow_great"]}
    assert None in survivor_reasons
    assert "inferior" in survivor_reasons

    # Winner is the None-reason candidate
    assert outcome.winner is not None
    assert reasons[outcome.winner.result.engine_id] is None


def test_balanced_profile_prefers_fast_even_with_lower_quality():
    # fast_good has q=0.85, r=0.4 (ratio_term=0.6), speed=1.0 -> 0.51
    # slow_great has q=0.95, r=0.3 (ratio_term=0.7), speed=10/300 -> ~0.022
    # Balanced should pick fast_good.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
    ]
    outcome = asyncio.run(run_contest(engines, _request("balanced")))
    assert outcome.winner.result.engine_id == "fast_good"


def test_quality_first_profile_rewards_quality_over_speed():
    # Under quality_first (q_weight=2, s_weight=0.5, floor=0.80), the
    # 0.50 candidate is still disqualified (floor), and between
    # fast_good (q=0.85) and slow_great (q=0.95) the latter's quality
    # advantage matters more.
    #
    # v3.1.1 speed_factor is log-space: 10/300 ≈ 0.033, log-compressed
    # to 1 + log10(0.033)/2 ≈ 0.26 (above the SPEED_FACTOR_FLOOR of 0.1).
    # composite fast_good   = (2*0.85)*(1*0.6)*(0.5*1.0)     = 0.510
    # composite slow_great  = (2*0.95)*(1*0.7)*(0.5*0.26)    = 0.173
    # fast_good still wins the 30x speed gap, but slow_great is much
    # closer than the v3.1.0 linear scoring (0.022 vs 0.173). A larger
    # quality gap at the same speed ratio could now flip the result —
    # that's the point of log-space: stop crushing slow-but-good engines.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
    ]
    outcome = asyncio.run(run_contest(engines, _request("quality_first")))
    assert outcome.winner.result.engine_id == "fast_good"


def test_speed_factor_log_space_compressed():
    # v3.1.1: speed_factor is now log-compressed rather than raw linear.
    # 10x slower maps to 0.5 (was 0.1 under linear), 100x slower maps
    # to the floor 0.1 (was 0.01). This lets quality_first weighting
    # actually reward quality against a clear-but-not-catastrophic
    # speed gap.
    engines = [
        MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10),
        MockEngine("b", quality=0.9, ratio=0.4, elapsed_ms=100),  # 10x slower
        MockEngine("c", quality=0.9, ratio=0.4, elapsed_ms=1000),  # 100x slower
    ]
    outcome = asyncio.run(run_contest(engines, _request()))
    sf = {c.result.engine_id: c.speed_factor for c in outcome.candidates if c.reject_reason in (None, "inferior")}
    assert abs(sf["a"] - 1.0) < 0.001           # fastest
    assert abs(sf["b"] - 0.5) < 0.001           # 10x slower -> 0.5
    assert abs(sf["c"] - 0.1) < 0.001           # 100x slower -> floor


def test_zero_composite_survivor_is_not_a_winner():
    # Regression from the CERBERUS 49-memory drain (2026-04-23):
    # a short memory produced ratio=1.0 (no compression), ratio_term=0,
    # composite=0. The sole survivor then "won" with composite=0,
    # which later violated the mcc_winner_has_output
    # check in the DB (persist coerces 0 to NULL; NULL on a winner
    # row is invalid). Fix: survivors with composite_score == 0
    # are not winner-eligible; they fall through to reject_reason=
    # 'inferior' and outcome.winner becomes None.
    engines = [
        MockEngine("artemis", quality=1.0, ratio=1.0, elapsed_ms=1),   # ratio_term=0 -> composite=0
        MockEngine("low_q", quality=0.50, ratio=0.3, elapsed_ms=50), # below quality_floor
    ]
    outcome = asyncio.run(run_contest(engines, _request()))
    assert outcome.winner is None
    reasons = {c.result.engine_id: c.reject_reason for c in outcome.candidates}
    assert reasons["artemis"] == "inferior"
    assert reasons["low_q"] == "quality_floor"


def test_all_fail_returns_no_winner():
    engines = [
        MockEngine("low1", quality=0.5, ratio=0.4, elapsed_ms=10),
        MockEngine("low2", quality=0.4, ratio=0.4, elapsed_ms=10),
    ]
    outcome = asyncio.run(run_contest(engines, _request()))
    assert outcome.winner is None
    assert all(c.reject_reason == "quality_floor" for c in outcome.candidates)


def test_crashing_engine_becomes_error_candidate():
    class Crashing(CompressionEngine):
        id = "crash"
        label = "Crashing"
        version = "1"

        async def compress(self, request):  # noqa: ARG002
            raise RuntimeError("kaboom")

    outcome = asyncio.run(run_contest([Crashing()], _request()))
    assert outcome.winner is None
    assert len(outcome.candidates) == 1
    c = outcome.candidates[0]
    assert c.reject_reason == "error"
    assert c.result.error is not None and "kaboom" in c.result.error


def test_empty_engine_list():
    outcome = asyncio.run(run_contest([], _request()))
    assert outcome.winner is None
    assert outcome.candidates == []


def test_supports_exception_treated_as_ineligible():
    class BadSupports(CompressionEngine):
        id = "bad_supports"
        label = "x"
        version = "1"

        async def compress(self, request):  # noqa: ARG002
            return CompressionResult(engine_id=self.id, engine_version="1", original_tokens=0)

        def supports(self, request):  # noqa: ARG002
            raise RuntimeError("boom")

    outcome = asyncio.run(run_contest([BadSupports()], _request()))
    assert outcome.winner is None
    assert outcome.candidates[0].reject_reason == "disabled"


def test_contest_id_is_unique_per_run():
    engines = [MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10)]
    out_a = asyncio.run(run_contest(engines, _request()))
    out_b = asyncio.run(run_contest(engines, _request()))
    assert out_a.contest_id != out_b.contest_id


def test_outcome_is_serializable_shape():
    # Sanity check that the ContestOutcome dataclass is well-formed.
    engines = [MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10)]
    outcome = asyncio.run(run_contest(engines, _request()))
    assert isinstance(outcome, ContestOutcome)
    assert isinstance(outcome.scoring_profile, str)
    assert isinstance(outcome.candidates, list)
    assert all(c.result is not None for c in outcome.candidates)


def test_scoring_profile_weights_actually_flip_the_winner():
    """Codex re-review regression: prove that quality_first vs balanced
    pick DIFFERENT winners under the exponentiated-weights math. The
    prior test suite only asserted same-winner across profiles which
    didn't exercise whether weights were load-bearing.

    Candidates chosen so:
        balanced (q_w=1, r_w=1, s_w=1) → fast_good wins on speed
        quality_first (q_w=2, s_w=0.5) → slow_great wins on quality
    """
    engines = [
        MockEngine("fast_good", quality=0.82, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.99, ratio=0.4, elapsed_ms=50),
    ]
    balanced_out = asyncio.run(run_contest(engines, _request("balanced")))
    quality_out = asyncio.run(run_contest(engines, _request("quality_first")))
    assert balanced_out.winner is not None
    assert quality_out.winner is not None
    assert balanced_out.winner.result.engine_id == "fast_good"
    assert quality_out.winner.result.engine_id == "slow_great"
    assert (
        balanced_out.winner.result.engine_id
        != quality_out.winner.result.engine_id
    ), "profile weights must actually flip the contest winner"

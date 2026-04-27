"""run_contest + judge-LLM integration.

Verifies the three contract guarantees:
  1. With a judge supplied, every succeeded candidate's
     quality_score is REPLACED by the judge's fidelity rating
     before composite_score is computed.
  2. Engine self-reported score is preserved on the candidate's
     manifest under ``engine_quality_score`` (audit trail carries
     both readings).
  3. Judge failure (returns None OR raises) falls back to engine
     self-reported score — the contest never fails closed.

Also exercises: judge is called for every engine including non-
APOLLO; APOLLO dense forms are narrated before being scored;
candidate.judge_model is stamped on winners.
"""
from __future__ import annotations

import asyncio


from compression.base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from compression.contest import run_contest
from compression.judge import Judge, JudgeScore, NullJudge


# ── stub engines (opt out of GPU, deterministic results) ────────────────


class _StaticEngine(CompressionEngine):
    """Engine that returns a fixed CompressionResult. Used to drive
    contest scoring without any LLM / GPU calls.

    Class-level defaults satisfy the CompressionEngine ABC's init
    check; per-instance id/label are set BEFORE super().__init__()
    so the ABC sees the instance-specific values and so two instances
    in the same contest don't share state via type(self)."""

    id = "static-stub"
    label = "static-stub (test)"
    version = "test"
    gpu_intent = GPUIntent.CPU_ONLY

    def __init__(
        self,
        *,
        id: str,
        quality: float = 0.75,
        ratio: float = 0.4,
        elapsed_ms: int = 50,
        content: str = "compressed output",
    ):
        # Per-instance attributes override class-level for this instance
        # only. super().__init__() reads self.id via MRO and sees these.
        self.id = id
        self.label = f"{id} (test stub)"
        super().__init__()
        self._quality = quality
        self._ratio = ratio
        self._elapsed = elapsed_ms
        self._content = content

    async def compress(self, request):
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            compressed_content=self._content,
            original_tokens=100,
            compressed_tokens=int(100 * self._ratio),
            compression_ratio=self._ratio,
            quality_score=self._quality,
            elapsed_ms=self._elapsed,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.OFF,
            manifest={"source": "test"},
        )


# ── test doubles for Judge ──────────────────────────────────────────────


class _FixedJudge(Judge):
    """Returns a constant fidelity for every candidate."""
    model_id = "fixed-judge"

    def __init__(self, fidelity: float, reasoning: str = "test"):
        self._fidelity = fidelity
        self._reasoning = reasoning
        self.calls: list = []

    async def score(self, **kwargs):
        self.calls.append(kwargs)
        return JudgeScore(
            fidelity=self._fidelity,
            model_id=self.model_id,
            reasoning=self._reasoning,
        )


class _NoneJudge(Judge):
    """Returns None for every candidate (unavailable / parse failure)."""
    model_id = "none-judge"

    def __init__(self):
        self.calls = 0

    async def score(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        return None


class _RaisingJudge(Judge):
    """Raises on every score() call. Contest must not propagate."""
    model_id = "raising-judge"

    async def score(self, **kwargs):  # noqa: ARG002
        raise RuntimeError("judge unreachable")


def _request(content: str = "Original memory content for testing."):
    return CompressionRequest(
        memory_id="m1",
        content=content,
        owner_id="default",
    )


# ── 1. judge score replaces engine-reported quality ────────────────────


def test_judge_replaces_engine_quality_score():
    """Every succeeded candidate carries the judge's fidelity on its
    final quality_score; composite_score is computed from judge
    score, not engine self-report."""
    # Engine reports 0.50 (would lose under balanced 0.70 floor); judge
    # rates 0.90. With judge active the candidate survives the floor
    # and wins.
    engine = _StaticEngine(id="stubA", quality=0.50, ratio=0.4, elapsed_ms=50)
    judge = _FixedJudge(fidelity=0.90)

    outcome = asyncio.run(run_contest([engine], _request(), judge=judge))

    assert outcome.winner is not None, (
        "Judge fidelity 0.90 should clear the balanced 0.70 floor"
    )
    winner = outcome.winner
    # quality_score now carries the judge's rating.
    assert winner.result.quality_score == 0.90
    # judge_model stamped on the result.
    assert winner.result.judge_model == "fixed-judge"
    # Engine self-report preserved on the manifest for audit.
    assert winner.result.manifest.get("engine_quality_score") == 0.50
    # Judge's reasoning captured too.
    assert winner.result.manifest.get("judge_reasoning") == "test"
    # Composite is computed from judge score.
    assert winner.composite_score > 0


# ── 2. without judge, engine scores drive contest unchanged ─────────────


def test_no_judge_keeps_engine_scores():
    """Contest without a judge uses engine self-reported quality scores."""
    engine = _StaticEngine(id="stubA", quality=0.85, ratio=0.3, elapsed_ms=50)

    outcome = asyncio.run(run_contest([engine], _request()))

    assert outcome.winner is not None
    winner = outcome.winner
    assert winner.result.quality_score == 0.85   # engine's own number
    assert winner.result.judge_model is None
    # No engine_quality_score on manifest (nothing was replaced).
    assert "engine_quality_score" not in (winner.result.manifest or {})


# ── 3. null judge treated like "no judge" ───────────────────────────────


def test_null_judge_keeps_engine_scores():
    engine = _StaticEngine(id="stubA", quality=0.85, ratio=0.3, elapsed_ms=50)

    outcome = asyncio.run(run_contest([engine], _request(), judge=NullJudge()))

    assert outcome.winner is not None
    # NullJudge returns None → fall through, engine score wins.
    assert outcome.winner.result.quality_score == 0.85
    # judge_model stays None — judge declined to rate this one.
    assert outcome.winner.result.judge_model is None


# ── 4. judge returns None → fall back silently ──────────────────────────


def test_judge_returning_none_falls_back_to_engine():
    """Judge unavailable / parse failure → contest uses engine
    score, never fails closed."""
    engine = _StaticEngine(id="stubA", quality=0.82, ratio=0.35, elapsed_ms=50)
    judge = _NoneJudge()

    outcome = asyncio.run(run_contest([engine], _request(), judge=judge))

    assert outcome.winner is not None
    assert outcome.winner.result.quality_score == 0.82
    assert outcome.winner.result.judge_model is None
    assert judge.calls == 1     # judge WAS called, it just said None


# ── 5. judge raises → swallowed, engine score used ──────────────────────


def test_judge_exception_falls_back_to_engine():
    """A judge that raises MUST NOT crash the contest — the whole
    point of judge-soft-failure is a never-fails-closed guarantee."""
    engine = _StaticEngine(id="stubA", quality=0.80, ratio=0.3, elapsed_ms=50)
    judge = _RaisingJudge()

    outcome = asyncio.run(run_contest([engine], _request(), judge=judge))

    assert outcome.winner is not None
    assert outcome.winner.result.quality_score == 0.80
    assert outcome.winner.result.judge_model is None


# ── 6. quality floor applied AFTER judge rescoring ──────────────────────


def test_judge_rescoring_happens_before_quality_floor():
    """Engine reports 0.50 (below 0.70 balanced floor); judge rates
    0.80. Without judge the candidate would be rejected with
    reject_reason='quality_floor'. With judge active it survives."""
    engine = _StaticEngine(id="stubA", quality=0.50, ratio=0.4, elapsed_ms=50)
    judge = _FixedJudge(fidelity=0.80)

    outcome = asyncio.run(run_contest([engine], _request(), judge=judge))

    assert outcome.winner is not None, (
        "Candidate with judge-upgraded score 0.80 should clear the floor"
    )
    # No candidate should be stamped with reject_reason='quality_floor'
    # — the judge pulled it above the floor.
    for cand in outcome.candidates:
        assert cand.reject_reason != "quality_floor", (
            "Judge-rescored candidate should not land in quality_floor rejects"
        )


def test_judge_rescoring_can_drop_engine_below_floor():
    """Engine reports 0.85 (survives); judge rates 0.40 (below floor).
    Candidate should now be rejected with reject_reason='quality_floor'."""
    engine = _StaticEngine(id="stubA", quality=0.85, ratio=0.3, elapsed_ms=50)
    judge = _FixedJudge(fidelity=0.40)

    outcome = asyncio.run(run_contest([engine], _request(), judge=judge))

    assert outcome.winner is None, (
        "Judge-downgraded candidate should fall below the floor"
    )
    assert len(outcome.candidates) == 1
    assert outcome.candidates[0].reject_reason == "quality_floor"


# ── 7. judge sees narrated form for APOLLO, prose for others ───────────


class _RecordingJudge(Judge):
    """Records the candidate_narrated argument the contest passes."""
    model_id = "recording-judge"

    def __init__(self):
        self.narrations: dict = {}

    async def score(self, *, original, candidate_encoded,
                    candidate_narrated, candidate_engine_id):
        self.narrations[candidate_engine_id] = candidate_narrated
        return JudgeScore(
            fidelity=0.80, model_id=self.model_id, reasoning="ok",
        )


def test_judge_sees_narrated_form_for_apollo_candidate():
    """APOLLO emits dense form; the contest narrates it to prose
    before handing to the judge. ARTEMIS emits prose-shaped
    output directly and pass through verbatim."""
    # An APOLLO-id engine that emits a portfolio dense form.
    apollo_stub = _StaticEngine(
        id="apollo",
        quality=0.80,
        ratio=0.2,
        elapsed_ms=80,
        content="AAPL:100@150.25/175.50:tech",
    )
    # An ARTEMIS-id engine that emits plain prose.
    artemis_stub = _StaticEngine(
        id="artemis",
        quality=0.75,
        ratio=0.4,
        elapsed_ms=10,
        content="Some extractive prose output.",
    )
    judge = _RecordingJudge()

    asyncio.run(run_contest(
        [apollo_stub, artemis_stub],
        _request(),
        judge=judge,
    ))

    # APOLLO narration should resolve the dense form into a sentence
    # containing the ticker and the prices.
    apollo_narrated = judge.narrations["apollo"]
    assert "AAPL" in apollo_narrated
    assert "150.25" in apollo_narrated
    # ARTEMIS output passes through verbatim.
    assert judge.narrations["artemis"] == "Some extractive prose output."

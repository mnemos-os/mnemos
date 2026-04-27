"""CrossEncoderJudge + EnsembleJudge (v3.3 S-II cross-encoder sidecar).

Tests exercise the two classes with mocked sentence-transformers so the
suite stays fast and deps-free. A real-model integration test against
a live CrossEncoder checkpoint lives in scripts/live_apollo_judge_smoke.py.

The Judge ABC contract matters here: score() MUST NOT raise, MUST
return None on failure (parse, model load, empty inputs), MUST clamp
fidelity into [0, 1].
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType

import pytest

from compression.judge import (
    CrossEncoderJudge,
    EnsembleJudge,
    Judge,
    JudgeScore,
)


# ── test double: a CrossEncoder stand-in that returns a canned logit ──


class _FakeCrossEncoder:
    def __init__(self, model_name, device=None):
        self.model_name = model_name
        self.device = device
        self.calls: list = []
        # Tests mutate this to control the returned score.
        self._score: float = 0.75

    def predict(self, pairs, activation_fn="sigmoid", show_progress_bar=False):
        self.calls.append({
            "pairs": pairs,
            "activation_fn": activation_fn,
        })
        return [self._score]


def _install_fake_ce(monkeypatch, score: float = 0.75) -> _FakeCrossEncoder:
    """Inject a fake sentence_transformers.CrossEncoder into the module
    import path so CrossEncoderJudge imports the stand-in."""
    fake_module = ModuleType("sentence_transformers")
    instance = _FakeCrossEncoder("fake-model")
    instance._score = score

    class _Factory:
        def __init__(self_inner, model_name, device=None):
            instance.model_name = model_name
            instance.device = device

        def predict(self_inner, *a, **kw):
            return instance.predict(*a, **kw)

    fake_module.CrossEncoder = _Factory
    # _load() calls CrossEncoder(...) and stores the result. Our _Factory
    # instances delegate to the one _FakeCrossEncoder so the test can
    # inspect .calls via the returned 'instance'.
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    return instance


# ── CrossEncoderJudge ─────────────────────────────────────────────────────


def test_cross_encoder_judge_import_error_when_missing(monkeypatch):
    """Construction must fail with a clear message if
    sentence-transformers isn't installed."""
    # Ensure the module is not cached
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    # Also block the import itself.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError) as exc:
        CrossEncoderJudge()
    msg = str(exc.value)
    assert "sentence-transformers" in msg.lower() or "sentence_transformers" in msg
    assert "[full]" in msg, (
        "error message should point operators at the optional extra"
    )


def test_cross_encoder_judge_scores_pair(monkeypatch):
    fake = _install_fake_ce(monkeypatch, score=0.82)
    judge = CrossEncoderJudge("test-ce-model")

    result = asyncio.run(judge.score(
        original="Alice joined Acme as a senior engineer.",
        candidate_encoded="PERSON:name=Alice|role=Senior Engineer|org=Acme",
        candidate_narrated="Alice is Senior Engineer at Acme.",
        candidate_engine_id="apollo",
    ))

    assert result is not None
    assert result.fidelity == 0.82
    assert result.model_id == "test-ce-model"
    assert result.reasoning == ""  # cross-encoder produces no narrative
    # The real pair passed to predict should include the narrated form,
    # not the dense encoded form (parity with LLMJudge behavior).
    assert len(fake.calls) == 1
    pair = fake.calls[0]["pairs"][0]
    assert pair[0].startswith("Alice joined Acme")
    assert pair[1].startswith("Alice is Senior Engineer")


def test_cross_encoder_judge_clamps_out_of_range(monkeypatch):
    """Raw model output outside [0, 1] must be clamped."""
    _install_fake_ce(monkeypatch, score=1.5)
    judge = CrossEncoderJudge()
    r = asyncio.run(judge.score(
        original="x", candidate_encoded="y", candidate_narrated="y",
        candidate_engine_id="artemis",
    ))
    assert r is not None
    assert r.fidelity == 1.0


def test_cross_encoder_judge_clamps_negative(monkeypatch):
    _install_fake_ce(monkeypatch, score=-0.2)
    judge = CrossEncoderJudge()
    r = asyncio.run(judge.score(
        original="x", candidate_encoded="y", candidate_narrated="y",
        candidate_engine_id="artemis",
    ))
    assert r is not None
    assert r.fidelity == 0.0


def test_cross_encoder_judge_empty_inputs_short_circuit(monkeypatch):
    fake = _install_fake_ce(monkeypatch, score=0.9)
    judge = CrossEncoderJudge()

    r1 = asyncio.run(judge.score(
        original="", candidate_encoded="y", candidate_narrated="y",
        candidate_engine_id="artemis",
    ))
    r2 = asyncio.run(judge.score(
        original="x", candidate_encoded="y", candidate_narrated="",
        candidate_engine_id="artemis",
    ))
    assert r1 is None and r2 is None
    assert fake.calls == [], "empty inputs must short-circuit before model load"


def test_cross_encoder_judge_predict_exception_returns_none(monkeypatch):
    """Model raising inside predict() → None, contest never fails closed."""
    fake_module = ModuleType("sentence_transformers")

    class _Raising:
        def __init__(self, *a, **kw):
            pass

        def predict(self, *a, **kw):
            raise RuntimeError("kaboom")

    fake_module.CrossEncoder = _Raising
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    judge = CrossEncoderJudge()
    r = asyncio.run(judge.score(
        original="x", candidate_encoded="y", candidate_narrated="y",
        candidate_engine_id="artemis",
    ))
    assert r is None


def test_cross_encoder_judge_truncates_long_inputs(monkeypatch):
    fake = _install_fake_ce(monkeypatch, score=0.5)
    judge = CrossEncoderJudge()
    long = "x" * 10000
    asyncio.run(judge.score(
        original=long, candidate_encoded=long, candidate_narrated=long,
        candidate_engine_id="artemis",
    ))
    pair = fake.calls[0]["pairs"][0]
    # Both elements of the pair truncated to 4000 chars.
    assert len(pair[0]) == 4000
    assert len(pair[1]) == 4000


# ── EnsembleJudge ─────────────────────────────────────────────────────────


class _FixedJudge(Judge):
    """Returns a constant fidelity + custom reasoning."""

    def __init__(self, fidelity: float, model_id: str, reasoning: str = ""):
        self._fidelity = fidelity
        self.model_id = model_id
        self._reasoning = reasoning
        self.calls = 0

    async def score(self, **kwargs):
        self.calls += 1
        return JudgeScore(
            fidelity=self._fidelity,
            model_id=self.model_id,
            reasoning=self._reasoning,
        )


class _NoneJudge(Judge):
    model_id = "none-judge"

    def __init__(self):
        self.calls = 0

    async def score(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        return None


class _RaisingJudge(Judge):
    model_id = "raising-judge"

    async def score(self, **kwargs):  # noqa: ARG002
        raise RuntimeError("secondary down")


def _inputs():
    return {
        "original": "some content",
        "candidate_encoded": "dense:x",
        "candidate_narrated": "prose form",
        "candidate_engine_id": "apollo",
    }


def test_ensemble_returns_primary_score():
    primary = _FixedJudge(0.83, "primary", "reasoned answer")
    secondary = _FixedJudge(0.72, "ce")
    ensemble = EnsembleJudge(primary=primary, secondaries=[secondary])

    r = asyncio.run(ensemble.score(**_inputs()))

    assert r is not None
    assert r.fidelity == 0.83  # primary's score, not secondary's
    assert r.model_id == "primary"


def test_ensemble_records_secondary_on_manifest_via_reasoning():
    primary = _FixedJudge(0.90, "primary", "primary reasoning")
    secondary = _FixedJudge(0.82, "ce-test")
    ensemble = EnsembleJudge(primary=primary, secondaries=[secondary])

    r = asyncio.run(ensemble.score(**_inputs()))

    assert r is not None
    # Secondary score piggybacks in reasoning field — bracket prefix
    # lets downstream parsers find it.
    assert "[secondaries:" in r.reasoning
    assert "ce-test=0.820" in r.reasoning
    # Primary reasoning preserved after the prefix.
    assert "primary reasoning" in r.reasoning


def test_ensemble_multiple_secondaries():
    primary = _FixedJudge(0.85, "primary")
    sec1 = _FixedJudge(0.78, "ce-1")
    sec2 = _FixedJudge(0.92, "ce-2")
    ensemble = EnsembleJudge(primary=primary, secondaries=[sec1, sec2])

    r = asyncio.run(ensemble.score(**_inputs()))
    assert r is not None
    assert "ce-1=0.780" in r.reasoning
    assert "ce-2=0.920" in r.reasoning


def test_ensemble_primary_none_whole_ensemble_none():
    """Primary failing is treated as ensemble failure. We never
    silently promote a secondary — the authoritative judge needs to
    produce a score."""
    primary = _NoneJudge()
    secondary = _FixedJudge(0.75, "ce-test")
    ensemble = EnsembleJudge(primary=primary, secondaries=[secondary])

    r = asyncio.run(ensemble.score(**_inputs()))
    assert r is None
    assert primary.calls == 1
    # Secondary not called — short-circuit on primary failure.
    # Actually: secondary SHOULDN'T be called if primary returned None.


def test_ensemble_secondary_failure_doesnt_affect_primary():
    """Secondary raising or returning None just means its score is
    absent from the manifest — primary's score still wins."""
    primary = _FixedJudge(0.88, "primary", "ok")
    bad_sec = _RaisingJudge()
    good_sec = _FixedJudge(0.79, "ce-good")
    ensemble = EnsembleJudge(
        primary=primary, secondaries=[bad_sec, good_sec],
    )

    r = asyncio.run(ensemble.score(**_inputs()))
    assert r is not None
    assert r.fidelity == 0.88
    # Bad secondary absent from reasoning; good secondary present.
    assert "raising-judge" not in r.reasoning
    assert "ce-good=0.790" in r.reasoning


def test_ensemble_no_secondaries_identity():
    """With empty secondaries, EnsembleJudge should behave as
    primary pass-through."""
    primary = _FixedJudge(0.91, "primary", "pristine reasoning")
    ensemble = EnsembleJudge(primary=primary, secondaries=[])

    r = asyncio.run(ensemble.score(**_inputs()))
    assert r is not None
    assert r.fidelity == 0.91
    # No "[secondaries:" prefix because there are none.
    assert "[secondaries:" not in r.reasoning
    assert r.reasoning == "pristine reasoning"


def test_ensemble_model_id_mirrors_primary():
    """Audit log shows the authoritative judge's id — operators
    querying by judge_model should be able to distinguish ensemble
    runs via the reasoning-field prefix without renaming every row."""
    primary = _FixedJudge(0.5, "gemma4-consult")
    ensemble = EnsembleJudge(primary=primary, secondaries=[])
    assert ensemble.model_id == "gemma4-consult"

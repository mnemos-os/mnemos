"""Unit tests for the Judge ABC + LLMJudge + _parse_judge_output.

Contest-integration tests (judge replaces engine-reported score,
judge None leaves engine score, judge raise is swallowed) live in
test_contest_judge.py.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict


from compression.judge import (
    JudgeScore,
    LLMJudge,
    NullJudge,
    _parse_judge_output,
)


def _fresh_gpu_url() -> str:
    """Unique per-test URL so the gpu_guard registry hands out a
    fresh CLOSED-state guard each time."""
    return f"http://judge-test-{uuid.uuid4().hex[:8]}:8000"


# ── parser ────────────────────────────────────────────────────────────────


def test_parse_accepts_minimal_shape():
    out = _parse_judge_output('{"fidelity": 0.85, "reasoning": "ok"}')
    assert out is not None
    assert out.fidelity == 0.85
    assert out.reasoning == "ok"


def test_parse_accepts_preamble_and_suffix():
    """LLMs often emit prose around the JSON; parser should still
    find the object."""
    out = _parse_judge_output(
        'Here is the rating:\n{"fidelity": 0.72, "reasoning": "decent"}\nDone.'
    )
    assert out is not None
    assert out.fidelity == 0.72


def test_parse_clamps_out_of_range_high():
    out = _parse_judge_output('{"fidelity": 1.5, "reasoning": "x"}')
    assert out is not None
    assert out.fidelity == 1.0


def test_parse_clamps_out_of_range_low():
    out = _parse_judge_output('{"fidelity": -0.3, "reasoning": "x"}')
    assert out is not None
    assert out.fidelity == 0.0


def test_parse_rejects_non_numeric_fidelity():
    assert _parse_judge_output('{"fidelity": "high", "reasoning": "x"}') is None


def test_parse_rejects_missing_fidelity():
    assert _parse_judge_output('{"reasoning": "x"}') is None


def test_parse_rejects_empty_input():
    assert _parse_judge_output("") is None
    assert _parse_judge_output(None) is None  # type: ignore[arg-type]


def test_parse_rejects_malformed_json():
    assert _parse_judge_output('{"fidelity": 0.8, "reasoning": ') is None


def test_parse_truncates_long_reasoning():
    long = "x" * 1000
    out = _parse_judge_output(f'{{"fidelity": 0.8, "reasoning": "{long}"}}')
    assert out is not None
    assert len(out.reasoning) <= 500


# ── NullJudge ─────────────────────────────────────────────────────────────


def test_null_judge_returns_none():
    j = NullJudge()
    out = asyncio.run(j.score(
        original="o",
        candidate_encoded="e",
        candidate_narrated="n",
        candidate_engine_id="artemis",
    ))
    assert out is None


# ── LLMJudge HTTP paths (substituted client) ──────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, response=None, raise_on_post=None):
        self._response = response
        self._raise = raise_on_post
        self.is_closed = False
        self.calls: list = []

    async def post(self, url, json=None, timeout=None):  # noqa: ARG002
        self.calls.append({"url": url, "json": json})
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        self.is_closed = True


def _install_fake_client(judge: LLMJudge, client: _FakeClient) -> None:
    async def _get(_self=judge):
        return client
    judge._get_client = _get  # type: ignore[assignment]


def test_llmjudge_success_stamps_fidelity_and_model_id():
    gpu_url = _fresh_gpu_url()
    j = LLMJudge(model_id="judge-test-1", gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({
        "choices": [{"text": '{"fidelity": 0.91, "reasoning": "preserved"}'}]
    }))
    _install_fake_client(j, fake)

    out = asyncio.run(j.score(
        original="Alice joined Acme as a senior engineer last week.",
        candidate_encoded="summary=Alice joined Acme;facts=[];entities=[];concepts=[]",
        candidate_narrated="Alice joined Acme.",
        candidate_engine_id="apollo",
    ))

    assert out is not None
    assert out.fidelity == 0.91
    assert out.model_id == "judge-test-1"   # LLMJudge stamps its own id
    assert out.reasoning == "preserved"
    assert len(fake.calls) == 1
    # The prompt must include both the original and the narrated form.
    prompt = fake.calls[0]["json"]["prompt"]
    assert "Alice joined Acme as a senior engineer" in prompt
    assert "Alice joined Acme." in prompt


def test_llmjudge_parse_failure_returns_none():
    """HTTP success with unparseable output → None (judge fails
    soft; contest falls back to engine-reported score)."""
    gpu_url = _fresh_gpu_url()
    j = LLMJudge(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "I cannot rate this."}]}))
    _install_fake_client(j, fake)

    out = asyncio.run(j.score(
        original="something",
        candidate_encoded="x",
        candidate_narrated="x",
        candidate_engine_id="artemis",
    ))
    assert out is None


def test_llmjudge_http_failure_returns_none():
    gpu_url = _fresh_gpu_url()
    j = LLMJudge(gpu_url=gpu_url)
    fake = _FakeClient(raise_on_post=RuntimeError("connection refused"))
    _install_fake_client(j, fake)

    out = asyncio.run(j.score(
        original="something",
        candidate_encoded="x",
        candidate_narrated="x",
        candidate_engine_id="artemis",
    ))
    assert out is None


def test_llmjudge_empty_inputs_short_circuit():
    """Empty original or narrated form → None without an HTTP call."""
    gpu_url = _fresh_gpu_url()
    j = LLMJudge(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "unused"}]}))
    _install_fake_client(j, fake)

    out = asyncio.run(j.score(
        original="", candidate_encoded="x", candidate_narrated="x",
        candidate_engine_id="artemis",
    ))
    assert out is None
    out = asyncio.run(j.score(
        original="x", candidate_encoded="x", candidate_narrated="",
        candidate_engine_id="artemis",
    ))
    assert out is None
    assert fake.calls == []


def test_llmjudge_circuit_open_short_circuits():
    """Pre-open the guard — judge should return None without HTTP."""
    gpu_url = _fresh_gpu_url()
    from compression.gpu_guard import get_guard
    guard = get_guard(gpu_url)
    for _ in range(10):
        asyncio.run(guard.record_failure(RuntimeError("probe fail")))

    j = LLMJudge(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "unused"}]}))
    _install_fake_client(j, fake)

    out = asyncio.run(j.score(
        original="x", candidate_encoded="x", candidate_narrated="x",
        candidate_engine_id="artemis",
    ))
    assert out is None
    assert fake.calls == []  # no HTTP attempt while circuit open


def test_llmjudge_truncates_long_prompts():
    """Very long originals / candidates should be trimmed to a
    safe upper bound (the judge's context window varies; 4000 chars
    each is a conservative cap)."""
    gpu_url = _fresh_gpu_url()
    j = LLMJudge(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({
        "choices": [{"text": '{"fidelity": 0.5, "reasoning": "x"}'}]
    }))
    _install_fake_client(j, fake)

    long = "x" * 10000
    asyncio.run(j.score(
        original=long, candidate_encoded=long, candidate_narrated=long,
        candidate_engine_id="artemis",
    ))

    prompt = fake.calls[0]["json"]["prompt"]
    # Prompt itself will be longer than 4000 because of the scaffolding,
    # but the injected original/narrated slots are capped.
    assert prompt.count("x") <= 8200  # 4000 × 2 + small buffer


# ── JudgeScore dataclass ──────────────────────────────────────────────────


def test_judge_score_construction():
    s = JudgeScore(fidelity=0.7, model_id="m", reasoning="r")
    assert s.fidelity == 0.7
    assert s.model_id == "m"
    assert s.reasoning == "r"


def test_judge_score_default_reasoning_empty():
    s = JudgeScore(fidelity=0.5, model_id="m")
    assert s.reasoning == ""

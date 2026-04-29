"""Judge-LLM fidelity scoring (v3.3 S-II).

Replaces engine self-reported ``quality_score`` values in the
compression contest with a judge-rated fidelity score. The judge is
a separate LLM call that compares the narrated form of each
compressed candidate against the root memory and returns a score in
[0, 1]. This is what lets APOLLO's LLM fallback (currently pinned
at 0.55 below the quality floors) actually win contests on fact-shaped
content where its dense encoding preserves meaning.

Design

  * ``Judge`` ABC — single async ``score()`` method. Callers supply
    (original, candidate dense form, candidate narrated form,
    engine id). Return is a ``JudgeScore`` or ``None`` on failure.
  * ``LLMJudge`` — GPU-backed concrete judge. httpx scaffolding
    against ``GPU_PROVIDER_HOST``; GPUGuard
    integration for circuit-open short-circuit; JSON parse with
    strict shape checking; None on malformed output.
  * ``NullJudge`` — no-op for disabled / test paths. Used as the
    default when the contest runs without a configured judge.

Integration point is ``mnemos.domain.compression.contest.run_contest(judge=...)``:
when a judge is supplied, every surviving candidate gets its
``quality_score`` replaced by the judge's fidelity rating BEFORE
composite_score is computed. The engine's self-reported score is
preserved in the candidate's manifest under ``engine_quality_score``
for audit clarity. The judge's model_id is stamped on
``CompressionResult.judge_model`` so the audit trail records which
judge scored which candidate.

On judge failure (HTTP error, parse failure, circuit-open), the
candidate falls back to its engine self-reported score — the
contest never fails closed because the judge is down.
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from mnemos.core.config import get_settings

from .gpu_guard import get_guard

logger = logging.getLogger(__name__)


# GPU provider endpoint for APOLLO fallback and judge calls.
_PROVIDER_SETTINGS = get_settings().providers
_GPU_PROVIDER_HOST = _PROVIDER_SETTINGS.gpu_provider_host
_GPU_PROVIDER_PORT = _PROVIDER_SETTINGS.gpu_provider_port
_GPU_PROVIDER_TIMEOUT = _PROVIDER_SETTINGS.gpu_provider_timeout


@dataclass
class JudgeScore:
    """Output of Judge.score().

    fidelity is in [0, 1]; 1.0 = perfect preservation of meaning,
    0.0 = compressed form unrelated to original. model_id records
    which model produced the score (stamped onto
    CompressionResult.judge_model for the audit log). reasoning is
    a short free-text justification the judge emits alongside the
    score; persisted into the candidate manifest but not used for
    scoring arithmetic.
    """

    fidelity: float
    model_id: str
    reasoning: str = ""


class Judge(ABC):
    """Base class for fidelity judges."""

    model_id: str = ""

    @abstractmethod
    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,
        candidate_narrated: str,
        candidate_engine_id: str,
    ) -> Optional[JudgeScore]:
        """Return a fidelity score [0, 1] for candidate against
        original. ``None`` signals judge failure — callers fall back
        to the engine's self-reported quality score; the contest
        never fails closed because the judge is down.

        Implementations MUST:
          * Not raise on infrastructure failures (return None, log).
          * Clamp any numeric output to [0, 1].
          * Stamp ``model_id`` on every returned JudgeScore.
        """
        raise NotImplementedError


class NullJudge(Judge):
    """No-op judge. Returns None for every candidate so the contest
    keeps using engine self-reported scores. Used as the default
    when ``MNEMOS_JUDGE_ENABLED`` is off."""

    model_id = "null"

    async def score(self, **kwargs) -> Optional[JudgeScore]:  # noqa: ARG002
        return None


# Strict shape for the judge's one-line JSON output.
# { "fidelity": 0.85, "reasoning": "brief" }
_JUDGE_OUTPUT_RE = re.compile(r"\{[^{}]*?\"fidelity\"[^{}]*?\}", re.DOTALL)


_JUDGE_PROMPT = """\
You are rating how faithfully a compressed memory preserves the meaning of the original.

Original memory:
{original}

Compressed memory (rendered back to prose for comparison):
{narrated}

Rate fidelity on a 0.0 to 1.0 scale:
  1.0 — all facts, identifiers, numbers, and nuance preserved
  0.8 — most content preserved, minor losses
  0.5 — partial preservation, some meaningful content lost
  0.2 — major content lost or distorted
  0.0 — compressed form does not reflect the original

Output ONE line of valid JSON, exactly this shape, no prose around it:
{{"fidelity": <float>, "reasoning": "<one-sentence justification>"}}

Output:"""


class LLMJudge(Judge):
    """GPU-backed fidelity judge.

    Uses the same ``GPU_PROVIDER_HOST`` endpoint as APOLLO's LLM
    fallback. The circuit breaker is shared, so one open-circuit
    signal skips the judge alongside other GPU-consuming work and a GPU
    outage degrades the contest to engine self-reported scoring
    rather than falling over entirely.
    """

    def __init__(
        self,
        model_id: str = "judge-default",
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
    ) -> None:
        self.model_id = model_id
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,  # noqa: ARG002 — part of the Judge ABC
        candidate_narrated: str,
        candidate_engine_id: str,
    ) -> Optional[JudgeScore]:
        if not original or not candidate_narrated:
            return None

        guard = get_guard(self.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            logger.info(
                "LLMJudge: circuit open for %s (%s); falling back to engine "
                "self-reported score for candidate engine=%s",
                self.gpu_url, guard.state.value, candidate_engine_id,
            )
            return None

        started = time.perf_counter()
        prompt = _JUDGE_PROMPT.format(
            original=original[:4000],
            narrated=candidate_narrated[:4000],
        )
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": prompt,
                    "max_tokens": 200,
                    "temperature": 0.0,  # deterministic scoring
                    "top_p": 1.0,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            raw = (
                payload.get("choices", [{}])[0].get("text", "")
                if isinstance(payload, dict) else ""
            ).strip()
        except Exception as exc:
            logger.warning(
                "LLMJudge: HTTP call failed for candidate engine=%s: %s",
                candidate_engine_id, exc,
            )
            await guard.record_failure(exc, probe_token=probe_token)
            return None

        # HTTP 2xx received — signal success to the guard regardless
        # of whether the output parses (parse failure is a
        # prompt/model issue, not a GPU-health issue).
        await guard.record_success(probe_token=probe_token)

        parsed = _parse_judge_output(raw)
        if parsed is None:
            logger.warning(
                "LLMJudge: output parse failed for candidate engine=%s; "
                "raw=%r (first 200 chars)",
                candidate_engine_id, raw[:200],
            )
            return None

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "LLMJudge: scored candidate engine=%s fidelity=%.3f in %dms",
            candidate_engine_id, parsed.fidelity, elapsed_ms,
        )
        return JudgeScore(
            fidelity=parsed.fidelity,
            model_id=self.model_id,
            reasoning=parsed.reasoning,
        )


def _parse_judge_output(raw: str) -> Optional[JudgeScore]:
    """Parse a judge's one-line JSON output. Accepts preamble/suffix
    by extracting the first JSON object that contains a "fidelity"
    key. Clamps fidelity into [0, 1]."""
    if not raw:
        return None
    match = _JUDGE_OUTPUT_RE.search(raw)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError):
        return None
    fidelity = obj.get("fidelity")
    if not isinstance(fidelity, (int, float)):
        return None
    # Clamp to [0, 1] — a judge that returns 1.5 or -0.2 is honest
    # in intent but out-of-range for the contest's scoring math.
    clamped = max(0.0, min(1.0, float(fidelity)))
    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    # model_id is stamped by the caller (LLMJudge knows its own id);
    # _parse_judge_output returns a bare JudgeScore for the parsing
    # layer only. The Judge implementation re-wraps with its model_id.
    return JudgeScore(fidelity=clamped, model_id="", reasoning=reasoning[:500])


# ── CrossEncoderJudge — specialized small-model scorer ────────────────────
#
# Purpose-built reranker / STS models are 20–500M params — 10–100× smaller
# than an LLM judge. They produce a scalar similarity score directly
# instead of free-form reasoning + JSON, so they're dramatically faster
# (<50ms CPU) but lose the audit-trail narrative an LLM judge produces.
#
# Design choice: keep LLMJudge as the authoritative judge for scoring
# (reasoning narrative matters); use CrossEncoderJudge either stand-alone
# when latency dominates, or as the secondary scorer inside EnsembleJudge
# to gather correlation telemetry across two independent measurement
# paths. When the LLM and cross-encoder agree on most memories, that's
# evidence the LLM judge can eventually be relegated to disagreement-
# review mode. Until we have that evidence, the LLM stays primary.


_DEFAULT_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-12-v2"


class CrossEncoderJudge(Judge):
    """Sentence-Transformers CrossEncoder as a fidelity scorer.

    Loads the cross-encoder model lazily on first score() call. Runs
    on CPU by default (the default model is 33M params, <50ms per
    pair on any modern CPU). Normalizes the raw logit output via
    sigmoid so the fidelity lands in [0, 1] and is directly
    comparable to an LLM judge's fidelity rating.

    The cross-encoder sees (original, candidate_narrated) as a pair.
    For APOLLO candidates the contest narrates the dense form first
    (same plumbing LLMJudge uses); prose-shaped candidates are passed
    through. That's consistent with LLMJudge's behavior — both judges
    score the SAME narrated pair.

    Reasoning is empty: cross-encoders produce no narrative. Callers
    that need a reason should use LLMJudge primary; CrossEncoderJudge
    is deliberately a thin numeric scorer.

    Soft-optional dependency: if sentence-transformers is not
    installed, construction raises ImportError with a clear message
    pointing at the `full` extra.
    """

    model_id: str = "cross-encoder"

    def __init__(
        self,
        model_name: str = _DEFAULT_CROSS_ENCODER_MODEL,
        *,
        device: Optional[str] = None,
        activation_fn: str = "sigmoid",
    ) -> None:
        try:
            from sentence_transformers import CrossEncoder  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "CrossEncoderJudge requires sentence-transformers. "
                "Install with: pip install 'mnemos-os[full]'"
            ) from exc
        self.model_name = model_name
        self.model_id = model_name
        self._device = device  # None means auto-detect (CPU default)
        self._activation_fn = activation_fn
        # Model loaded on first score() call — construction itself is
        # cheap and shouldn't block worker startup.
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import CrossEncoder
        kwargs = {}
        if self._device is not None:
            kwargs["device"] = self._device
        self._model = CrossEncoder(self.model_name, **kwargs)
        logger.info(
            "CrossEncoderJudge loaded model=%r device=%r",
            self.model_name, getattr(self._model, "device", self._device),
        )
        return self._model

    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,  # noqa: ARG002 — part of the Judge ABC
        candidate_narrated: str,
        candidate_engine_id: str,  # noqa: ARG002
    ) -> Optional[JudgeScore]:
        if not original or not candidate_narrated:
            return None
        try:
            model = self._load()
            # CrossEncoder.predict is synchronous + CPU-bound; offload to
            # the default executor so the contest's asyncio.gather doesn't
            # block. Small overhead for the <50ms call is fine.
            import asyncio
            raw = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: model.predict(
                    [(original[:4000], candidate_narrated[:4000])],
                    activation_fn=self._activation_fn,
                    show_progress_bar=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — judge MUST NOT crash the contest
            logger.warning(
                "CrossEncoderJudge: score failed (%s): %s",
                type(exc).__name__, exc,
            )
            return None

        # raw is a 1-element numpy array or list; coerce to float.
        try:
            score = float(raw[0])
        except (TypeError, IndexError, ValueError):
            return None
        # Clamp to [0, 1]. With sigmoid activation the score is already
        # in range; with activation_fn=None it's a logit and needs
        # sigmoid — but we default activation to sigmoid.
        fidelity = max(0.0, min(1.0, score))
        return JudgeScore(
            fidelity=fidelity,
            model_id=self.model_id,
            reasoning="",  # cross-encoders produce no narrative
        )


# ── EnsembleJudge — primary + secondary with correlation telemetry ───────


class EnsembleJudge(Judge):
    """Wrap a primary Judge + one or more secondary judges.

    The primary judge's fidelity score drives the contest's
    quality_score; secondary judges' scores are captured on the
    candidate's manifest under ``judge_secondary[<model_id>]`` for
    later correlation analysis.

    Use case: run LLMJudge primary (authoritative, produces reasoning)
    alongside CrossEncoderJudge secondary (fast telemetry). Over a
    corpus, compare the two distributions. If agreement is high,
    that's the evidence for eventually promoting the cross-encoder
    to the fast path.

    The primary's failure (returns None) is treated as a whole-
    ensemble failure — we don't silently promote a secondary.
    Failure modes in the secondary are logged but don't affect the
    returned score; the manifest just lacks that secondary's entry.
    """

    model_id: str = "ensemble"

    def __init__(
        self,
        primary: Judge,
        secondaries: Optional[List[Judge]] = None,
    ) -> None:
        self._primary = primary
        self._secondaries: List[Judge] = secondaries or []
        self.model_id = primary.model_id  # audit log shows the authoritative id

    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,
        candidate_narrated: str,
        candidate_engine_id: str,
    ) -> Optional[JudgeScore]:
        primary_score = await self._primary.score(
            original=original,
            candidate_encoded=candidate_encoded,
            candidate_narrated=candidate_narrated,
            candidate_engine_id=candidate_engine_id,
        )
        if primary_score is None:
            return None

        # Run secondaries for telemetry. Each runs independently; a
        # single secondary failure does not affect the returned score
        # or the other secondaries' captures.
        secondary_scores: Dict[str, float] = {}
        for sec in self._secondaries:
            try:
                s = await sec.score(
                    original=original,
                    candidate_encoded=candidate_encoded,
                    candidate_narrated=candidate_narrated,
                    candidate_engine_id=candidate_engine_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EnsembleJudge: secondary %s raised: %s",
                    type(sec).__name__, exc,
                )
                continue
            if s is not None:
                secondary_scores[sec.model_id or type(sec).__name__] = s.fidelity

        # Piggyback secondaries in the reasoning field as a structured
        # prefix — the contest's _apply_judge_scores writes
        # ``judge_reasoning`` onto the candidate manifest, so this
        # gives operators access to secondary scores without a schema
        # change. Format: "[secondaries: {name=0.91, ...}] <primary reasoning>"
        # Backwards-compatible consumers read the primary reasoning from
        # after the bracket; new consumers parse the prefix.
        if secondary_scores:
            suffix = ",".join(
                f"{name}={val:.3f}" for name, val in secondary_scores.items()
            )
            primary_score = JudgeScore(
                fidelity=primary_score.fidelity,
                model_id=primary_score.model_id,
                reasoning=(
                    f"[secondaries: {suffix}] {primary_score.reasoning}"
                ),
            )
        return primary_score

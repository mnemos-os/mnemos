"""persist_contest() — DB-write contract tests.

Mock-based checks for compression/contest_store.py: the function
opens one transaction, inserts one row per candidate (winner AND
losers), upserts the variant iff there's a winner, and passes the
winner's returned candidate id as winner_candidate_id.

Live-DB coverage lives separately (the migration dry-run on TYPHON
already validates the SQL against a real pgvector container).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from mnemos.domain.compression.base import (
    CompressionResult,
    IdentifierPolicy,
)
from mnemos.domain.compression.contest import ContestCandidate, ContestOutcome
from mnemos.domain.compression.contest_store import persist_contest


def _result(engine_id: str, *, content: str | None = "x" * 40, ratio: float | None = 0.4,
            quality: float | None = 0.9, elapsed_ms: int = 50,
            error: str | None = None, gpu_used: bool = False) -> CompressionResult:
    return CompressionResult(
        engine_id=engine_id,
        engine_version="1",
        original_tokens=100,
        compressed_tokens=int(100 * ratio) if (ratio is not None and content is not None) else None,
        compressed_content=content,
        compression_ratio=ratio,
        quality_score=quality,
        elapsed_ms=elapsed_ms,
        judge_model=None,
        gpu_used=gpu_used,
        identifier_policy=IdentifierPolicy.STRICT,
        manifest={"engine": engine_id},
        error=error,
    )


def _make_conn():
    """Build an AsyncMock connection whose .transaction() is an async ctx."""
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    # fetchrow returns {"id": <uuid>} for every INSERT
    def _gen_row(*args, **kwargs):
        return {"id": uuid.uuid4()}
    conn.fetchrow = AsyncMock(side_effect=_gen_row)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def _make_outcome(winner_engine_id: str | None = "fast_good") -> ContestOutcome:
    candidates = [
        ContestCandidate(
            result=_result("unsupp"),
            reject_reason="disabled",
        ),
        ContestCandidate(
            result=_result("broken", content=None, ratio=None, quality=None, error="boom"),
            reject_reason="error",
        ),
        ContestCandidate(
            result=_result("low_q", quality=0.50),
            reject_reason="quality_floor",
        ),
        ContestCandidate(
            result=_result("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
            speed_factor=1.0,
            composite_score=0.51,
            is_winner=(winner_engine_id == "fast_good"),
            reject_reason=None if winner_engine_id == "fast_good" else "inferior",
        ),
        ContestCandidate(
            result=_result("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
            speed_factor=10 / 300,
            composite_score=0.022,
            is_winner=(winner_engine_id == "slow_great"),
            reject_reason=None if winner_engine_id == "slow_great" else "inferior",
        ),
    ]
    winner = next((c for c in candidates if c.is_winner), None)
    return ContestOutcome(
        contest_id=uuid.uuid4(),
        memory_id="mem-1",
        owner_id="alice",
        scoring_profile="balanced",
        candidates=candidates,
        winner=winner,
    )


# ---- happy path ------------------------------------------------------------


def test_happy_path_writes_every_candidate_and_upserts_variant():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    result = asyncio.run(persist_contest(conn, outcome))

    # persist_contest no longer opens its own transaction — caller
    # (worker_contest._process_one) owns the transaction boundary so
    # persistence + queue-finalization commit atomically. See the
    # test_contest_store docstring and worker_contest.py for the
    # caller-owns-transaction contract.
    assert conn.transaction.call_count == 0

    # One fetchrow per candidate (5 total)
    assert conn.fetchrow.await_count == 5
    # One execute for the variant upsert
    assert conn.execute.await_count == 1

    assert result["candidates_written"] == 5
    assert result["variant_written"] is True
    assert result["winner_engine"] == "fast_good"


def test_no_winner_skips_variant_upsert():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id=None)
    # Mark every candidate as non-winner via reject_reason
    for c in outcome.candidates:
        c.is_winner = False
        c.reject_reason = c.reject_reason or "quality_floor"
    outcome.winner = None

    result = asyncio.run(persist_contest(conn, outcome))

    assert conn.fetchrow.await_count == 5
    assert conn.execute.await_count == 0  # no variant upsert
    assert result["variant_written"] is False
    assert result["winner_engine"] is None


# ---- argument shape --------------------------------------------------------


def test_candidate_insert_positional_args_include_all_fields():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    # Every INSERT has 19 positional args (see _INSERT_CANDIDATE_SQL).
    for call_args in conn.fetchrow.call_args_list:
        args = call_args.args
        # arg[0] is the SQL, then 19 bind parameters
        assert len(args) == 1 + 19, f"expected 20 positional args, got {len(args)}"


def test_variant_upsert_uses_winning_candidate_id():
    conn = _make_conn()
    winner_id = uuid.uuid4()

    # Override fetchrow to return a deterministic id for the winner
    def _side(*args, **kwargs):
        # args[0] is SQL; args[1] memory_id; args[4] engine_id ... cand.is_winner is args[17]
        is_winner = args[17]
        return {"id": winner_id if is_winner else uuid.uuid4()}
    conn.fetchrow = AsyncMock(side_effect=_side)

    outcome = _make_outcome(winner_engine_id="fast_good")
    asyncio.run(persist_contest(conn, outcome))

    # Variant upsert's $3 is winner_candidate_id
    assert conn.execute.await_count == 1
    variant_args = conn.execute.call_args.args
    assert variant_args[3] == winner_id, (
        f"variant_candidate_id should match winner's id; got {variant_args[3]!r}"
    )


def test_manifest_serialized_as_json_string():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    # manifest is arg position 19 (1 SQL + 18 params before it = index 19)
    for call_args in conn.fetchrow.call_args_list:
        manifest_arg = call_args.args[19]
        assert isinstance(manifest_arg, str)
        # Must round-trip as JSON
        parsed = json.loads(manifest_arg)
        assert isinstance(parsed, dict)
        # Engine-provided keys survive enrichment
        assert parsed.get("engine") == call_args.args[4]


# ---- manifest enrichment (v3.1.1) -----------------------------------------


def _manifest_for_engine(conn, engine_id: str) -> dict:
    """Pull the serialized manifest JSON for a given engine_id and
    decode it back to a dict.
    """
    for call_args in conn.fetchrow.call_args_list:
        if call_args.args[4] == engine_id:
            return json.loads(call_args.args[19])
    raise AssertionError(f"no insert for engine_id={engine_id!r}")


def test_errored_candidate_gets_error_in_audit_block():
    """A candidate with reject_reason='error' and result.error set must
    have the error text persisted into manifest._audit.error. Without
    this, the DB only records the 'error' bucket label — the actual
    exception message lives only in logs.
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "broken")
    assert "_audit" in manifest
    audit = manifest["_audit"]
    assert audit["reject_reason"] == "error"
    assert audit["error"] == "boom"
    assert audit["engine_version"] == "1"


def test_quality_floor_candidate_records_quality_score():
    """quality_floor rejections drop the below-floor quality_score into
    obscurity — the typed quality_score column captures it, but
    operators scanning for near-misses want the value in the manifest
    audit block too.
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "low_q")
    audit = manifest["_audit"]
    assert audit["reject_reason"] == "quality_floor"
    assert audit["quality_score"] == 0.50


def test_disabled_candidate_has_no_elapsed_or_gpu_fields():
    """A 'disabled' candidate (supports()=False) never ran, so
    elapsed_ms and gpu_used shouldn't pollute the audit block.
    The 'unsupp' fixture has elapsed_ms=50, so it WILL get those
    fields; this test uses a dedicated candidate.
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")
    # Force one candidate to look genuinely disabled (never dispatched)
    disabled = ContestCandidate(
        result=CompressionResult(
            engine_id="truly_disabled",
            engine_version="1",
            original_tokens=100,
            elapsed_ms=0,
            gpu_used=False,
        ),
        reject_reason="disabled",
    )
    outcome.candidates.insert(0, disabled)

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "truly_disabled")
    audit = manifest["_audit"]
    assert audit["reject_reason"] == "disabled"
    assert "elapsed_ms" not in audit
    assert "gpu_used" not in audit


def test_inferior_candidate_records_its_scores():
    """A candidate that lost on composite score ('inferior') records
    its achieved ratio + quality + elapsed for post-hoc comparison
    with the winner.
    """
    conn = _make_conn()
    # Two-winner configs: fast_good wins, slow_great is 'inferior'
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "slow_great")
    audit = manifest["_audit"]
    assert audit["reject_reason"] == "inferior"
    assert audit["quality_score"] == 0.95
    assert audit["compression_ratio"] == 0.3
    assert audit["elapsed_ms"] == 300
    assert audit["gpu_used"] is False


def test_winner_manifest_is_not_enriched():
    """Winners already have every useful field in typed columns —
    enrichment would clutter their manifest with redundant data.
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "fast_good")
    assert "_audit" not in manifest
    # Engine-provided keys still present.
    assert manifest.get("engine") == "fast_good"


def test_engine_authored_audit_block_not_clobbered():
    """Defensive: if an engine populated `_audit.*` intentionally, we
    use setdefault — we ADD fields but never overwrite. (Unlikely but
    guarded to keep the contract clean.)
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    # Pre-populate the 'broken' candidate's manifest with an engine-
    # authored _audit.error value different from result.error.
    for c in outcome.candidates:
        if c.result.engine_id == "broken":
            c.result.manifest = {
                "engine": "broken",
                "_audit": {"error": "engine_authored_error"},
            }

    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "broken")
    # Engine-authored 'error' value wins over result.error='boom'.
    assert manifest["_audit"]["error"] == "engine_authored_error"
    # But new audit keys still get added.
    assert manifest["_audit"]["reject_reason"] == "error"


def test_non_dict_audit_from_engine_does_not_crash():
    """Pathological: an engine returns manifest['_audit'] as a string
    (or list, or any non-dict). Enrichment must not raise; instead
    it stashes the engine's value in _audit_original and builds a
    fresh dict.
    """
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    for c in outcome.candidates:
        if c.result.engine_id == "broken":
            c.result.manifest = {
                "engine": "broken",
                "_audit": "not a dict — pathological engine output",
            }

    # Should not raise
    asyncio.run(persist_contest(conn, outcome))

    manifest = _manifest_for_engine(conn, "broken")
    assert isinstance(manifest["_audit"], dict)
    assert manifest["_audit"]["reject_reason"] == "error"
    assert manifest["_audit_original"] == "not a dict — pathological engine output"


def test_judge_model_fallback_applied_when_result_is_silent():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")
    # Explicitly clear every result's judge_model
    for c in outcome.candidates:
        c.result.judge_model = None

    asyncio.run(persist_contest(conn, outcome, judge_model="gemma4:e2b"))

    # judge_model is positional arg index 15 on the candidate insert
    # (1 SQL + 14 fields before it)
    for call_args in conn.fetchrow.call_args_list:
        assert call_args.args[15] == "gemma4:e2b"

    # Variant upsert judge_model is at positional index 12
    # (1 SQL + 11 fields before it)
    variant_args = conn.execute.call_args.args
    assert variant_args[12] == "gemma4:e2b"


def test_result_judge_model_wins_over_fallback():
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")
    # Set judge_model on one candidate; fallback should be ignored for it
    for c in outcome.candidates:
        if c.result.engine_id == "fast_good":
            c.result.judge_model = "gemma4:e4b"

    asyncio.run(persist_contest(conn, outcome, judge_model="gemma4:e2b"))

    judge_models = [call_args.args[15] for call_args in conn.fetchrow.call_args_list]
    assert "gemma4:e4b" in judge_models  # the candidate's own value
    assert "gemma4:e2b" in judge_models  # fallback on others


def test_zero_speed_factor_and_composite_coerced_to_null():
    # A disabled / errored / no-output / below-floor candidate has
    # speed_factor=0.0 and composite_score=0.0. Those should be
    # written as NULL, not as literal zeros, so the audit view can
    # distinguish "not scored" from "scored to 0".
    conn = _make_conn()
    outcome = _make_outcome(winner_engine_id="fast_good")

    asyncio.run(persist_contest(conn, outcome))

    # speed_factor at positional index 11, composite_score at 12
    # (1 SQL + 10 fields before speed_factor)
    for call_args in conn.fetchrow.call_args_list:
        sf = call_args.args[11]
        comp = call_args.args[12]
        engine_id = call_args.args[4]
        if engine_id in {"unsupp", "broken", "low_q"}:
            assert sf is None, f"{engine_id}: expected NULL sf, got {sf}"
            assert comp is None, f"{engine_id}: expected NULL composite, got {comp}"
        else:
            assert sf is not None and sf > 0
            assert comp is not None and comp > 0

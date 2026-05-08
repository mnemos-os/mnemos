"""MPF v0.2 native envelope emission tests.

These exercise the serializer + version resolver in isolation — no live
Postgres needed. The route-level integration test is in
tests/test_portability_routes.py (existing) and continues to cover the
v0.1 default emission.
"""
from __future__ import annotations

import pytest  # noqa: F401  (used by pytest.raises)
from fastapi import HTTPException

from mnemos.domain.portability.export import _resolve_export_version
from mnemos.domain.portability.schemas import (
    MPF_VERSION,
    MPF_VERSION_V0_2,
    MPFRecord,
)
from mnemos.domain.portability.serializers import (
    _derive_prov_activity_type,
    _memory_to_record,
    _record_provenance_v0_2,
)


_BASE_ROW = {
    "id": "mem_test1",
    "content": "hello",
    "category": "facts",
    "subcategory": "test",
    "created": "2026-05-06T12:00:00+00:00",
    "updated": "2026-05-06T12:00:00+00:00",
    "owner_id": "alice",
    "namespace": "default",
    "permission_mode": 600,
    "quality_rating": 75,
    "source_model": "claude-sonnet",
    "source_provider": "anthropic",
    "source_session": "sess_abc",
    "source_agent": "claude",
    "metadata": {},
}


# ── _resolve_export_version ─────────────────────────────────────────────


def test_resolve_export_version_default_is_v0_1():
    assert _resolve_export_version(None) == MPF_VERSION
    assert MPF_VERSION.startswith("0.1")


def test_resolve_export_version_accepts_0_1_variants():
    assert _resolve_export_version("0.1") == MPF_VERSION
    assert _resolve_export_version("0.1.0") == MPF_VERSION
    assert _resolve_export_version("0.1.1") == MPF_VERSION
    assert _resolve_export_version(" 0.1 ") == MPF_VERSION  # tolerates whitespace


def test_resolve_export_version_accepts_0_2_variants():
    assert _resolve_export_version("0.2") == MPF_VERSION_V0_2
    assert _resolve_export_version("0.2.0") == MPF_VERSION_V0_2


def test_resolve_export_version_rejects_unsupported():
    for bad in ("0.3", "1.0", "v0.2", "garbage", "0.10.0"):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_export_version(bad)
        assert exc_info.value.status_code == 400


# ── _memory_to_record: v0.1 (default) emission ──────────────────────────


def test_memory_to_record_v0_1_omits_provenance():
    rec = _memory_to_record(_BASE_ROW, mpf_version="0.1.1")
    assert isinstance(rec, MPFRecord)
    assert rec.kind == "memory"
    assert rec.id == "mem_test1"
    # v0.1 record level has NO provenance / bi-temporal fields.
    assert rec.provenance is None
    assert rec.transaction_time is None
    assert rec.valid_time_start is None
    assert rec.valid_time_end is None
    # Payload still carries created/updated/owner_id as before.
    assert rec.payload["created"] == "2026-05-06T12:00:00+00:00"
    assert rec.payload["owner_id"] == "alice"


def test_memory_to_record_v0_1_serialization_omits_v0_2_fields():
    """When the envelope is v0.1, serialization must NOT include the
    record-level v0.2 fields even if they were populated."""
    rec = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    dumped = rec.model_dump_for_envelope("0.1.1")
    assert "provenance" not in dumped
    assert "transaction_time" not in dumped
    assert "valid_time_start" not in dumped


# ── _memory_to_record: v0.2 emission ────────────────────────────────────


def test_memory_to_record_v0_2_populates_required_provenance():
    rec = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    assert rec.provenance is not None
    # Required PROV-DM fields per the v0.2 spec.
    assert "wasAttributedTo" in rec.provenance
    assert "wasGeneratedBy" in rec.provenance
    assert "generatedAtTime" in rec.provenance
    # wasAttributedTo derived from owner_id.
    assert rec.provenance["wasAttributedTo"] == {"type": "user", "id": "alice"}
    # wasGeneratedBy derived from source_session.
    assert rec.provenance["wasGeneratedBy"]["id"] == "sess_abc"
    # generatedAtTime equals created (ISO).
    assert rec.provenance["generatedAtTime"] == "2026-05-06T12:00:00+00:00"


def test_memory_to_record_v0_2_attributes_default_owner_to_system():
    """owner_id of 'default' / 'system' / 'mnemos' → wasAttributedTo.type=system."""
    for owner in ("default", "system", "mnemos", "DEFAULT"):
        row = {**_BASE_ROW, "owner_id": owner}
        rec = _memory_to_record(row, mpf_version="0.2.0")
        assert rec.provenance["wasAttributedTo"]["type"] == "system"


def test_memory_to_record_v0_2_populates_bitemporal_defaults():
    """Without metadata.valid_time, valid_time_start defaults to created and
    valid_time_end stays open-ended (None)."""
    rec = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    assert rec.transaction_time == "2026-05-06T12:00:00+00:00"
    assert rec.valid_time_start == "2026-05-06T12:00:00+00:00"
    assert rec.valid_time_end is None


def test_memory_to_record_v0_2_honors_metadata_valid_time():
    """If metadata carries valid_time.{start,end}, those win over defaults."""
    row = {
        **_BASE_ROW,
        "metadata": {
            "valid_time": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-12-31T23:59:59+00:00",
            }
        },
    }
    rec = _memory_to_record(row, mpf_version="0.2.0")
    assert rec.valid_time_start == "2026-01-01T00:00:00+00:00"
    assert rec.valid_time_end == "2026-12-31T23:59:59+00:00"


def test_memory_to_record_v0_2_serialization_round_trips():
    """The v0.2 record should serialize cleanly with all expected keys."""
    rec = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    dumped = rec.model_dump_for_envelope("0.2.0")
    assert "provenance" in dumped
    assert "transaction_time" in dumped
    assert "valid_time_start" in dumped
    # valid_time_end is None for the BASE_ROW path; exclude_none drops it.
    assert "valid_time_end" not in dumped


# ── activity-type derivation ────────────────────────────────────────────


def test_activity_type_default_is_chat_session():
    row = {**_BASE_ROW, "source_provider": "anthropic", "source_agent": "claude"}
    assert _derive_prov_activity_type(row) == "chat_session"


def test_activity_type_morpheus_distillation():
    row = {**_BASE_ROW, "source_agent": "morpheus_runner"}
    assert _derive_prov_activity_type(row) == "distillation"


def test_activity_type_federation_pull():
    row = {**_BASE_ROW, "source_agent": "fed:pythia", "source_provider": "federation"}
    assert _derive_prov_activity_type(row) == "federation_pull"


def test_activity_type_graeae_etl_job():
    row = {**_BASE_ROW, "source_provider": "graeae", "source_agent": "consultation"}
    assert _derive_prov_activity_type(row) == "etl_job"


def test_activity_type_apollo_distillation():
    """Apollo (compression) memories are also distillation activities."""
    row = {**_BASE_ROW, "source_agent": "apollo_engine"}
    assert _derive_prov_activity_type(row) == "distillation"


# ── _record_provenance_v0_2 missing fields ──────────────────────────────


def test_provenance_falls_back_to_memory_id_when_no_session():
    """Memories without source_session/source_agent/source_provider get a
    memory-scoped activity id rather than failing the required field."""
    row = {
        **_BASE_ROW,
        "source_session": None,
        "source_agent": None,
        "source_provider": None,
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasGeneratedBy"]["id"] == f"mem:{row['id']}"


def test_provenance_uses_updated_when_created_missing():
    """If created is missing, fall back to updated for generatedAtTime."""
    row = {**_BASE_ROW, "created": None, "updated": "2026-04-01T00:00:00+00:00"}
    prov = _record_provenance_v0_2(row)
    assert prov["generatedAtTime"] == "2026-04-01T00:00:00+00:00"


# ── HTTP serialization shape: exclude None ──────────────────────────────


def test_envelope_dump_excludes_null_v0_2_fields_in_v0_1_response():
    """Codex round-1 finding: v0.1 emission must NOT serialize the new
    v0.2 record fields as `null` — that breaks backward compat.

    We can't easily run a TestClient here without a live DB, so instead
    we validate the serializer the route uses (response_model_exclude_none=True)
    is wired correctly by checking the route's decorator. Companion check:
    the MPFRecord dump itself omits None v0.2 fields when called via
    `model_dump_for_envelope` (already covered by the existing test
    `test_memory_to_record_v0_1_serialization_omits_v0_2_fields`).
    """
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    # The route must opt into FastAPI's exclude-none response shape.
    # Unfortunately decorators don't show in inspect.getsource, so we
    # check the module-level source for the @router.get block instead.
    mod_src = inspect.getsource(routes_mod)
    export_decorator_idx = mod_src.find('@router.get(\n    "/export"')
    assert export_decorator_idx >= 0, "GET /export route decorator not found"
    next_def_idx = mod_src.find("\nasync def ", export_decorator_idx)
    decorator_block = mod_src[export_decorator_idx:next_def_idx]
    assert "response_model_exclude_none=True" in decorator_block, (
        "GET /v1/export must set response_model_exclude_none=True so the "
        "new optional v0.2 fields don't leak as `null` on v0.1 responses"
    )
    _ = src  # silence unused-var lint if it surfaces


def test_v0_1_envelope_dump_via_helper_strips_record_v0_2_fields():
    """Direct dump test: a v0.1 envelope serialization must NOT carry
    record-level v0.2 fields, even when records carry them."""
    from mnemos.domain.portability.schemas import MPFEnvelope

    v0_2_record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    env = MPFEnvelope(
        mpf_version="0.1.1",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[v0_2_record],
    )
    # The HTTP route uses response_model_exclude_none=True. Simulate that
    # contract here.
    dumped = env.model_dump(exclude_none=True)
    # Sidecar fields must NOT be present (no kg_triples, memory_versions,
    # compression_manifest, deletion_log).
    for absent in ("kg_triples", "memory_versions", "compression_manifest", "deletion_log"):
        assert absent not in dumped, f"v0.1 envelope must not carry null {absent}"
    # Record dump itself: the v0.2 record fields will surface (because
    # the record was built v0.2-shape), but on a v0.1 envelope the route
    # must strip them. That's what model_dump_for_envelope does.
    record_dump = v0_2_record.model_dump_for_envelope("0.1.1")
    for absent in ("provenance", "valid_time_start", "valid_time_end", "transaction_time"):
        assert absent not in record_dump, (
            f"v0.1 record dump must not carry {absent}"
        )


def test_change_type_compress_is_normalized_to_update():
    """Round-2 codex finding: MNEMOS uses change_type='compress' for
    compression-DAG version rows, but the v0.2 spec only allows
    create/update/delete/revert/merge/branch. Serializer normalizes."""
    from mnemos.domain.portability.serializers import _memory_version_to_entry

    row = {
        "id": "ver_001",
        "memory_id": "mem_test1",
        "version_num": 2,
        "commit_hash": "abcd",
        "branch": "main",
        "content": "compressed content",
        "snapshot_at": "2026-05-06T12:00:00+00:00",
        "snapshot_by": "apollo",
        "change_type": "compress",
    }
    entry = _memory_version_to_entry(row, mpf_version="0.2.0")
    assert entry["change_type"] == "update"
    # Original value preserved in metadata for round-trip fidelity.
    assert entry["metadata"]["_mnemos_change_type"] == "compress"


def test_change_type_recognized_values_pass_through():
    """Spec-allowed values should be unchanged (no metadata pollution)."""
    from mnemos.domain.portability.serializers import _memory_version_to_entry

    base = {
        "id": "ver_002",
        "memory_id": "mem_test2",
        "version_num": 3,
        "commit_hash": "ef01",
        "branch": "main",
        "content": "x",
        "snapshot_at": "2026-05-06T12:00:00+00:00",
        "snapshot_by": "alice",
    }
    for valid in ("create", "update", "delete", "revert", "merge", "branch"):
        entry = _memory_version_to_entry({**base, "change_type": valid}, mpf_version="0.2.0")
        assert entry["change_type"] == valid
        # No _mnemos_change_type metadata pollution for valid values.
        if "metadata" in entry:
            assert "_mnemos_change_type" not in entry.get("metadata", {})


def _load_v0_2_schema():
    """Load the vendored v0.2 schema so tests don't silently skip.

    The spec is vendored at mnemos/domain/portability/vendor/mpf-v0.2.json
    and kept in sync with the canonical mnemos-os/mpf v0.2.0 release.
    """
    import json
    from pathlib import Path

    vendored = (
        Path(__file__).resolve().parents[1]
        / "mnemos"
        / "domain"
        / "portability"
        / "vendor"
        / "mpf-v0.2.json"
    )
    with open(vendored) as fh:
        return json.load(fh)


def test_v0_2_envelope_with_compress_sidecar_validates_against_schema():
    """Round-2 codex regression: v0.2 envelope with a compression-DAG
    version row in memory_versions sidecar must validate against the
    published spec."""
    from mnemos.domain.portability.schemas import MPFEnvelope
    from mnemos.domain.portability.serializers import _memory_version_to_entry

    import jsonschema

    schema = _load_v0_2_schema()

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    # A compression DAG version row — change_type='compress' in the DB.
    compress_version = _memory_version_to_entry({
        "id": "ver_compress_1",
        "memory_id": _BASE_ROW["id"],
        "version_num": 2,
        "commit_hash": "cccc",
        "branch": "main",
        "parent_version_id": "ver_init_1",
        "content": "compressed content",
        "snapshot_at": "2026-05-06T13:00:00+00:00",
        "snapshot_by": "apollo_engine",
        "change_type": "compress",  # the offending DB value
    }, mpf_version="0.2.0")

    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        memory_versions=[compress_version],
    )
    dumped = env.model_dump(exclude_none=True)

    # Sanity: change_type was normalized to 'update' in the dumped sidecar.
    assert dumped["memory_versions"][0]["change_type"] == "update"
    # And jsonschema accepts the whole envelope.
    jsonschema.validate(instance=dumped, schema=schema)


def test_v0_2_envelope_validates_against_published_schema():
    """End-to-end: a v0.2 envelope produced by the serializer must validate
    against the vendored schema/mpf-v0.2.json from mnemos-os/mpf v0.2.0."""
    import jsonschema
    from mnemos.domain.portability.schemas import MPFEnvelope

    schema = _load_v0_2_schema()

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
    )
    dumped = env.model_dump(exclude_none=True)

    # Sanity: v0.2 record-level required fields are present.
    assert "provenance" in dumped["records"][0]
    assert "wasAttributedTo" in dumped["records"][0]["provenance"]

    # Strict schema validation. Raises if it doesn't conform.
    jsonschema.validate(instance=dumped, schema=schema)


# ── Sidecar field normalization for v0.2 schema conformance ──────────────


def test_kg_triple_confidence_clamped_to_unit_interval():
    """Codex round-3 finding: kg_triples[].confidence has v0.2 spec
    bounds [0, 1]; raw DB rows can carry out-of-range values that
    schema validation rejects. Serializer clamps + stashes the raw."""
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    base = {
        "id": "kg_001",
        "predicate": "rel",
        "subject": "alice",
        "object": "bob",
        "memory_id": "mem_test1",
        "metadata": {},
    }
    high = _kg_triple_to_entry({**base, "confidence": 2.0}, mpf_version="0.2.0")
    assert high["confidence"] == 1.0
    assert high["metadata"]["_mnemos_raw_confidence"] == 2.0

    low = _kg_triple_to_entry({**base, "confidence": -0.5}, mpf_version="0.2.0")
    assert low["confidence"] == 0.0
    assert low["metadata"]["_mnemos_raw_confidence"] == -0.5

    valid = _kg_triple_to_entry({**base, "confidence": 0.75}, mpf_version="0.2.0")
    assert valid["confidence"] == 0.75
    # No raw stash needed when value is in range.
    assert "_mnemos_raw_confidence" not in valid.get("metadata", {})


def test_kg_triple_confidence_unparseable_dropped():
    """Garbage non-numeric confidence is dropped from the output, raw
    preserved in metadata."""
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    entry = _kg_triple_to_entry({
        "id": "kg_002",
        "predicate": "rel",
        "subject": "x",
        "object": "y",
        "memory_id": "mem_test1",
        "confidence": "not-a-number",
        "metadata": {},
    }, mpf_version="0.2.0")
    assert "confidence" not in entry  # dropped via exclude-None
    assert entry["metadata"]["_mnemos_raw_confidence"] == "not-a-number"


def test_compression_compressed_tokens_clamped_to_non_negative():
    """v0.2 spec requires compressed_tokens >= 0. Legacy DB rows can
    carry -1 as a sentinel."""
    from mnemos.domain.portability.serializers import _compression_variant_to_entry

    base = {
        "memory_id": "mem_test1",
        "engine_id": "apollo_v1",
        "engine_version": "1.0.0",
        "compressed_content": "compressed",
    }
    negative = _compression_variant_to_entry({**base, "compressed_tokens": -1}, mpf_version="0.2.0")
    assert "compressed_tokens" not in negative  # dropped

    zero = _compression_variant_to_entry({**base, "compressed_tokens": 0}, mpf_version="0.2.0")
    assert zero["compressed_tokens"] == 0  # zero is valid

    positive = _compression_variant_to_entry({**base, "compressed_tokens": 42}, mpf_version="0.2.0")
    assert positive["compressed_tokens"] == 42

    garbage = _compression_variant_to_entry({**base, "compressed_tokens": "not-int"}, mpf_version="0.2.0")
    assert "compressed_tokens" not in garbage


def test_v0_2_envelope_with_kg_sidecar_validates_against_schema():
    """End-to-end: a v0.2 envelope with kg_triples sidecar including
    out-of-range confidence (clamped) validates against the spec."""
    import jsonschema
    from mnemos.domain.portability.schemas import MPFEnvelope
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    schema = _load_v0_2_schema()

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    # Out-of-range confidence — must be clamped before envelope assembly.
    kg_entry = _kg_triple_to_entry({
        "id": "kg_test1",
        "predicate": "is_a",
        "subject": "alice",
        "object": "person",
        "memory_id": _BASE_ROW["id"],
        "confidence": 2.5,  # invalid per spec
        "metadata": {},
    }, mpf_version="0.2.0")

    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        kg_triples=[kg_entry],
    )
    dumped = env.model_dump(exclude_none=True)
    assert dumped["kg_triples"][0]["confidence"] == 1.0
    jsonschema.validate(instance=dumped, schema=schema)


def test_v0_2_envelope_with_compression_sidecar_validates_against_schema():
    """End-to-end: a v0.2 envelope with compression_manifest sidecar
    including a -1 compressed_tokens (dropped) validates."""
    import jsonschema
    from mnemos.domain.portability.schemas import MPFEnvelope
    from mnemos.domain.portability.serializers import _compression_variant_to_entry

    schema = _load_v0_2_schema()

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    cv_entry = _compression_variant_to_entry({
        "memory_id": _BASE_ROW["id"],
        "engine_id": "apollo_v1",
        "engine_version": "1.0.0",
        "compressed_content": "shorter",
        "compressed_tokens": -1,  # legacy sentinel
        "compression_ratio": 0.42,
    }, mpf_version="0.2.0")

    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        compression_manifest=[cv_entry],
    )
    dumped = env.model_dump(exclude_none=True)
    assert "compressed_tokens" not in dumped["compression_manifest"][0]
    jsonschema.validate(instance=dumped, schema=schema)


# ── v0.1 sidecar backward-compat regression ──────────────────────────────


def test_v0_1_kg_triple_keeps_out_of_range_confidence_verbatim():
    """v0.1 emission must preserve raw DB values — clamping is v0.2-only."""
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    entry = _kg_triple_to_entry({
        "id": "kg_legacy",
        "predicate": "r",
        "subject": "a",
        "object": "b",
        "memory_id": "mem_test1",
        "confidence": 2.5,  # invalid for v0.2, valid as a v0.1 raw value
        "metadata": {},
    }, mpf_version="0.1.1")
    assert entry["confidence"] == 2.5
    assert "metadata" not in entry or "_mnemos_raw_confidence" not in entry.get("metadata", {})


def test_v0_1_memory_version_keeps_compress_change_type_verbatim():
    """v0.1 emission must preserve change_type='compress' verbatim."""
    from mnemos.domain.portability.serializers import _memory_version_to_entry

    entry = _memory_version_to_entry({
        "id": "ver_legacy",
        "memory_id": "mem_test1",
        "version_num": 2,
        "commit_hash": "abcd",
        "branch": "main",
        "content": "x",
        "snapshot_at": "2026-05-06T12:00:00+00:00",
        "snapshot_by": "apollo",
        "change_type": "compress",
    }, mpf_version="0.1.1")
    assert entry["change_type"] == "compress"
    if "metadata" in entry:
        assert "_mnemos_change_type" not in entry["metadata"]


def test_v0_1_compression_variant_keeps_negative_compressed_tokens_verbatim():
    """v0.1 emission must preserve compressed_tokens=-1 verbatim."""
    from mnemos.domain.portability.serializers import _compression_variant_to_entry

    entry = _compression_variant_to_entry({
        "memory_id": "mem_test1",
        "engine_id": "apollo_v1",
        "engine_version": "1.0.0",
        "compressed_content": "x",
        "compressed_tokens": -1,
    }, mpf_version="0.1.1")
    assert entry["compressed_tokens"] == -1


# ── NaN / Infinity edge cases (JSON-unsafe values) ───────────────────────


def test_kg_confidence_nan_dropped_and_stringified_in_metadata():
    """JSON has no NaN/Inf representation. The serializer must drop the
    field and stash a JSON-safe string in metadata — otherwise FastAPI
    serialization 500s on the response."""
    import math
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    base = {
        "id": "kg_nan",
        "predicate": "rel",
        "subject": "a",
        "object": "b",
        "memory_id": "mem_test1",
        "metadata": {},
    }
    nan_entry = _kg_triple_to_entry({**base, "confidence": math.nan}, mpf_version="0.2.0")
    assert "confidence" not in nan_entry  # dropped
    raw = nan_entry["metadata"]["_mnemos_raw_confidence"]
    # JSON-safe string, not the float nan.
    assert isinstance(raw, str)
    assert raw == "nan" or "nan" in raw.lower()


def test_kg_confidence_infinity_dropped_and_stringified_in_metadata():
    """+Infinity / -Infinity must also be JSON-safe in metadata."""
    import math
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    base = {
        "id": "kg_inf",
        "predicate": "rel",
        "subject": "a",
        "object": "b",
        "memory_id": "mem_test1",
        "metadata": {},
    }
    inf_entry = _kg_triple_to_entry({**base, "confidence": math.inf}, mpf_version="0.2.0")
    assert "confidence" not in inf_entry
    raw = inf_entry["metadata"]["_mnemos_raw_confidence"]
    assert isinstance(raw, str)

    neg_inf_entry = _kg_triple_to_entry({**base, "confidence": -math.inf}, mpf_version="0.2.0")
    assert "confidence" not in neg_inf_entry
    raw_neg = neg_inf_entry["metadata"]["_mnemos_raw_confidence"]
    assert isinstance(raw_neg, str)


def test_kg_confidence_nan_envelope_serializes_to_json_cleanly():
    """End-to-end: a v0.2 envelope with a NaN-confidence kg triple must
    serialize to valid JSON (no `NaN` literals that would 500 a response)."""
    import json
    import math

    from mnemos.domain.portability.schemas import MPFEnvelope
    from mnemos.domain.portability.serializers import _kg_triple_to_entry

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    kg_entry = _kg_triple_to_entry({
        "id": "kg_nan_e2e",
        "predicate": "p",
        "subject": "x",
        "object": "y",
        "memory_id": _BASE_ROW["id"],
        "confidence": math.nan,
        "metadata": {},
    }, mpf_version="0.2.0")
    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        kg_triples=[kg_entry],
    )
    dumped = env.model_dump(exclude_none=True)
    # json.dumps must succeed without allow_nan tricks — i.e., no NaN
    # values lurking in the structure.
    serialized = json.dumps(dumped, allow_nan=False)
    assert "NaN" not in serialized  # case-sensitive — Python's json renders 'NaN' literal


# ── deletion_log v0.2 sidecar emission ──────────────────────────────────


def test_deletion_log_serializer_emits_required_v0_2_fields():
    """The deletion_log serializer must populate id, record_id, deleted_at."""
    from mnemos.domain.portability.serializers import _deletion_log_to_entry

    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "memory_id": "mem_deleted_42",
        "content_hash": "sha256:abcdef",
        "owner_id": "alice",
        "namespace": "default",
        "requested_by": "alice",
        "requested_at": "2026-05-06T12:00:00+00:00",
        "executed_at": "2026-05-06T12:00:01+00:00",
        "request_kind": "gdpr_wipe",
        "reason": "user requested deletion",
        "source": ["api"],
    }
    entry = _deletion_log_to_entry(row, mpf_version="0.2.0")
    assert entry["id"] == "00000000-0000-0000-0000-000000000001"
    assert entry["record_id"] == "mem_deleted_42"
    assert entry["deleted_at"] == "2026-05-06T12:00:01+00:00"
    assert entry["deleted_by"] == "alice"
    assert entry["reason"] == "user requested deletion"
    assert entry["tombstone_hash"] == "sha256:abcdef"
    # Round-trip extras stashed in metadata.
    assert entry["metadata"]["_mnemos_request_kind"] == "gdpr_wipe"
    assert entry["metadata"]["_mnemos_source"] == ["api"]


def test_deletion_log_serializer_returns_empty_on_v0_1():
    """v0.1 emission has no deletion_log sidecar — serializer returns {}
    so the caller can filter it out cleanly."""
    from mnemos.domain.portability.serializers import _deletion_log_to_entry

    entry = _deletion_log_to_entry({
        "id": "00000000-0000-0000-0000-000000000002",
        "memory_id": "x",
        "content_hash": "h",
        "executed_at": "2026-05-06T00:00:00+00:00",
    }, mpf_version="0.1.1")
    assert entry == {}


def test_deletion_log_falls_back_to_requested_at_when_executed_at_missing():
    """deleted_at is required by the v0.2 spec. Fall back to requested_at
    if executed_at is somehow null (defensive)."""
    from mnemos.domain.portability.serializers import _deletion_log_to_entry

    entry = _deletion_log_to_entry({
        "id": "00000000-0000-0000-0000-000000000003",
        "memory_id": "mem_x",
        "content_hash": "h",
        "executed_at": None,
        "requested_at": "2026-05-06T11:00:00+00:00",
        "request_kind": "admin_purge",
    }, mpf_version="0.2.0")
    assert entry["deleted_at"] == "2026-05-06T11:00:00+00:00"


def test_v0_2_envelope_with_deletion_log_validates_against_schema():
    """End-to-end: a v0.2 envelope with deletion_log sidecar entries
    validates against the published spec."""
    import jsonschema
    from mnemos.domain.portability.schemas import MPFEnvelope
    from mnemos.domain.portability.serializers import _deletion_log_to_entry

    schema = _load_v0_2_schema()

    record = _memory_to_record(_BASE_ROW, mpf_version="0.2.0")
    dl_entry = _deletion_log_to_entry({
        "id": "00000000-0000-0000-0000-000000000004",
        "memory_id": "mem_deleted",
        "content_hash": "sha256:0123",
        "owner_id": "alice",
        "namespace": "default",
        "requested_by": "alice",
        "requested_at": "2026-05-06T12:00:00+00:00",
        "executed_at": "2026-05-06T12:00:01+00:00",
        "request_kind": "gdpr_wipe",
        "reason": "test",
        "source": [],
    }, mpf_version="0.2.0")

    env = MPFEnvelope(
        mpf_version="0.2.0",
        source_system="mnemos",
        source_version="5.3.2",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        deletion_log=[dl_entry],
    )
    dumped = env.model_dump(exclude_none=True)
    assert "deletion_log" in dumped
    assert dumped["deletion_log"][0]["record_id"] == "mem_deleted"
    jsonschema.validate(instance=dumped, schema=schema)


def test_v0_1_envelope_omits_deletion_log_field_entirely():
    """A v0.1 envelope must NOT carry a deletion_log key (response_model
    exclude_none drops the None field)."""
    from mnemos.domain.portability.schemas import MPFEnvelope

    record = _memory_to_record(_BASE_ROW, mpf_version="0.1.1")
    env = MPFEnvelope(
        mpf_version="0.1.1",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=1,
        records=[record],
        # deletion_log left as None (default)
    )
    dumped = env.model_dump(exclude_none=True)
    assert "deletion_log" not in dumped


def test_deletion_log_413_message_documents_pagination_unavailable():
    """When deletion_log exceeds the cap, the 413 is now a DEFENSIVE
    FALLBACK (most callers get a `deletion_log_next_cursor` instead of
    a 413). The message must explain that the cursor path failed and
    point at actionable causes."""
    from fastapi import HTTPException
    from mnemos.domain.portability.export import (
        _EXPORT_SIDECAR_HARD_LIMIT,
        _enforce_sidecar_cap,
    )

    # Simulate a row count exceeding the cap.
    too_many = list(range(_EXPORT_SIDECAR_HARD_LIMIT + 1))

    # Generic sidecar — guidance points at category/limit narrowing.
    with pytest.raises(HTTPException) as exc_info:
        _enforce_sidecar_cap(too_many, "kg_triples")
    assert exc_info.value.status_code == 413
    assert "category" in exc_info.value.detail.lower()

    # deletion_log — defensive-fallback guidance.
    with pytest.raises(HTTPException) as exc_info:
        _enforce_sidecar_cap(too_many, "deletion_log")
    detail = exc_info.value.detail
    assert exc_info.value.status_code == 413
    assert "deletion_log" in detail
    # Should reference the cursor path and the failure mode.
    assert "cursor" in detail.lower()
    assert (
        "defensive fallback" in detail.lower()
        or "deletion_log_next_cursor" in detail
    )
    # Should NOT misleadingly suggest category/limit narrowing.
    detail_lower = detail.lower()
    assert (
        "category" not in detail_lower
        or "does not reduce" in detail_lower
        or "won't reduce" in detail_lower
        or "not memory-bound" in detail_lower
    ), (
        "deletion_log 413 must explicitly state that category/limit "
        "narrowing doesn't help"
    )


# ── Real MNEMOS provenance columns (federation, morpheus) ────────────────


def test_provenance_morpheus_row_uses_distillation_with_lineage():
    """Morpheus-synthesized rows have prov_kind='morpheus_local' +
    morpheus_run_id + source_memories[]. Provenance must classify as
    distillation, not chat_session, and include wasInfluencedBy."""
    row = {
        **_BASE_ROW,
        "source_provider": "graeae",  # would heuristically be etl_job
        "source_agent": None,
        "prov_kind": "morpheus_local",
        "morpheus_run_id": "00000000-0000-0000-0000-000000000042",
        "source_memories": ["mem_src_1", "mem_src_2", "mem_src_3"],
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasGeneratedBy"]["type"] == "distillation"
    assert "morpheus" in prov["wasGeneratedBy"]["id"]
    assert prov["wasAttributedTo"] == {"type": "system", "id": "mnemos:morpheus"}
    assert prov["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_src_1"},
        {"type": "memory", "id": "mem_src_2"},
        {"type": "memory", "id": "mem_src_3"},
    ]


def test_provenance_morpheus_run_id_alone_does_NOT_classify_as_morpheus():
    """Round-3 codex finding: morpheus_run_id alone is NOT sufficient.
    `phase_consolidate` updates EXISTING user-authored memories with a
    run_id without setting prov_kind='morpheus_local'. Those rows must
    keep their original user attribution, not be falsely tagged as
    Morpheus-generated."""
    row = {
        **_BASE_ROW,
        # Existing user memory the consolidate phase touched.
        "morpheus_run_id": "abcd",
        "prov_kind": None,
        # Original author / activity preserved.
        "owner_id": "alice",
        "source_provider": "anthropic",
        "source_session": "sess_user",
    }
    prov = _record_provenance_v0_2(row)
    # Falls through to the heuristic path. NOT distillation, NOT
    # mnemos:morpheus.
    assert prov["wasGeneratedBy"]["type"] == "chat_session"
    assert prov["wasAttributedTo"] == {"type": "user", "id": "alice"}
    assert "wasInfluencedBy" not in prov


def test_provenance_morpheus_handles_sqlite_json_text_source_memories():
    """SQLite stores source_memory_ids as a JSON-text array. The
    serializer parses string values for cross-backend portability."""
    row = {
        **_BASE_ROW,
        "prov_kind": "morpheus_local",  # the deterministic signal
        "morpheus_run_id": "abcd",
        "source_memories": '["mem_a", "mem_b"]',  # JSON text, not list
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_a"},
        {"type": "memory", "id": "mem_b"},
    ]


def test_provenance_morpheus_with_no_source_memories_omits_wasInfluencedBy():
    """When the influence array is empty, wasInfluencedBy should not be
    emitted at all (it's optional in the spec)."""
    row = {
        **_BASE_ROW,
        "prov_kind": "morpheus_local",
        "morpheus_run_id": "abcd",
        "source_memories": [],
    }
    prov = _record_provenance_v0_2(row)
    assert "wasInfluencedBy" not in prov


def test_provenance_federation_row_uses_federation_pull_attributed_to_system():
    """federation_source rows are authoritatively federation_pull,
    attributed to system (federation worker), not to the original
    user owner."""
    row = {
        **_BASE_ROW,
        "owner_id": "alice",  # original user owner
        "federation_source": "pythia",
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasAttributedTo"] == {"type": "system", "id": "federation:pythia"}
    assert prov["wasGeneratedBy"]["type"] == "federation_pull"
    assert prov["wasGeneratedBy"]["id"] == "federation:pythia"


def test_provenance_default_path_unchanged_for_non_special_rows():
    """Rows without federation_source / morpheus_run_id / morpheus prov_kind
    fall through to the existing source_provider/agent heuristic."""
    row = {
        **_BASE_ROW,
        "federation_source": None,
        "morpheus_run_id": None,
        "prov_kind": None,
        "source_provider": "anthropic",
        "source_agent": "claude",
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasGeneratedBy"]["type"] == "chat_session"
    assert prov["wasAttributedTo"] == {"type": "user", "id": "alice"}
    assert "wasInfluencedBy" not in prov


def test_provenance_morpheus_reads_sqlite_source_memory_ids_column():
    """Round-1 codex finding: SQLite uses `source_memory_ids` (JSON text),
    not `source_memories` (the postgres array column). The serializer
    must read either key."""
    row = {
        **_BASE_ROW,
        "prov_kind": "morpheus_local",
        "morpheus_run_id": "run_42",
        # SQLite shape — note the column name AND the JSON-text value.
        "source_memory_ids": '["mem_sqlite_1", "mem_sqlite_2"]',
        # source_memories absent (postgres key not in this row)
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_sqlite_1"},
        {"type": "memory", "id": "mem_sqlite_2"},
    ]


def test_provenance_morpheus_postgres_source_memories_takes_precedence():
    """If both keys are present, postgres `source_memories` (the column
    explicitly aliased by the export query) wins."""
    row = {
        **_BASE_ROW,
        "prov_kind": "morpheus_local",
        "morpheus_run_id": "run_43",
        "source_memories": ["mem_pg_1", "mem_pg_2"],
        "source_memory_ids": '["mem_sqlite_1"]',  # would lose
    }
    prov = _record_provenance_v0_2(row)
    assert prov["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_pg_1"},
        {"type": "memory", "id": "mem_pg_2"},
    ]


# ── SQLite backend-level export shape (codex round-2 finding) ────────────


@pytest.mark.asyncio
async def test_sqlite_fetch_memory_export_includes_provenance_columns(tmp_path):
    """Round-2 codex finding: the serializer reads source_memory_ids, but
    the SQLite export query has to project it. Backend-level coverage:
    insert a Morpheus-shaped row directly via SQL, call fetch_memory_export,
    confirm the row dict carries prov_kind / morpheus_run_id /
    source_memory_ids / federation_source so the v0.2 serializer can
    populate wasInfluencedBy."""
    from types import SimpleNamespace
    from mnemos.persistence.sqlite import SqliteBackend, _execute

    backend = SqliteBackend(
        tmp_path / "prov.sqlite3",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await backend.open()
    try:
        # Insert a Morpheus-style row directly via SQL so we control
        # the provenance columns the higher-level insert helper
        # doesn't expose.
        async with backend.transactional() as tx:
            conn = tx.conn
            await _execute(
                conn,
                "INSERT INTO memories (id, content, category, owner_id, "
                "namespace, permission_mode, quality_rating, metadata, "
                "morpheus_run_id, source_memory_ids, provenance, created, "
                "updated, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    "mem_morph_test",
                    "synthesized cluster summary",
                    "synthesis",
                    "morpheus",
                    "default",
                    600,
                    80,
                    "{}",
                    "run_42",
                    '["mem_a","mem_b"]',
                    "morpheus_local",
                    "2026-05-06T12:00:00+00:00",
                    "2026-05-06T12:00:00+00:00",
                ),
            )
            rows = await backend.memories.fetch_memory_export(
                tx,
                effective_owner="morpheus",
                effective_ns="default",
                category=None,
                limit=10,
                offset=0,
            )
        assert len(rows) == 1
        row = dict(rows[0])
        # Provenance columns must be projected.
        assert row["morpheus_run_id"] == "run_42"
        assert row["source_memory_ids"] == '["mem_a","mem_b"]'
        assert row["prov_kind"] == "morpheus_local"
        # Now feed it to the serializer as the export pipeline would.
        rec = _memory_to_record(row, mpf_version="0.2.0")
        assert rec.provenance["wasGeneratedBy"]["type"] == "distillation"
        assert rec.provenance["wasGeneratedBy"]["id"] == "morpheus:run_42"
        assert rec.provenance["wasInfluencedBy"] == [
            {"type": "memory", "id": "mem_a"},
            {"type": "memory", "id": "mem_b"},
        ]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_fetch_memory_export_federation_source_classifies_as_pull(tmp_path):
    """Federation rows in SQLite must classify as federation_pull through
    the export pipeline."""
    from types import SimpleNamespace
    from mnemos.persistence.sqlite import SqliteBackend, _execute

    backend = SqliteBackend(
        tmp_path / "fed.sqlite3",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await backend.open()
    try:
        async with backend.transactional() as tx:
            conn = tx.conn
            await _execute(
                conn,
                "INSERT INTO memories (id, content, category, owner_id, "
                "namespace, permission_mode, quality_rating, metadata, "
                "federation_source, created, updated, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    "mem_fed_test",
                    "from peer pythia",
                    "facts",
                    "alice",
                    "default",
                    600,
                    75,
                    "{}",
                    "pythia",
                    "2026-05-06T12:00:00+00:00",
                    "2026-05-06T12:00:00+00:00",
                ),
            )
            rows = await backend.memories.fetch_memory_export(
                tx,
                effective_owner="alice",
                effective_ns="default",
                category=None,
                limit=10,
                offset=0,
            )
        row = dict(rows[0])
        assert row["federation_source"] == "pythia"
        rec = _memory_to_record(row, mpf_version="0.2.0")
        assert rec.provenance["wasGeneratedBy"]["type"] == "federation_pull"
        assert rec.provenance["wasGeneratedBy"]["id"] == "federation:pythia"
        # Federation row attribution is to the system worker, not the
        # original owner.
        assert rec.provenance["wasAttributedTo"]["type"] == "system"
    finally:
        await backend.close()


# ── Cross-tenant wasInfluencedBy filter (codex round-4) ──────────────────


def _build_envelope_with_morpheus_summary(in_scope_ids, source_ids):
    """Helper: build an envelope where the Morpheus summary references
    `source_ids` in its source_memories, but the records[] list contains
    only `in_scope_ids` rows. Mirrors the export.py post-processing path.
    """
    from mnemos.domain.portability.schemas import MPFEnvelope

    summary_row = {
        **_BASE_ROW,
        "id": "mem_summary",
        "owner_id": "alice",
        "prov_kind": "morpheus_local",
        "morpheus_run_id": "run_xx",
        "source_memories": list(source_ids),
    }
    summary_rec = _memory_to_record(summary_row, mpf_version="0.2.0")

    extra_records = [
        _memory_to_record({**_BASE_ROW, "id": rid}, mpf_version="0.2.0")
        for rid in in_scope_ids
    ]
    records = [summary_rec, *extra_records]

    # Apply the export.py post-process filter logic in the test —
    # avoids needing a live DB just to exercise the filter.
    in_scope = {r.id for r in records}
    for rec in records:
        if rec.provenance and "wasInfluencedBy" in rec.provenance:
            filtered = [
                infl
                for infl in rec.provenance["wasInfluencedBy"]
                if not (
                    infl.get("type") == "memory"
                    and infl.get("id") not in in_scope
                )
            ]
            if filtered:
                rec.provenance["wasInfluencedBy"] = filtered
            else:
                rec.provenance.pop("wasInfluencedBy", None)

    return MPFEnvelope(
        mpf_version="0.2.0",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=len(records),
        records=records,
    )


def test_morpheus_wasInfluencedBy_filters_out_of_scope_memory_ids():
    """Round-4 codex finding: Morpheus summary's source_memories may
    reference other owners' memory IDs that aren't in this export's
    scope. Those must NOT leak through wasInfluencedBy."""
    env = _build_envelope_with_morpheus_summary(
        in_scope_ids={"mem_alice_1", "mem_alice_2"},
        source_ids=["mem_alice_1", "mem_BOB_42", "mem_alice_2", "mem_EVE_99"],
    )
    summary = env.records[0]
    assert summary.id == "mem_summary"
    # Only the alice IDs (in scope) survive in wasInfluencedBy.
    assert summary.provenance["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_alice_1"},
        {"type": "memory", "id": "mem_alice_2"},
    ]


def test_morpheus_wasInfluencedBy_omitted_entirely_if_all_sources_out_of_scope():
    """If every source ID is out-of-scope, wasInfluencedBy is dropped
    rather than emitted as an empty array."""
    env = _build_envelope_with_morpheus_summary(
        in_scope_ids={"mem_alice_1"},
        source_ids=["mem_BOB_42", "mem_EVE_99", "mem_CARL_1"],
    )
    summary = env.records[0]
    assert "wasInfluencedBy" not in summary.provenance


def test_morpheus_wasInfluencedBy_preserved_when_all_sources_in_scope():
    """Sanity: when every source is in records[], filtering is a no-op."""
    env = _build_envelope_with_morpheus_summary(
        in_scope_ids={"mem_alice_1", "mem_alice_2", "mem_alice_3"},
        source_ids=["mem_alice_1", "mem_alice_2", "mem_alice_3"],
    )
    summary = env.records[0]
    assert summary.provenance["wasInfluencedBy"] == [
        {"type": "memory", "id": "mem_alice_1"},
        {"type": "memory", "id": "mem_alice_2"},
        {"type": "memory", "id": "mem_alice_3"},
    ]


def test_export_filter_logic_in_export_module_is_present():
    """Source-level guard: the cross-tenant filter must live in
    export.py's post-processing (not just in this test's helper)."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    src = inspect.getsource(export_mod.export_memories)
    # The filter must reference both the in-scope IDs AND the
    # wasInfluencedBy field name to count as wired.
    assert "in_scope_ids" in src or "in_scope" in src
    assert "wasInfluencedBy" in src
    assert "0.2" in src  # the v0.2 version gate


# ── deletion_log time-window pagination (#141) ───────────────────────────


def test_fetch_deletion_log_for_export_accepts_time_window_params():
    """Source-level guard: fetch_deletion_log_for_export must accept
    from_executed_at / to_executed_at kwargs and emit them as SQL
    conditions."""
    import inspect
    from mnemos.db import portability_repo

    sig = inspect.signature(portability_repo.fetch_deletion_log_for_export)
    assert "from_executed_at" in sig.parameters
    assert "to_executed_at" in sig.parameters
    src = inspect.getsource(portability_repo.fetch_deletion_log_for_export)
    assert "executed_at >=" in src
    assert "executed_at <=" in src
    # Must use timestamptz cast to handle ISO strings safely.
    assert "::timestamptz" in src


def test_export_memories_threads_deletion_log_time_window_params():
    """Source-level guard: export_memories must accept the params and
    pass them into fetch_deletion_log_for_export."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    sig = inspect.signature(export_mod.export_memories)
    assert "deletion_log_from" in sig.parameters
    assert "deletion_log_to" in sig.parameters

    src = inspect.getsource(export_mod.export_memories)
    # Both params must reach fetch_deletion_log_for_export. The actual
    # values passed are effective_dl_from / effective_dl_to — an
    # indirection that lets a cursor's encoded window override the
    # request-time params on subsequent pages (codex round-2 fix).
    # Round-5 reorganized the indirection: effective_dl_from/_to now
    # come from cursor_dl_from / deletion_log_from at the deletion_log
    # block (page 1 vs page 2 selection).
    assert "from_executed_at=effective_dl_from" in src
    assert "to_executed_at=effective_dl_to" in src
    # The indirection must seed from cursor (page 2+) or request (page 1).
    assert "cursor_dl_from if cursor_data is not None else deletion_log_from" in src
    assert "cursor_dl_to if cursor_data is not None else deletion_log_to" in src


def test_route_export_exposes_deletion_log_pagination_params():
    """Source-level guard: GET /v1/export route exposes the pagination
    params and threads them into the domain function."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    sig = inspect.signature(routes_mod.export_memories)
    assert "deletion_log_from" in sig.parameters
    assert "deletion_log_to" in sig.parameters

    src = inspect.getsource(routes_mod.export_memories)
    # Datetime-typed at the boundary, ISO-stringified before passing
    # to the domain function (which threads to repo as str).
    assert "deletion_log_from.isoformat()" in src
    assert "deletion_log_to.isoformat()" in src
    # Naive-datetime rejection.
    assert "tzinfo is None" in src
    # Inverted-range rejection.
    assert "must be <=" in src or "deletion_log_from > deletion_log_to" in src


def test_route_export_rejects_naive_deletion_log_from():
    """Naive datetimes silently shift to DB session timezone — must 400.
    Source-level guard via inspect (TestClient setup is heavy and the
    domain function is the substantive piece)."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    assert "tzinfo is None" in src
    assert "400" in src or "status_code=400" in src


def test_route_export_rejects_inverted_deletion_log_range():
    """from > to should reject with 400, not silently empty result."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    # Both shapes acceptable — function may use either comparison.
    assert "deletion_log_from > deletion_log_to" in src or "must be <=" in src


def test_deletion_log_413_no_longer_promises_follow_up_pagination():
    """Sanity: with #142 keyset cursor landed, the 413 must NOT still
    say 'tracked as follow-up' / 'until that ships'. Those wordings
    were stop-gaps before pagination existed."""
    from fastapi import HTTPException
    from mnemos.domain.portability.export import (
        _EXPORT_SIDECAR_HARD_LIMIT,
        _enforce_sidecar_cap,
    )

    too_many = list(range(_EXPORT_SIDECAR_HARD_LIMIT + 1))
    with pytest.raises(HTTPException) as exc_info:
        _enforce_sidecar_cap(too_many, "deletion_log")
    detail = exc_info.value.detail.lower()
    assert "follow-up" not in detail
    assert "until that ships" not in detail
    # Should reference the cursor mechanism (now landed).
    assert "cursor" in detail


def test_deletion_log_export_index_migration_is_wired():
    """The MPF v0.2 deletion_log export access path needs an index over
    (owner_id, namespace, executed_at, id). Source guard: the migration
    file exists and is in the canonical loader list."""
    import inspect
    from pathlib import Path
    from mnemos.installer import db as installer_db

    # The migration file must exist.
    repo_root = Path(installer_db.__file__).resolve().parents[2]
    mig_path = repo_root / "db" / "migrations_v5_3_3_deletion_log_export_index.sql"
    assert mig_path.exists(), (
        "v5.3.3 deletion_log_export_index migration file missing — "
        "required for the deletion_log time-window pagination to scale"
    )

    # The migration must reference the deletion_log table and the
    # composite columns the export uses.
    text = mig_path.read_text()
    assert "deletion_log" in text
    assert "owner_id" in text
    assert "namespace" in text
    assert "executed_at" in text

    # And run_migrations must include it in the canonical list.
    src = inspect.getsource(installer_db.run_migrations)
    assert "migrations_v5_3_3_deletion_log_export_index.sql" in src


# ── Keyset cursor (#142) ─────────────────────────────────────────────────


def test_cursor_encode_decode_round_trips():
    """Opaque cursor token encodes (executed_at, id, export_as_of) and
    decodes back."""
    from mnemos.domain.portability.export import (
        _decode_deletion_log_cursor,
        _encode_deletion_log_cursor,
    )

    token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:30:00+00:00",
    )
    # Token is a non-empty url-safe string (no padding chars).
    assert isinstance(token, str)
    assert "=" not in token
    assert "/" not in token
    assert "+" not in token

    cursor_data = _decode_deletion_log_cursor(token)
    assert cursor_data["executed_at"] == "2026-05-06T12:00:00+00:00"
    assert cursor_data["id"] == "00000000-0000-0000-0000-000000000042"
    assert cursor_data["export_as_of"] == "2026-05-06T11:30:00+00:00"


def test_cursor_decode_rejects_garbage_with_400():
    """Malformed cursors must surface a clear 400 — operators won't know
    why their pagination loop broke otherwise."""
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    for bad in ("not-base64", "Zm9v", "", "===", "AAAAAAAAAA"):
        with pytest.raises(HTTPException) as exc_info:
            _decode_deletion_log_cursor(bad)
        assert exc_info.value.status_code == 400
        assert "cursor" in exc_info.value.detail.lower()


def test_cursor_decode_rejects_well_formed_b64_but_wrong_shape():
    """A cursor that decodes to JSON but lacks executed_at/id/export_as_of
    must 400."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    # Valid b64-JSON but missing required fields.
    bad_payload = json.dumps({"foo": "bar"}).encode()
    token = base64.urlsafe_b64encode(bad_payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400


def test_cursor_decode_rejects_empty_string_fields():
    """Round-2 codex finding: empty-string fields used to bypass validation
    and silently disable the keyset predicate (replaying first page).
    Must 400."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    bad_payload = json.dumps({
        "executed_at": "",
        "id": "",
        "export_as_of": "",
    }).encode()
    token = base64.urlsafe_b64encode(bad_payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400


def test_cursor_decode_rejects_invalid_uuid():
    """Non-UUID id must 400 (would otherwise reach Postgres ::uuid cast as 500)."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    bad_payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "not-a-uuid",
        "export_as_of": "2026-05-06T11:00:00+00:00",
    }).encode()
    token = base64.urlsafe_b64encode(bad_payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400


def test_cursor_decode_rejects_naive_datetime():
    """Timezone-less datetime in cursor must 400 (avoids the same
    DB-session-tz pitfall as the route-level params)."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    bad_payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00",  # naive — no tz suffix
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
    }).encode()
    token = base64.urlsafe_b64encode(bad_payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400


def test_cursor_decode_accepts_z_suffix_normalized_to_utc():
    """The 'Z' UTC shorthand normalizes correctly."""
    import base64
    import json
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00Z",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00Z",
    }).encode()
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor_data = _decode_deletion_log_cursor(token)
    # Validator preserves the original shape (no normalization to +00:00)
    # so round-tripping the cursor doesn't drift.
    assert cursor_data["executed_at"] == "2026-05-06T12:00:00Z"
    assert cursor_data["export_as_of"] == "2026-05-06T11:00:00Z"


def test_cursor_always_packs_window_fields_for_self_containment():
    """Round-3 fix: cursor always packs deletion_log_from/_to (None for
    unbounded) so subsequent pages don't fall back to request params
    for missing sides. Ensures the cursor is truly self-contained."""
    import base64
    import json
    from mnemos.domain.portability.export import (
        _decode_deletion_log_cursor,
        _encode_deletion_log_cursor,
    )

    # Both sides bounded.
    token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:00:00+00:00",
        deletion_log_from="2026-05-01T00:00:00+00:00",
        deletion_log_to="2026-05-07T00:00:00+00:00",
    )
    decoded_payload = json.loads(
        base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    )
    assert decoded_payload["deletion_log_from"] == "2026-05-01T00:00:00+00:00"
    assert decoded_payload["deletion_log_to"] == "2026-05-07T00:00:00+00:00"

    # Unbounded sides — must still be packed (as JSON null), not omitted.
    token_unbounded = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:00:00+00:00",
        deletion_log_from=None,
        deletion_log_to=None,
    )
    decoded_unbounded = json.loads(
        base64.urlsafe_b64decode(token_unbounded + "=" * (-len(token_unbounded) % 4))
    )
    assert "deletion_log_from" in decoded_unbounded
    assert "deletion_log_to" in decoded_unbounded
    assert decoded_unbounded["deletion_log_from"] is None
    assert decoded_unbounded["deletion_log_to"] is None

    # Decoder surfaces both fields (None when unbounded) so callers can
    # use them as the SOLE source of truth.
    cursor_data = _decode_deletion_log_cursor(token_unbounded)
    assert cursor_data["deletion_log_from"] is None
    assert cursor_data["deletion_log_to"] is None


def test_cursor_decoder_returns_window_fields_unconditionally():
    """Decoder always returns deletion_log_from / deletion_log_to keys
    (None when unbounded). Callers consuming the dict don't need to
    branch on key presence."""
    import base64
    import json
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    # Forward-compat: older cursors without the window fields decode
    # cleanly with both fields = None (no KeyError).
    legacy_payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
    }).encode()
    legacy_token = base64.urlsafe_b64encode(legacy_payload).decode("ascii").rstrip("=")
    cursor_data = _decode_deletion_log_cursor(legacy_token)
    assert cursor_data["deletion_log_from"] is None
    assert cursor_data["deletion_log_to"] is None


def test_route_rejects_cursor_combined_with_window_params():
    """Round-3 fix: cursor is self-contained; combining it with
    deletion_log_from/_to is ambiguous and rejected at the route."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    # The route must reject the combo with a 400.
    assert "deletion_log_cursor cannot be combined with" in src
    assert "status_code=400" in src
    # And the rejection guard must reference both window params.
    assert "deletion_log_cursor is not None" in src


def test_export_uses_cursor_as_sole_source_for_window_on_subsequent_pages():
    """Source-level guard: when the cursor is present, the window
    derivations come SOLELY from the cursor (no conditional fallback
    to request params). The route enforces that combining cursor +
    window params is rejected, and the cursor decode resolves
    cursor_dl_from / cursor_dl_to which then drive the deletion_log
    fetch unconditionally."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    src = inspect.getsource(export_mod.export_memories)
    # Cursor-decode block resolves cursor_dl_from/_to from cursor_data.
    assert 'cursor_dl_from = cursor_data["deletion_log_from"]' in src
    assert 'cursor_dl_to = cursor_data["deletion_log_to"]' in src


def test_cursor_packs_tenant_scope_and_decoder_returns_it():
    """Round-4 fix: cursor binds the page-1 tenant scope so subsequent
    pages can't silently switch owners/namespaces and apply the
    keyset position to a different tenant's data."""
    import base64
    import json
    from mnemos.domain.portability.export import (
        _decode_deletion_log_cursor,
        _encode_deletion_log_cursor,
    )

    # Bound owner + namespace.
    token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:00:00+00:00",
        effective_owner="alice",
        effective_ns="prod",
    )
    decoded_payload = json.loads(
        base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    )
    assert decoded_payload["effective_owner"] == "alice"
    assert decoded_payload["effective_ns"] == "prod"

    cursor_data = _decode_deletion_log_cursor(token)
    assert cursor_data["effective_owner"] == "alice"
    assert cursor_data["effective_ns"] == "prod"
    assert cursor_data["_has_scope"] is True

    # Cross-tenant root export — both scope sides None must still
    # round-trip and signal _has_scope=True so subsequent pages bind
    # to the cross-tenant scope.
    token_cross = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:00:00+00:00",
        effective_owner=None,
        effective_ns=None,
    )
    cursor_cross = _decode_deletion_log_cursor(token_cross)
    assert cursor_cross["effective_owner"] is None
    assert cursor_cross["effective_ns"] is None
    assert cursor_cross["_has_scope"] is True


def test_cursor_decoder_legacy_no_scope_returns_has_scope_false():
    """Forward-compat: a legacy cursor without scope keys decodes with
    _has_scope=False so callers know to fall back to request-derived
    scope (the pre-round-4 behavior)."""
    import base64
    import json
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    legacy_payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
    }).encode()
    legacy_token = base64.urlsafe_b64encode(legacy_payload).decode("ascii").rstrip("=")
    cursor_data = _decode_deletion_log_cursor(legacy_token)
    assert cursor_data["_has_scope"] is False
    assert cursor_data["effective_owner"] is None
    assert cursor_data["effective_ns"] is None


def test_cursor_decoder_rejects_non_string_scope_fields():
    """A cursor with effective_owner or effective_ns set to a
    non-string non-null value (e.g., a number) must 400 — prevents
    an attacker from injecting structured payloads."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
        "effective_owner": 42,
        "effective_ns": None,
    }).encode()
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400
    assert "effective_owner" in exc_info.value.detail


def test_cursor_decoder_rejects_empty_string_effective_owner():
    """#159: effective_owner="" is ambiguous — cursors written by
    this server use null for unscoped, never an empty string. The
    type check passed (empty str passes isinstance(str)), so the
    decoder returned "" which the SQL query would then filter on
    (no real memory has owner_id=""). Tighten to reject explicitly."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
        "effective_owner": "",
        "effective_ns": "alice-ns",
    }).encode()
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400
    assert "effective_owner" in exc_info.value.detail


def test_cursor_decoder_rejects_empty_string_effective_ns():
    """#159: same for effective_ns."""
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
        "effective_owner": "alice",
        "effective_ns": "",
    }).encode()
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as exc_info:
        _decode_deletion_log_cursor(token)
    assert exc_info.value.status_code == 400
    assert "effective_ns" in exc_info.value.detail


def test_cursor_encoder_rejects_empty_string_scope():
    """#160: defense-in-depth — the encoder must also reject
    empty-string scope so the bug can't propagate into a cursor in
    the first place. Round-trip would be caught by the decoder
    (#159), but symmetrical validation at the source is cleaner."""
    from mnemos.domain.portability.export import _encode_deletion_log_cursor

    with pytest.raises(ValueError) as exc_info:
        _encode_deletion_log_cursor(
            "2026-05-06T12:00:00+00:00",
            "00000000-0000-0000-0000-000000000001",
            export_as_of="2026-05-06T11:00:00+00:00",
            effective_owner="",
            effective_ns="alice-ns",
        )
    assert "effective_owner" in str(exc_info.value)
    assert "non-empty" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        _encode_deletion_log_cursor(
            "2026-05-06T12:00:00+00:00",
            "00000000-0000-0000-0000-000000000001",
            export_as_of="2026-05-06T11:00:00+00:00",
            effective_owner="alice",
            effective_ns="",
        )
    assert "effective_ns" in str(exc_info.value)


def test_cursor_encoder_accepts_null_scope_for_unscoped_export():
    """Regression: null scope must continue to work — that's the
    documented unscoped value for root cross-tenant exports."""
    from mnemos.domain.portability.export import (
        _decode_deletion_log_cursor,
        _encode_deletion_log_cursor,
    )

    token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000001",
        export_as_of="2026-05-06T11:00:00+00:00",
        effective_owner=None,
        effective_ns=None,
    )
    cursor_data = _decode_deletion_log_cursor(token)
    assert cursor_data["effective_owner"] is None
    assert cursor_data["effective_ns"] is None


def test_cursor_decoder_accepts_null_scope_for_unscoped_export():
    """Regression: null is the documented unscoped value (root cross-
    tenant export). Empty-string-rejection must NOT also affect null."""
    import base64
    import json
    from mnemos.domain.portability.export import _decode_deletion_log_cursor

    payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
        "effective_owner": None,
        "effective_ns": None,
    }).encode()
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor_data = _decode_deletion_log_cursor(token)
    assert cursor_data["effective_owner"] is None
    assert cursor_data["effective_ns"] is None
    # Round-3 sentinel: scope keys WERE present (just null), so
    # _has_scope is True (not a legacy scope-less cursor).
    assert cursor_data["_has_scope"] is True


def test_route_rejects_empty_owner_id_with_422():
    """#162: empty `owner_id` query string must fail-fast at the
    Query() validator. Without min_length=1, FastAPI accepts the
    empty value, it reaches export_memories, the SQL filters on
    owner_id="" (no match), and a single-page export silently
    returns empty results."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mnemos.api.dependencies import UserContext, get_current_user
    from mnemos.api.routes.portability import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )

    with TestClient(app) as client:
        r = client.get("/v1/export?owner_id=")
    assert r.status_code == 422, r.text
    body = r.json()
    # The validation detail must reference the offending field.
    detail_str = str(body)
    assert "owner_id" in detail_str


def test_route_rejects_empty_namespace_with_422():
    """#162: same for namespace."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mnemos.api.dependencies import UserContext, get_current_user
    from mnemos.api.routes.portability import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )

    with TestClient(app) as client:
        r = client.get("/v1/export?namespace=")
    assert r.status_code == 422, r.text
    body = r.json()
    detail_str = str(body)
    assert "namespace" in detail_str


def test_route_rejects_cursor_combined_with_owner_or_namespace():
    """Round-4 route guard: cursor + owner_id or cursor + namespace
    are rejected with 400 — same reason as cursor + window params."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    assert "deletion_log_cursor cannot be combined with" in src
    assert "owner_id / namespace" in src
    # Both window AND scope rejections must be present.
    assert "deletion_log_from / deletion_log_to" in src


def test_export_uses_cursor_scope_as_sole_source_on_subsequent_pages():
    """Source-level guard (round-5 restructure): when cursor carries
    scope, effective_owner / effective_ns are overridden BEFORE the
    transaction so all envelope surfaces (records, KG, sidecars,
    deletion_log) stay in the cursor-bound scope.

    Round-5 also rejects legacy scope-less cursors with 400 (no
    silent fallback) and validates non-root scope match against
    authenticated user (anti-forgery)."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    src = inspect.getsource(export_mod.export_memories)
    # Cursor scope override applied to top-level effective_owner/_ns
    # (used by all surfaces, not just deletion_log).
    assert 'effective_owner = cursor_data["effective_owner"]' in src
    assert 'effective_ns = cursor_data["effective_ns"]' in src
    # Legacy cursor rejection.
    assert "pre-round-4 export" in src or "lacks tenant-scope binding" in src
    # Non-root scope-match validation (anti-forgery).
    assert "is_root(user)" in src
    assert "scope does not match" in src


def test_export_rejects_forged_cursor_for_non_root_with_403():
    """Round-5 critical: a non-root caller cannot mint a cursor with
    a victim's owner/namespace. The cursor is unsigned base64-JSON;
    if scope didn't match the authenticated user, anyone could read
    cross-tenant deletion_log via cursor injection. Verify the
    domain rejects mismatched scope with 403."""
    import asyncio
    from fastapi import HTTPException
    from mnemos.api.dependencies import UserContext
    from mnemos.domain.portability.export import (
        _encode_deletion_log_cursor,
        export_memories,
    )

    # Forge a cursor scoped to victim "alice" / "prod".
    forged_token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000042",
        export_as_of="2026-05-06T11:00:00+00:00",
        effective_owner="alice",
        effective_ns="prod",
    )

    # Non-root user "mallory" submits the forged cursor.
    mallory = UserContext(
        user_id="mallory",
        group_ids=[],
        role="user",
        namespace="dev",
        authenticated=True,
    )

    async def run():
        # No conn needed — should reject before touching the DB.
        await export_memories(
            conn=None,
            user=mallory,
            category=None,
            limit=10,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=True,
            include_unattached_kg=False,
            mpf_version="0.2",
            deletion_log_from=None,
            deletion_log_to=None,
            deletion_log_cursor=forged_token,
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status_code == 403
    assert (
        "scope does not match" in exc_info.value.detail
        or "cross-owner" in exc_info.value.detail
        or "cross-namespace" in exc_info.value.detail
    )


def test_export_rejects_legacy_scopeless_cursor_with_400():
    """Round-5 medium: pre-round-4 cursors without scope keys are
    explicitly rejected with 400 — operators restart pagination.
    Without this, the silent fallback to request-derived scope
    would broaden a paginated export to cross-tenant for root."""
    import asyncio
    import base64
    import json
    from fastapi import HTTPException
    from mnemos.api.dependencies import UserContext
    from mnemos.domain.portability.export import export_memories

    legacy_payload = json.dumps({
        "executed_at": "2026-05-06T12:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "export_as_of": "2026-05-06T11:00:00+00:00",
        "deletion_log_from": None,
        "deletion_log_to": None,
        # No effective_owner / effective_ns keys.
    }).encode()
    legacy_token = base64.urlsafe_b64encode(legacy_payload).decode("ascii").rstrip("=")

    root_user = UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="root",
        authenticated=True,
    )

    async def run():
        await export_memories(
            conn=None,
            user=root_user,
            category=None,
            limit=10,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=True,
            include_unattached_kg=False,
            mpf_version="0.2",
            deletion_log_from=None,
            deletion_log_to=None,
            deletion_log_cursor=legacy_token,
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status_code == 400
    assert (
        "pre-round-4" in exc_info.value.detail
        or "lacks tenant-scope binding" in exc_info.value.detail
    )


def test_export_rejects_v02_cursor_on_v01_export_with_400():
    """Round-5: cursor pagination is a v0.2-only feature; passing
    a cursor with mpf_version=0.1 must surface a clear 400 — not
    silently ignore the cursor."""
    import asyncio
    from fastapi import HTTPException
    from mnemos.api.dependencies import UserContext
    from mnemos.domain.portability.export import (
        _encode_deletion_log_cursor,
        export_memories,
    )

    token = _encode_deletion_log_cursor(
        "2026-05-06T12:00:00+00:00",
        "00000000-0000-0000-0000-000000000001",
        export_as_of="2026-05-06T11:00:00+00:00",
        effective_owner="alice",
        effective_ns="prod",
    )
    user = UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="prod",
        authenticated=True,
    )

    async def run():
        await export_memories(
            conn=None,
            user=user,
            category=None,
            limit=10,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=True,
            include_unattached_kg=False,
            mpf_version=None,  # default 0.1
            deletion_log_from=None,
            deletion_log_to=None,
            deletion_log_cursor=token,
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status_code == 400
    assert "v0.2" in exc_info.value.detail or "0.2" in exc_info.value.detail


def test_export_rejects_empty_cursor_with_400():
    """Round-6 medium: an empty-string cursor `""` is treated as
    'cursor present' by the route's combo guards (`is not None`)
    but pre-fix was treated as falsy by the domain's `if
    deletion_log_cursor:` — bypassing v0.2-only enforcement, scope
    binding, and forgery rejection. Page-2 root exports that
    correctly omitted owner_id/namespace would silently fall back
    to unscoped root export. Verify the domain now 400s on empty."""
    import asyncio
    from fastapi import HTTPException
    from mnemos.api.dependencies import UserContext
    from mnemos.domain.portability.export import export_memories

    root_user = UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="root",
        authenticated=True,
    )

    async def run():
        await export_memories(
            conn=None,
            user=root_user,
            category=None,
            limit=10,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=True,
            include_unattached_kg=False,
            mpf_version="0.2",
            deletion_log_from=None,
            deletion_log_to=None,
            deletion_log_cursor="",
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run())
    assert exc_info.value.status_code == 400
    assert "non-empty" in exc_info.value.detail.lower()


def test_route_query_param_rejects_empty_cursor():
    """Defense in depth: the route's `Query(min_length=1)` should
    block an empty cursor at the FastAPI validation layer (before
    the domain runs) — caller sees 422, not 400. Source-level
    guard since we can't easily exercise the route here."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    assert "min_length=1" in src


def test_export_documents_late_commit_caveat_with_mitigation():
    """Round-6 high (acknowledged limitation): the docstring must
    spell out the late-commit window AND a concrete operational
    mitigation. Source-level guard so future refactors don't
    silently strip the caveat."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    doc = inspect.getdoc(export_mod.export_memories)
    assert doc is not None
    assert "late-commit" in doc.lower()
    # Mitigation must name at least one of: quiesce / cross-check /
    # back-to-back. Operators reading this should know what to do.
    assert (
        "quiesce" in doc.lower()
        or "back-to-back" in doc.lower()
        or "cross-check" in doc.lower()
    )
    # And explicitly point to v0.3 as the proper fix.
    assert "v0.3" in doc or "materialized" in doc.lower()


def test_route_rejects_deletion_log_params_without_sidecars():
    """Round-7 medium: deletion_log_cursor / from / to params
    accepted with include_sidecars=false silently no-op (the
    deletion_log fetch is gated under the sidecars block). An
    operator paginating in a loop would terminate believing they
    drained the audit log. Source-level guard for the route's
    explicit 400."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    assert "deletion_log_params_present" in src
    assert "include_sidecars=true" in src
    assert "silently ignored" in src or "terminate prematurely" in src


def test_route_rejects_deletion_log_params_on_v01():
    """Round-7 medium: deletion_log params on mpf_version=0.1 (or
    default) silently no-op since v0.1 envelopes don't carry the
    sidecar. Route now 400s explicitly."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    src = inspect.getsource(routes_mod.export_memories)
    assert "v0.2-only fields" in src
    assert "mpf_version=0.2" in src


def test_envelope_carries_deletion_log_next_cursor_when_set():
    """Envelope schema accepts the new optional field; v0.1 backward
    compat preserved (None drops via response_model_exclude_none)."""
    from mnemos.domain.portability.schemas import MPFEnvelope

    env_with = MPFEnvelope(
        mpf_version="0.2.0",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=0,
        records=[],
        deletion_log_next_cursor="opaque-token-here",
    )
    dumped = env_with.model_dump(exclude_none=True)
    assert dumped["deletion_log_next_cursor"] == "opaque-token-here"

    # v0.1 envelope without the cursor — must NOT carry the field.
    env_v0_1 = MPFEnvelope(
        mpf_version="0.1.1",
        exported_at="2026-05-06T00:00:00+00:00",
        record_count=0,
        records=[],
    )
    dumped_v0_1 = env_v0_1.model_dump(exclude_none=True)
    assert "deletion_log_next_cursor" not in dumped_v0_1


def test_repo_fetch_deletion_log_accepts_cursor_kwargs():
    """Source-level guard: fetch_deletion_log_for_export accepts
    cursor_executed_at / cursor_id / export_as_of and emits the
    (executed_at, id) keyset comparison + export_as_of upper bound."""
    import inspect
    from mnemos.db import portability_repo

    sig = inspect.signature(portability_repo.fetch_deletion_log_for_export)
    assert "cursor_executed_at" in sig.parameters
    assert "cursor_id" in sig.parameters
    assert "export_as_of" in sig.parameters

    src = inspect.getsource(portability_repo.fetch_deletion_log_for_export)
    # Tuple comparison shape (the keyset trick).
    assert "(executed_at, id) >" in src
    # Must use timestamptz + uuid casts to avoid silent type coercion.
    assert "::timestamptz" in src
    assert "::uuid" in src
    # Snapshot anchor predicate.
    assert "executed_at <= " in src


def test_route_export_exposes_deletion_log_cursor_param():
    """Source-level guard: GET /v1/export exposes deletion_log_cursor
    and threads it into the domain function."""
    import inspect
    from mnemos.api.routes import portability as routes_mod

    sig = inspect.signature(routes_mod.export_memories)
    assert "deletion_log_cursor" in sig.parameters

    src = inspect.getsource(routes_mod.export_memories)
    assert "deletion_log_cursor=deletion_log_cursor" in src


def test_export_emits_next_cursor_on_overflow():
    """When fetch_deletion_log_for_export returns cap+1 rows, the export
    pipeline slices to the cap, encodes the boundary row's
    (executed_at, id) into next_cursor, and does NOT raise 413."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    src = inspect.getsource(export_mod.export_memories)
    # Source-level: presence of next_cursor wiring.
    assert "_encode_deletion_log_cursor" in src
    assert "deletion_log_next_cursor" in src
    # Slice to cap, not the verbatim `len(dl_rows) > cap` raise.
    assert "_EXPORT_SIDECAR_HARD_LIMIT" in src


def test_export_413_only_fires_when_cursor_extraction_fails():
    """Source-level: the 413 path for deletion_log only triggers when
    the boundary row lacks executed_at/id (defensive fallback). Normal
    operation emits next_cursor and slices."""
    import inspect
    from mnemos.domain.portability import export as export_mod

    src = inspect.getsource(export_mod.export_memories)
    # Look for the defensive guard branch.
    assert "last_executed_at is None or last_id is None" in src
    # And the 413 fallback inside that guard.
    assert "_enforce_sidecar_cap(dl_rows" in src

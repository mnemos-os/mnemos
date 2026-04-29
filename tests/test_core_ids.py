"""Direct coverage for canonical ID helpers."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import HTTPException

from mnemos.core.ids import (
    IDNamespace,
    caller_scoped_id,
    caller_scoped_uuid,
    new_memory_id,
    parse_uuid_or_400,
    parse_uuid_or_404,
)


def test_parse_uuid_or_400_valid_returns_canonical_string():
    value = "550E8400-E29B-41D4-A716-446655440000"

    assert parse_uuid_or_400(value, "memory") == "550e8400-e29b-41d4-a716-446655440000"


def test_parse_uuid_or_400_invalid_raises_with_what_in_detail():
    with pytest.raises(HTTPException) as exc:
        parse_uuid_or_400("not-a-uuid", "memory")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid memory id format"


def test_parse_uuid_or_404_invalid_raises_not_found():
    with pytest.raises(HTTPException) as exc:
        parse_uuid_or_404("not-a-uuid", "webhook")

    assert exc.value.status_code == 404


def test_caller_scoped_uuid_is_deterministic():
    first = caller_scoped_uuid(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
        extra="version-1",
    )
    second = caller_scoped_uuid(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
        extra="version-1",
    )

    assert first == second
    assert str(uuid.UUID(first)) == first


def test_caller_scoped_uuid_changes_with_namespace():
    base = caller_scoped_uuid(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
    )
    different = caller_scoped_uuid(
        caller_owner="alice",
        caller_namespace="other-ns",
        envelope_id="env-1",
    )

    assert base != different


def test_caller_scoped_id_is_deterministic():
    first = caller_scoped_id(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
        content="body",
    )
    second = caller_scoped_id(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
        content="body",
    )

    assert first == second


def test_caller_scoped_id_has_mnemos_hex_prefix():
    value = caller_scoped_id(
        caller_owner="alice",
        caller_namespace="alice-ns",
        envelope_id="env-1",
        content="body",
    )

    assert value.startswith("mnemos_")
    assert re.fullmatch(r"mnemos_[0-9a-f]{32}", value)


def test_new_memory_id_has_timestamp_and_hex_tail():
    value = new_memory_id()

    assert value.startswith("mem_")
    match = re.fullmatch(r"mem_(\d{13,})_([0-9a-f]{6})", value)
    assert match is not None
    assert int(match.group(1)) > 0


def test_new_memory_id_is_unique_across_many_calls():
    values = {new_memory_id() for _ in range(100)}

    assert len(values) == 100


def test_idnamespace_documents_expected_prefixes():
    assert IDNamespace.MEMORY == "mem"
    assert IDNamespace.KG_TRIPLE == "kgt"
    assert IDNamespace.DREAM == "drm"
    assert IDNamespace.VERSION == "ver"
    assert IDNamespace.COMPRESSION_MANIFEST == "cpm"

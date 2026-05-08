"""Pydantic schemas and MPF constants."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mnemos._version import __version__ as SOURCE_VERSION

MPF_VERSION = "0.1.1"
MPF_VERSION_PREFIX = "0.1."
MPF_VERSION_V0_2 = "0.2.0"
MPF_VERSION_PREFIX_V0_2 = "0.2."
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "mnemos"


class MPFRecord(BaseModel):
    """A single record in an MPF envelope. Discriminated by ``kind``.

    The optional v0.2 fields (provenance, valid_time_*, transaction_time)
    are sibling-of-payload — they live at the record level per the v0.2
    spec, not nested in the payload. v0.1 emission omits them entirely;
    v0.2 emission populates them from existing row fields.
    """

    id: str
    kind: str
    payload_version: str
    payload: Dict[str, Any]
    # v0.2-only record-level fields. Optional so v0.1 envelopes serialize
    # cleanly via exclude_none.
    provenance: Optional[Dict[str, Any]] = None
    valid_time_start: Optional[str] = None
    valid_time_end: Optional[str] = None
    transaction_time: Optional[str] = None

    def model_dump_for_envelope(self, version: str) -> Dict[str, Any]:
        """Serialize with v0.2 fields when version starts with 0.2, else strip."""
        if version.startswith(MPF_VERSION_PREFIX_V0_2):
            return self.model_dump(exclude_none=True)
        return self.model_dump(
            exclude_none=True,
            exclude={"provenance", "valid_time_start", "valid_time_end", "transaction_time"},
        )


class MPFEnvelope(BaseModel):
    """An MPF v0.1.x or v0.2.x file envelope."""

    mpf_version: str = MPF_VERSION
    source_system: Optional[str] = SOURCE_SYSTEM
    source_version: Optional[str] = SOURCE_VERSION
    source_instance: Optional[str] = None
    exported_at: Optional[str] = None
    record_count: Optional[int] = None
    records: List[MPFRecord] = Field(default_factory=list)
    kg_triples: Optional[List[Dict[str, Any]]] = None
    memory_versions: Optional[List[Dict[str, Any]]] = None
    compression_manifest: Optional[List[Dict[str, Any]]] = None
    # v0.2 deletion_log sidecar — populated by the export pipeline when
    # mpf_version=0.2 + include_sidecars=true (scoped by owner/namespace,
    # capped at _EXPORT_SIDECAR_HARD_LIMIT). v0.1 envelopes do not carry
    # this field; FastAPI's response_model_exclude_none drops it on v0.1
    # responses. Each entry maps to a deletion_log table row with
    # MNEMOS-specific extras (request_kind, requested_at, source[]) in
    # the entry's metadata sub-dict for round-trip fidelity.
    deletion_log: Optional[List[Dict[str, Any]]] = None
    # Keyset pagination cursor for the next deletion_log page. Present
    # only when the current page hit the per-envelope cap; clients pass
    # this back as `deletion_log_cursor` to fetch the next chunk. Opaque
    # base64-JSON (executed_at, id). v0.1 envelopes never carry this.
    deletion_log_next_cursor: Optional[str] = None


class ImportStats(BaseModel):
    """Summary of an import run."""

    imported: int
    skipped: int
    failed: int
    unsupported_kinds: Dict[str, int] = Field(default_factory=dict)
    sidecars_imported: Dict[str, int] = Field(default_factory=dict)
    sidecars_skipped: Dict[str, int] = Field(default_factory=dict)
    sidecars_failed: Dict[str, int] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)

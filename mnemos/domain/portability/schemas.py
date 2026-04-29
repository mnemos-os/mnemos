"""Pydantic schemas and MPF constants."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mnemos._version import __version__ as SOURCE_VERSION

MPF_VERSION = "0.1.1"
MPF_VERSION_PREFIX = "0.1."
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "mnemos"


class MPFRecord(BaseModel):
    """A single record in an MPF envelope. Discriminated by ``kind``."""

    id: str
    kind: str
    payload_version: str
    payload: Dict[str, Any]


class MPFEnvelope(BaseModel):
    """An MPF v0.1.x file envelope."""

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

"""APOLLO schema ABC.

Each schema implements (detect, encode, narrate). Detection is
rule-based regex and cheap — runs on every schema-aware contest
eligibility check. Encoding produces the dense LLM-parseable form
(typed key:value). Narration expands the dense form back to prose
for human reading.

v3.3 S-IC narration is rule-based; S-II wires a small-LLM readback
cached per memory_id.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class DetectionResult:
    """Schema.detect return value when content matches a schema.

    Populated by the schema; consumed by APOLLOEngine.compress to
    build the CompressionResult manifest.
    """

    schema_id: str
    schema_version: str
    fields: Dict[str, Any]      # structured fields extracted from content
    confidence: float           # [0.0, 1.0] — self-assessed match confidence
    original_length: int        # source content length in chars
    notes: Optional[str] = None  # operator-readable match summary


class Schema(ABC):
    """Base class for APOLLO schemas.

    Subclasses MUST set class-level `id` and `version`, and implement
    `detect` + `encode`. `narrate` has a pass-through default that
    returns the encoded form verbatim; concrete schemas override with
    a rule-based readback in S-IC and the LLM replaces in S-II.
    """

    id: str = ""
    version: str = ""

    @abstractmethod
    def detect(self, content: str) -> Optional[DetectionResult]:
        """Return a DetectionResult if content matches this schema,
        else None. Must be side-effect-free so APOLLOEngine can
        safely invoke during supports() and again during compress()
        without double-booking state.
        """
        raise NotImplementedError

    @abstractmethod
    def encode(self, match: DetectionResult) -> str:
        """Serialize fields to the schema's dense LLM-parseable form.
        The encoding MUST be deterministic so two identical source
        memories produce identical encoded strings (important for
        DAG content-addressing in S-II)."""
        raise NotImplementedError

    def narrate(self, encoded: str) -> str:
        """Expand dense form back to prose for human reading.

        Default: return the encoded form verbatim (dense form is
        often LLM-readable as-is during S-IC). Concrete schemas
        override with a rule-based readback; S-II replaces with a
        small-LLM call and caches per memory_id.
        """
        return encoded

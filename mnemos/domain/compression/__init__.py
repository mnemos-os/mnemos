"""
Compression Module

Provides the core plugin CompressionEngine ABC and contest framework.
APOLLO + ARTEMIS engines are optional extras and are imported lazily.

- APOLLO: schema-aware dense encoding for LLM-to-LLM consumption
  (v3.3 S-IC: PortfolioSchema as the first concrete schema with
  rule-based detection; S-II adds LLM fallback, narration endpoint,
  judge-LLM scoring, decision/person/event schemas).
- ARTEMIS: CPU-only extractive with identifier preservation,
  labeled-block handling, and evidence-based self-scoring.
- QualityAnalyzer: Quality manifest generation.
"""

from .base import (
    BASE_CHUNK_RATIO,
    MIN_CHUNK_RATIO,
    SAFETY_MARGIN,
    SUMMARIZATION_OVERHEAD_TOKENS,
    CompressionEngine,
    CompressionRequest,
    GPUIntent,
    IdentifierPolicy,
)
from .base import CompressionResult as EngineCompressionResult
from .contest import (
    BUILT_IN_PROFILES,
    ContestCandidate,
    ContestOutcome,
    ScoringProfile,
    load_scoring_profile,
    run_contest,
)
from .contest_store import persist_contest
from .quality_analyzer import QualityAnalyzer, QualityManifest

_OPTIONAL_EXPORTS = {
    "APOLLOEngine": (".apollo", "APOLLOEngine"),
    "APOLLOSchema": (".apollo_schemas", "Schema"),
    "PortfolioSchema": (".apollo_schemas", "PortfolioSchema"),
    "ARTEMISEngine": (".artemis", "ARTEMISEngine"),
}


def __getattr__(name: str):
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(name)
    module_name, attr_name = _OPTIONAL_EXPORTS[name]
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

__all__ = [
    # v3.1 competitive-selection plugin ABC
    "CompressionEngine",
    "CompressionRequest",
    "EngineCompressionResult",
    "GPUIntent",
    "IdentifierPolicy",
    "BASE_CHUNK_RATIO",
    "MIN_CHUNK_RATIO",
    "SAFETY_MARGIN",
    "SUMMARIZATION_OVERHEAD_TOKENS",
    # v3.1 competitive-selection orchestrator
    "ScoringProfile",
    "BUILT_IN_PROFILES",
    "load_scoring_profile",
    "ContestCandidate",
    "ContestOutcome",
    "run_contest",
    "persist_contest",
    "QualityAnalyzer",
    "QualityManifest",
    # v3.3 going-forward stack: APOLLO (schema-aware) + ARTEMIS (extractive)
    "APOLLOEngine",
    "APOLLOSchema",
    "PortfolioSchema",
    "ARTEMISEngine",
]

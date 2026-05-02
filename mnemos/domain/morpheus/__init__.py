"""MORPHEUS — dream-state memory consolidation.

NAMING CONVENTION (locked in v3.4):
  - `morpheus` is the **internal** identifier used throughout the
    codebase — this Python module, REST routes (`/v1/morpheus/*`),
    database tables (`morpheus_runs`, `morpheus_clusters`,
    `morpheus_run_id` column), and Python class names (`MorpheusRun`,
    `MorpheusCluster`, etc.).
  - **`APOLLO S-IVB`** is the **release / marketing identifier** used
    in roadmaps, charters, announcements, and conceptual references
    (Saturn V third-stage = trans-lunar burn = leaves convergent
    compression for divergent dreams).
  - Both names refer to the same subsystem. They coexist by design,
    not as transitional naming. No rename is planned.
  - When you see "APOLLO S-IVB phase N" in a charter or roadmap, it
    maps to "MORPHEUS slice N" in code. Phases 1-2 ship in v3.4
    (REPLAY → CLUSTER → SYNTHESISE → COMMIT pipeline). Slice 3 adds
    the optional CONSOLIDATE mutation phase; EXTRACT / ARCHIVE remain
    queued per the charters.

The off-peak worker that processes accumulated memory into shaped form.
Named after the Greek god of dreams (μορφεύς, "the one who shapes")
and the Matrix character of the same name — both meanings land:
MORPHEUS shapes raw memories into clearer summaries, and (in later
slices) wakes the corpus from its raw-data simulation into something
the operator can actually use.

Architecture per GRAEAE consensus (consultation 2026-04-25):

  v1 — slice 1 (this scaffold)
    * morpheus_runs table + per-row morpheus_run_id tagging.
    * Runner skeleton; phases stubbed but the audit + rollback shape
      is real.
    * Admin API: list runs, get run details, manually trigger,
      rollback by run_id.

  v1 — slice 2 (synthesis)
    * REPLAY: scan memories from last N hours.
    * CLUSTER: cosine-similarity over pgvector embeddings (no LLM).
    * SYNTHESISE: per-cluster LLM pass producing summary memories.
    * COMMIT: insert with provenance='morpheus_local',
      morpheus_run_id=<run>, source_memories=[<original ids>].

  v2 (mutation paths — operator-gated)
    * CONSOLIDATE: merge near-duplicate clusters into a canonical
      with permission_mode=400 read-only pointers on originals.
    * EXTRACT: KG triples mined from verbatim_content.
    * ARCHIVE: cold-set rotation (PERSEPHONE subsystem).

Rollback contract: every change tags morpheus_run_id; undo is
DELETE FROM memories WHERE morpheus_run_id = X for run-created rows.
Mutation paths additionally restore in-place audit state, including
consolidated_into pointers and pre-consolidation permission modes.
"""

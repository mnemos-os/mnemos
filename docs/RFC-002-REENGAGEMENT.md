# RFC-002 Re-engagement Memo: MNEMOS, MemPalace, MPF, and MIF

To the MemPalace working group:

This memo is a request to re-open the RFC-002 interoperability thread. Time has
passed since the original discussion, and the MNEMOS side of the conversation
has changed materially enough that the old framing is no longer the right one.
The short version is simple: MNEMOS now has shipped evidence for portability,
MemPalace-compatible operation, schema-compatibility checks, derived-memory
pipelines, and cold archival. The next step should be a practical interop pass,
not another abstract comparison of memory systems.

## What Changed on the MNEMOS Side

First, MNEMOS v3.4 CHARON shipped the full MPF v0.1.x sidecar surface. MPF now
handles native `memory` records plus `kg_triples`, `memory_versions`, and
`compression_manifest` sidecars. That matters for RFC-002 because the migration
surface is no longer only "export some text and hope the receiver can rebuild
meaning." Version history, graph edges, and compression outputs can travel with
the memory payload when the caller has the right authority.

Second, KNOSSOS phase 1 shipped as a stdio MCP shim that speaks MemPalace tool
names against a MNEMOS backend. The current phase-1 surface implements 16
MemPalace-compatible tool names and keeps the vocabulary stable: wings, rooms,
drawers, tunnels, diaries, and graph operations are translated onto MNEMOS
owner, namespace, category, memory, and KG primitives. The implementation and
operator framing are documented in [docs/KNOSSOS.md](KNOSSOS.md).

Third, federation now has a schema-compatibility preflight. A peer can call
`GET /v1/federation/schema` and receive `mnemos_version`,
`schema_signature`, and `migrations_fingerprint` before deciding whether to
pull. This is deliberately modest, but it solves an important interop problem:
the receiver can distinguish "same protocol, compatible schema" from "same API
surface, drifted migration lineage" before it starts importing remote state.

Fourth, MORPHEUS slices 2, 3, and 4 are now in the shipped line. The pipeline is
REPLAY -> CLUSTER -> CONSOLIDATE -> SYNTHESISE -> EXTRACT, with per-row
`morpheus_run_id` tagging so run-created or run-mutated rows can be rolled back
without crossing into other runs. This gives MNEMOS a concrete answer for
derived memory: synthesis and extraction are not just notes in a roadmap; they
are recorded, scoped, and reversible.

Fifth, PERSEPHONE shipped the archival subsystem. Cold-set rotation moves
unrecalled memories into a zstd-compressed `memory_archive` table while leaving
live stub pointers in `memories`. Recall-tracking columns drive eligibility,
archive markers remain visible to federation, and restore is explicit rather
than silently mutating reads.

## MIF Alignment Posture

MNEMOS is not trying to compete with Zircote's MIF or with MemPalace's
portability format work. MPF v0.1.1 is frozen as the MNEMOS-side interchange
shape while the ecosystem converges. The posture is "align to MIF," not "fork
the portability conversation."

That distinction matters. KNOSSOS already speaks MemPalace's MCP protocol
because the user-facing tool vocabulary has value. MPF exists to move MNEMOS
state safely while MIF matures as the broader memory interchange format. If MIF
can absorb the lessons from MPF sidecars, version DAG preservation, compression
manifests, and federation preflight, MNEMOS should contribute those lessons
there. The goal is fewer formats over time, not another permanent dialect.

## Concrete Re-engagement Proposals

1. Build a joint smoke-test suite for cross-system fidelity.

   The test should round-trip a small but adversarial corpus through:
   MNEMOS -> MPF -> MemPalace -> MIF -> MNEMOS. The corpus should include plain
   memories, KG triples, updated memories with version history, compression
   artifacts, deleted or archived markers where each side can represent them,
   and MemPalace-native metadata that must survive without silent loss. The
   point is not to declare one format superior. The point is to validate the
   fidelity claims both projects make, identify fields that need explicit
   mapping, and produce a regression suite future adapters can run.

2. Cross-link KNOSSOS as a MemPalace operator option.

   MemPalace remains a strong local-first tool. KNOSSOS is for the point where a
   user or team outgrows that scope and needs shared namespaces, HTTP APIs,
   ownership, federation, version DAGs, audit trails, and archival without
   retraining their agents on new tool names. A short MemPalace-side note could
   say: if you want the MemPalace protocol but a server-backed MNEMOS datastore,
   use KNOSSOS. The MNEMOS docs can reciprocate by presenting MemPalace as the
   right default for local-first users.

3. Co-author an interop spec for MemPalace tool-name semantics.

   The practical gap is not just file formats. MCP adapters need precise tool
   semantics: required arguments, response keys, error behavior, id stability,
   graph traversal meaning, and what "wing" maps to when the backend has
   separate ownership and namespace axes. A joint spec would let KNOSSOS-style
   adapters from any backend become mechanical. MemPalace would retain the
   canonical vocabulary; MNEMOS would contribute the adapter and server-backed
   semantics it has already had to define.

## Ask

Please re-open RFC-002 on whichever forum the working group prefers: GitHub
issues, a mailing list thread, a shared design document, or a dedicated
interop call. The MNEMOS side can bring shipped code, docs, and test corpus
proposals. The best next artifact is a narrow interop matrix with executable
smoke tests, not a broad manifesto.

---

Author: Jason Perlow <jperlow@gmail.com>

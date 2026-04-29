# Memory Portability Format (MPF)

**A schema-versioned JSON envelope for moving agent memory and parsed
documents between systems.** MNEMOS defines it. Other RAG memory
systems (MemPalace, Mem0, Letta, Zep) can produce or consume it.
Document-ingest pipelines (docling, markitdown) can target it as an
output format without changing their own schemas.

This document is a **spec**, not a marketing pitch. Fields are
normative, versioning is explicit, and every interop mapping has a
table saying what maps to what and what gets lost.

The envelope is deliberately **compositional**: one `records[]` array
holds a discriminated union of record kinds (`document`, `memory`,
`fact`, `event`), each with its own payload schema. MPF does not
redefine what a parsed document or a memory record looks like — it
wraps them. DoclingDocument v1.x rides inside MPF unchanged. MNEMOS
memories ride inside MPF in their native shape. Mem0-style extracted
facts ride inside MPF as first-class records without needing to pretend
to be documents.

---

## Why this format exists

Three concrete problems it solves:

1. **Migrate MNEMOS between deployments** without in-place schema
   pain — dump, transform if needed, load on the target.
2. **Import into MNEMOS from other memory systems** so users who
   outgrow MemPalace / Mem0 / Letta / Zep have a graduation path
   that preserves their existing content.
3. **Move parsed documents between RAG systems** without each
   system re-running docling. Docling emits MPF; any consumer
   ingests it.

Non-goals:
- Not a wire protocol for synchronous replication. Federation
  (pull-based, content-addressed) is a separate RFC. Dual-commit
  fan-out is a third. Both can use MPF as their payload shape, but
  MPF itself is a file-level format, not a transport.
- Not an embedding interchange format. Embeddings are optional,
  model-specific, and covered in the "Embeddings" section — if you
  change embedding models between export and import, embeddings get
  regenerated, not interpreted.
- Not a replacement for DoclingDocument. MPF sits above it. A
  `kind: "document"` record's `payload` is an unmodified DoclingDocument
  v1.x object.

---

## Relationship to DoclingDocument

[DoclingDocument](https://docling-project.github.io/docling/concepts/docling_document/)
is the canonical per-document JSON in the docling ecosystem:
SemVer-versioned (v1.10.0 at the time of this spec), Pydantic-modeled,
JSON-Pointer–referenced, cross-language-consumed (`docling-ts`,
`docling-java`, `docling-langchain`). Docling's scope is **parsing**:
turn PDFs / Word / HTML into a structured document tree with
provenance.

Docling does not define a corpus-level envelope, embedding round-trip,
KG triples, compression manifests, or a cross-RAG interchange format.
MPF fills exactly that gap:

| Layer | Concern | Owned by |
|---|---|---|
| Per-document schema | Structure, provenance, layout, hierarchy | DoclingDocument |
| Corpus envelope | Bundling, cross-record relations, sidecars | MPF |
| Retrieval sidecars | Embeddings, KG triples, compression | MPF |
| Memory payload schema | Agent memory semantics | Per-system (MNEMOS, Mem0, etc.) |

A docling user producing MPF writes
`{kind: "document", payload: <DoclingDocument.model_dump()>}` —
literally one wrapper object around an unmodified payload. A MPF
consumer that doesn't understand `kind: "document"` skips those records
and processes the `kind: "memory"` and `kind: "fact"` records it does
know about. This is deliberate forward/backward compatibility.

---

## Design principles

1. **Schema-versioned at the envelope level.** Every file declares
   `mpf_version`. Importers implement per-version readers. Old readers
   fail loudly on newer envelopes rather than silently dropping unknown
   fields.
2. **Payload-versioned at the record level.** Every record carries
   `payload_version` so consumers can tell when it's safe to skip an
   unknown payload vs when it's a hard parse error. DoclingDocument
   payloads use docling's SemVer; MNEMOS memory payloads use MNEMOS's.
3. **Discriminated union over parallel arrays.** A single `records[]`
   scales to new record kinds without growing new top-level keys each
   time an ecosystem member adds a concept (Mem0's `fact`, Letta's
   `agent_state`, Zep's `event`).
4. **Sidecars are record-ID-keyed.** Embeddings, compression manifests,
   KG triples, and cross-record relations all reference records by
   stable `id`. They live at the envelope level, not inline on records,
   because the same embedding model can apply across both documents and
   memories.
5. **Human-readable by default.** UTF-8 JSON. No base64 blobs in the
   hot path except where unavoidable (embedding vectors — see sidecar
   section).
6. **Streaming-friendly for large exports.** Single-file JSON is
   canonical for small exports. JSONL (one record per line) variant for
   corpora above ~100 MB.
7. **Losslessly round-trippable within a version.** Export on
   `mpf_version=0.1`, import on `mpf_version=0.1` → byte-identical
   payloads, preserved IDs, preserved timestamps, preserved relations.
8. **Forward-compatible unknown-kind parsing.** Consumers MUST accept
   unknown `kind` values and skip those records rather than crashing.
   This is the ratchet that lets the ecosystem grow new kinds without
   breaking existing consumers.

---

## Envelope at a glance

```jsonc
{
  "mpf_version": "0.1.0",
  "source_system": "mnemos",
  "source_version": "4.0.0",
  "source_instance": "pythia.internal",         // optional diagnostic
  "source_commit": "1838507",                   // optional diagnostic
  "exported_at": "2026-04-23T15:00:00Z",
  "record_count": 6667,

  "records": [
    // kind: document — DoclingDocument payload, unmodified
    {
      "id": "doc_01JXYZ",
      "kind": "document",
      "payload_version": "1.10.0",
      "payload": {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "name": "Q1-earnings.pdf",
        "origin": { "mimetype": "application/pdf",
                    "binary_hash": 13242342434234,
                    "filename": "Q1-earnings.pdf" },
        "texts": [ /* ... */ ],
        "tables": [ /* ... */ ],
        "body": { /* ... */ }
      }
    },

    // kind: memory — MNEMOS-style memory record
    {
      "id": "mem_01JAB2",
      "kind": "memory",
      "payload_version": "mnemos-3.1",
      "payload": {
        "content": "Hermes gateway depends on MNEMOS API at :5002.",
        "category": "solutions",
        "subcategory": "auth",
        "created": "2026-01-15T10:30:00Z",
        "updated": "2026-01-15T10:30:00Z",
        "owner_id": "alice",
        "namespace": "team-a",
        "permission_mode": 600,
        "source_model": "claude-4-7-opus",
        "source_provider": "anthropic",
        "metadata": { "source_agent": "claude-code" }
      }
    },

    // kind: fact — Mem0-style extracted triple/claim
    {
      "id": "fact_01JCD3",
      "kind": "fact",
      "payload_version": "mpf-0.1",
      "payload": {
        "statement": "Paris is the capital of France.",
        "subject": "Paris",
        "predicate": "capitalOf",
        "object": "France",
        "confidence": 0.98,
        "created": "2026-04-23T12:00:00Z"
      }
    },

    // kind: event — Zep/Letta-style session/turn event
    {
      "id": "evt_01JEF4",
      "kind": "event",
      "payload_version": "mpf-0.1",
      "payload": {
        "event_type": "session_turn",
        "session_id": "sess-xyz",
        "actor": "user",
        "content": "How do I deploy MNEMOS on CERBERUS?",
        "occurred_at": "2026-04-23T11:45:00Z"
      }
    }
  ],

  "kg_triples": [
    {
      "id": "trip_01JGH5",
      "subject_id": "mem_01JAB2",                // may be a record id ...
      "predicate": "depends_on",
      "object_literal": "mnemos-api",            // ... OR a literal
      "valid_from": "2026-04-21T00:00:00Z",
      "valid_until": null,
      "created": "2026-04-21T14:05:00Z",
      "owner_id": "alice",
      "metadata": { "confidence": 0.95 }
    }
  ],

  "relations": [                                 // cross-record provenance
    {
      "from": "mem_01JAB2",
      "rel": "derived_from",
      "to": "doc_01JXYZ",
      "created": "2026-04-23T12:05:00Z"
    }
  ],

  "compression_manifest": [                      // record-ID-keyed
    {
      "record_id": "mem_01JAB2",
      "engine_id": "apollo",
      "compressed_content": "<condensed form>",
      "compression_ratio": 0.28,
      "composite_score": 0.61,
      "scoring_profile": "balanced",
      "selected_at": "2026-04-23T14:00:00Z",
      "winner_contest_id": "ctst_aaa"
    }
  ],

  "compression_candidates": [                    // full audit log, optional
    // One row per engine attempt per contest — see MNEMOS v3.1 contest.
  ],

  "memory_versions": [                           // optional DAG history
    {
      "id": "ver_001",
      "record_id": "mem_01JAB2",
      "version_num": 1,
      "commit_hash": "3d4e2f...",
      "branch": "main",
      "parent_version_id": null,
      "content": "<prior content>",
      "snapshot_at": "2026-01-15T10:30:00Z",
      "change_type": "create"
    }
  ],

  "embeddings": {
    "model": "nomic-embed-text",
    "dim": 768,
    "encoding": "sidecar",                       // or "inline_base64_f32"
    "sidecar_file": "embeddings.parquet"         // if encoding=sidecar
  },

  "attestations": []                             // reserved for v0.2+
}
```

---

## Record envelope fields

| Field | Required | Description |
|---|---|---|
| `id` | **required** | Stable, unique identifier. MUST be unique within the envelope. Preserved on round-trip. Recommendation: ULID or UUID v4. |
| `kind` | **required** | One of the registered kinds (see registry below), or a new value a producer coins. |
| `payload_version` | **required** | Version of the payload schema. For `kind: document`, the DoclingDocument version. For `kind: memory`, the producing system's native version (e.g. `"mnemos-3.1"`). |
| `payload` | **required** | Opaque to the envelope. The payload schema is owned by the payload_version. |

Consumers MUST NOT rewrite `id` or `payload` during round-trip. They
MAY enrich `payload` on import if the payload_version supports additive
extension (MNEMOS does).

---

## Kind registry (v0.1)

The MPF spec maintains a small curated list of record kinds in
`docs/mpf_kinds.md`. v0.1 seeds it with four kinds. New kinds require a
spec PR plus reference payload schema.

| Kind | Payload schema source | Intended for |
|---|---|---|
| `document` | DoclingDocument v1.x (docling-core) | Parsed documents from docling / markitdown / similar ingest pipelines |
| `memory` | Producing system's native (e.g. `mnemos-3.1`) | Agent/RAG memory records with category, provenance, relations |
| `fact` | MPF-defined (`mpf-0.1`) | Extracted atomic claims/triples (Mem0-style). See `payload.subject` / `predicate` / `object` / `statement` / `confidence`. |
| `event` | MPF-defined (`mpf-0.1`) | Session turns, agent actions, temporal events (Zep/Letta-style) |

### Coining new kinds

Producers MAY emit records with custom `kind` values before a spec PR
lands. Consumers that don't understand the kind MUST skip the record
without erroring. Custom kinds SHOULD be namespaced (`acme.observation`)
to make governance easier when they're promoted to the registry.

---

## Required vs optional (envelope level)

| Field | Required? | Default if missing |
|---|---|---|
| `mpf_version` | **required** | (parse error if absent) |
| `exported_at` | **required** | (parse error if absent) |
| `records` | **required** | (may be empty `[]`) |
| `records[].id` | **required** | (parse error if absent) |
| `records[].kind` | **required** | (parse error if absent) |
| `records[].payload_version` | **required** | (parse error if absent) |
| `records[].payload` | **required** | (parse error if absent) |
| `source_system` / `source_version` / `source_instance` / `source_commit` | optional | (diagnostic only) |
| `record_count` | optional | (computed from `records.length` if absent) |
| `kg_triples` | optional | `[]` |
| `relations` | optional | `[]` |
| `compression_manifest` | optional | `[]` — regenerate on import via contest |
| `compression_candidates` | optional | `[]` — audit history lost |
| `memory_versions` | optional | `[]` — DAG history lost |
| `embeddings` | optional | regenerated on import |
| `attestations` | optional | `[]` (reserved for v0.2+ signed exports) |

---

## MPF versions

| MPF version | Envelope additions | Kind registry additions |
|---|---|---|
| `0.1` | Initial envelope, `records[]` + discriminator, sidecars, `relations[]` | `document`, `memory`, `fact`, `event` |

Future bumps are reserved for breaking changes to the envelope. Kind
registry additions do NOT bump MPF version — they're forward-compatible
by the "skip unknown kinds" rule.

### Upgrading payloads across versions

Payload upgrades are governed by the payload schema, not by MPF. A
v2.4 MNEMOS importer reading an MPF envelope with `kind: memory`,
`payload_version: "mnemos-3.1"` drops unknown v3.1 fields and writes
an `import.log` per record. A v3.1 MNEMOS importer reading
`payload_version: "mnemos-2.4"` fills missing fields with documented
defaults.

### MNEMOS memory payload upgrade rules (v2.4 → v3.x)

| Missing field | v3.x importer default |
|---|---|
| `owner_id` | `"default"` |
| `group_id` | `NULL` |
| `namespace` | `"default"` |
| `permission_mode` | `600` |
| `source_model` / `source_provider` / `source_session` / `source_agent` | `NULL` |
| `federation_source` | `NULL` |
| `verbatim_content` | `NULL` |

### Memory version DAG backfill (v2.4 → v3.x)

When migrating a v2.4 memory_versions record into v3.x, the importer
computes a `commit_hash` using the same semantics as
`db/migrations_v3_dag.sql`:

```
sha256( convert_to(memory_id || '|' || version_num || '|' || content || '|' || snapshot_at, 'UTF8') )
```

`branch` defaults to `"main"`, `parent_version_id` to `NULL`. This is
the same content-addressing the MNEMOS v3_dag backfill uses, so a
v2.4 → v3.x migration via MPF is indistinguishable from an in-place
schema migration.

---

## Interop

### MNEMOS (producer + consumer, reference implementation)

Canonical producer. `POST /v1/export` emits MPF v0.1 with MNEMOS
memories as `kind: memory` records. `POST /v1/import` accepts MPF v0.1
and transforms payloads at the declared `payload_version` into the
target schema. For the PYTHIA v2.3 → CERBERUS v3.1 migration, the
importer applies the v2.4 → v3.1 upgrade rules above.

### docling

Docling produces MPF by wrapping `DoclingDocument.model_dump()` in a
`kind: document` record. No change to DoclingDocument itself. The
proposed upstream contribution is a `docling export --format mpf`
subcommand:

```python
def to_mpf(doc: DoclingDocument, record_id: str | None = None) -> dict:
    return {
        "id": record_id or f"doc_{ulid()}",
        "kind": "document",
        "payload_version": doc.version,        # e.g. "1.10.0"
        "payload": doc.model_dump(mode="json"),
    }
```

A MPF envelope built from a corpus of docling outputs is:

```python
{
  "mpf_version": "0.1.0",
  "source_system": "docling",
  "source_version": docling.__version__,
  "exported_at": datetime.now(timezone.utc).isoformat(),
  "records": [to_mpf(d) for d in documents],
}
```

This is the full contribution — everything else (embeddings, KG,
compression) is optional and filled in by the downstream RAG memory
system when it ingests.

### MNEMOS as a docling consumer

MNEMOS v3.1.x accepts MPF on `POST /v1/import`. When a record has
`kind: document`, MNEMOS either:
- Stores the DoclingDocument payload intact as a new memory with
  `category="document"` and `metadata.docling_document` preserving the
  full payload (lossless), OR
- Decomposes the document into per-section memories via the existing
  `tools/docling_import.py` adapter and adds a `relations[]` entry
  linking each section memory to the source document record.

The operator picks the mode per import. Both preserve round-trip
fidelity because the source DoclingDocument payload is retained either
inline (mode 1) or in the relations graph pointing back at the
`kind: document` record (mode 2, requires the envelope to still include
the document record).

### MemPalace

MemPalace's native export is a "palace" tree of drawers and cards.
Mapping to MPF:

| MemPalace concept | MPF |
|---|---|
| Palace (root) | `source_instance` |
| Drawer | `records[].payload.category` (one drawer per category) on `kind: memory` |
| Card | `records[]` of `kind: memory` |
| Spatial layout (coords) | `records[].payload.metadata.mempalace` (opaque round-trip) |
| AAAK compressed card | `compression_manifest[]` with `record_id` pointing at the card |
| Card tags | `records[].payload.metadata.tags` (array) |
| Temporal validity | `kg_triples[]` with `valid_from`/`valid_until` if user annotated it |

Roundtrip claim: MemPalace → MPF → MemPalace preserves all content,
category/drawer mapping, and card-level metadata. Spatial layout rides
in an opaque `metadata.mempalace` blob; a MemPalace importer can
reconstruct the palace if the blob is present. A MNEMOS consumer just
ignores the blob.

### Mem0 / Letta / Zep (minimum-viable producer)

Mem0 and Letta primarily store extracted facts + session events; Zep
stores temporal episodes. Each has a natural `kind`:

- Mem0 extracted facts → `kind: fact`, `payload_version: "mpf-0.1"`,
  payload carries `{statement, subject, predicate, object, confidence}`.
- Letta agent memories → `kind: memory`, `payload_version: "letta-<v>"`
  with Letta's native schema as the payload. Forward-compatible:
  consumers that don't know Letta's payload_version skip it.
- Letta/Zep session turns → `kind: event`, `payload_version: "mpf-0.1"`,
  payload carries `{event_type, session_id, actor, content, occurred_at}`.

These systems emit only the envelope required fields per record. MNEMOS's
importer maps `kind: fact` records into `kg_triples[]` + a synthesized
`memory` record carrying the `statement`; and maps `kind: event` records
into `memories` with `category="session_activity"`. First-class integration
is a ~30-line adapter in the source system's export code.

### LangChain / LlamaIndex in-process summary buffers

Not a producer of MPF directly — these are session-scoped. Wrap the
session end with an MPF emitter that serializes the buffer as `kind:
event` records (one per turn) plus a single `kind: memory` record for
the final summary. Category defaults to `"session_activity"`, metadata
captures the session ID and agent name.

---

## File layout

### Single-file JSON (canonical, small/medium exports)

One file, `export.json`, contains everything above. Target size up to
~100 MB. Fits in memory on commodity hosts.

### JSONL variant (streaming, large corpora)

For exports over ~100 MB, use a directory:

```
export/
├── manifest.json                   # envelope without records / sidecar arrays
├── records.jsonl                   # one record per line, mixed kinds
├── kg_triples.jsonl                # one triple per line
├── relations.jsonl                 # one relation per line
├── memory_versions.jsonl           # DAG history, one version per line
├── compression_manifest.jsonl      # one manifest row per line
├── compression_candidates.jsonl    # full audit, one row per line
└── embeddings.parquet              # optional sidecar, columnar
```

`manifest.json` declares `mpf_version`, `source_system`, `exported_at`,
and counts. Readers stream each JSONL file line by line. No record
straddles a line boundary.

### Embedding sidecar

Embeddings are optional. When present, two encodings:

- `inline_base64_f32`: inline in each record's `payload.embedding` as
  `base64(float32 bytes)`. OK for small exports. Doubles file size.
- `sidecar`: parquet file with `record_id`, `vector`, and `model`
  columns. Referenced from `embeddings.sidecar_file`. Much more
  efficient for anything over a few thousand vectors.

The importer MAY regenerate embeddings with its own model if the
export's `embeddings.model` differs from its configured model. This is
the default for cross-system imports (MNEMOS uses `nomic-embed-text`;
Mem0 uses OpenAI `text-embedding-3-small`; these aren't interchangeable).

---

## Validation

A JSON Schema lives at `docs/mpf_v0.1.json` (tracked in the MNEMOS
repo, pinned per envelope version). Producers SHOULD validate before
emission. Consumers MUST validate the envelope before processing.
Payload-level validation is per-payload_version and delegated to the
owning system's schema.

Roundtrip invariants that MUST hold:

1. For any record R at `mpf_version=V`, exported and re-imported at
   `mpf_version=V` with matching `payload_version`: `R.id`, `R.kind`,
   `R.payload_version`, and `R.payload` are byte-identical after
   canonical JSON serialization.
2. For any `kind: document` record: the DoclingDocument payload is
   byte-identical after round-trip through a MPF consumer that
   preserves documents.
3. For any sidecar row (`kg_triples[]`, `relations[]`,
   `compression_manifest[]`, `memory_versions[]`) referencing a
   record_id: after round-trip, the reference still resolves and the
   sidecar row's payload fields are byte-identical.
4. For any export at `payload_version=P` imported at `payload_version=P`:
   no fields are dropped. Importers that can't preserve a field MUST
   log it and emit a non-zero exit code.

---

## Security and privacy

- **Secrets must be redacted BEFORE export.** The format is plaintext.
  Consumers are untrusted until proven otherwise.
- **Federation source tags are preserved.** Memories pulled from
  federation peers keep their `federation_source` attribution inside
  the memory payload, and importers MUST preserve it — this is audit
  provenance, not a user-editable field.
- **Signed envelopes (v0.2+ optional).** The top-level `attestations[]`
  array is reserved for detached Ed25519 signatures over the canonical
  JSON form (hash-then-sign). Not required in v0.1; reserved in the
  schema to avoid future breaking changes.
- **Embeddings may reveal content.** If embeddings are included,
  downstream consumers can partially reconstruct content via inversion
  attacks. Strip embeddings from exports shared outside trusted scope.
- **ID collisions.** Producers MUST ensure `records[].id` is unique
  within the envelope. Consumers MAY reject envelopes with duplicate
  IDs. Recommended generator: ULID (time-ordered, collision-safe) or
  UUID v4.

---

## Reference implementation

MNEMOS v3.1.x adds:
- `POST /v1/export` — returns a download of the full corpus as MPF
  (single-file JSON for corpora under ~100 MB, directory JSONL for
  larger)
- `POST /v1/import` — accepts MPF upload; validates envelope; stages
  into a transaction; commits on success or rolls back with a
  per-record error list
- `tools/mpf_dump.py` — CLI that hits `/v1/export` and writes either
  shape
- `tools/mpf_load.py` — CLI that reads from either shape and POSTs to
  `/v1/import`
- `tools/mpf_validate.py` — standalone JSON Schema validator

MNEMOS v2.4.x backports `/v1/export` and `/v1/import` with the v2.4
memory payload schema (fewer optional fields, no compression sidecar).
Both implementations share the envelope parser and schema validator
via the `mnemos.mpf` module.

---

## Contributing to docling

The pitch to the docling project:

> Add `docling export --format mpf` subcommand emitting the Memory
> Portability Format. Enables docling users to hand parsed documents
> to any RAG memory system that consumes MPF (MNEMOS, MemPalace, Mem0,
> Letta, Zep) without a per-target adapter. DoclingDocument is wrapped
> unchanged inside a one-object MPF record (`{kind: "document",
> payload_version: doc.version, payload: doc.model_dump()}`). No
> changes to docling-core required. Spec is at
> github.com/mnemos-os/mnemos/blob/master/docs/MEMORY_EXPORT_FORMAT.md.

This sits naturally alongside docling's existing RAG integrations
(`docling-langchain`, `docling-haystack`, `docling-mcp`): MPF is the
serialized corpus-level output; those adapters remain the in-process
consumers. Reference implementation (~200 lines) is offered alongside
the PR.

---

## Changelog

- **2026-04-23** — Initial MPF v0.1 spec. Envelope uses `records[]`
  with `kind` discriminator after GRAEAE architectural consultation
  (4-muse consensus on Option 3 over parallel-arrays Option 1).
  DoclingDocument is extended, not replaced — per-document schema
  stays docling's, corpus envelope is MPF's.

---

*Feedback and revision requests welcome on the MNEMOS repo issue
tracker. Schema revisions require an MPF version bump and a payload
migration table entry.*

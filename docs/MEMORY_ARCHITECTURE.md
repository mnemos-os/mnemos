# MNEMOS Memory Architecture

This document describes the memory model that underpins MNEMOS — the
architectural choices, why they were made, and how the pieces fit
together. It is the reference companion to `SPECIFICATION.md` (the
external surface contract) and `docs/history/V4_PLAN.md` (the release roadmap).
Where those docs answer "what does it do" and "what's coming," this
one answers "why does it look this way."

The audience is downstream implementers, integrators, and
maintainers. We assume familiarity with the Python ecosystem,
relational databases, and the broad shape of agentic-LLM tooling
(MCP, RAG, embeddings, etc.). We do not assume prior MNEMOS
exposure.

---

## 1. Five-axis design surface

Every choice in MNEMOS sits on five axes:

1. **Identity** — what is a memory? How do we name one?
2. **Provenance** — where did it come from, who created it, who
   owns it?
3. **Versioning** — how does it change over time? What's the audit
   trail?
4. **Compression** — when content gets large, how do we keep
   working with it cheaply?
5. **Federation** — how do peer instances share without trusting
   each other for correctness?

Each section below traces one axis end-to-end. Axes interact —
compression decisions cascade through versioning, federation
filters on permission and provenance — but separating them makes
the contract legible.

---

## 2. Identity

### 2.1 The memory id

A memory id is a stable string of the form `mem_<random>`. The
random suffix is a millisecond-resolution timestamp + a 6-byte
random hex tail. There is no global ordering implied; ids that
sort earlier do NOT necessarily come from earlier writes (clock
skew across nodes is real). Sort by `created` if you need temporal
order.

The id is the canonical handle. Every other object — versions,
compression candidates, KG triples, audit chain entries —
references the memory by id. We never embed the id in the content
field; the content is opaque text payload, the id is metadata.

### 2.2 Federation-prefixed ids

When a memory arrives via federation pull, its local id is
`<peer_name>:<remote_id>`. The local-vs-federated distinction is
load-bearing — a search filter that excludes federation rows
(`federation_source IS NULL`) keeps loops out of the per-instance
"native" view. The full id is exposed to operators in API
responses; downstream code should treat it as opaque.

### 2.3 Why not UUIDs

UUID-as-handle is the obvious alternative. We avoid it for
practical reasons: the `mem_` prefix makes log-grep useful (every
mention of a memory in any log file is one greppable string), and
the timestamp prefix gives operators a "when was this written"
hint without joining the row. UUIDs have neither property.

---

## 3. Provenance + ownership

### 3.1 Two-axis tenancy

Every memory carries `(owner_id, namespace)`. These are independent
axes: ownership is the user/agent that wrote the memory; namespace
is the operational scope it lives in (`default`, `production`,
`research`, ...). A memory can be owned by Alice but live in the
`research` namespace where Bob can read it; the search visibility
matrix is filtered on both axes plus permission_mode.

`group_id` exists as a tertiary axis but is reserved for v5.0+
group-based ACLs; v4.x uses `(owner_id, namespace)` exclusively
for visibility decisions.

### 3.2 Permission modes

We borrow the Unix-mode shape: a 3-digit octal where each digit
represents a class (owner, group, others) and each digit value
indicates read/write/execute bits. Mnemos uses:

| Mode | Owner | Group | Others |
|------|-------|-------|--------|
| 600  | rw-   | ---   | ---    |
| 644  | rw-   | r--   | r--    |
| 444  | r--   | r--   | r--    |

Federation visibility is gated on the "others-readable" bit
(ones digit ≥ 4). A memory at 600 is invisible to peers; at 644
it shows up in `/v1/federation/feed`. There is NO partial-trust
mode where a peer sees only some memories from one owner; the
unit of federation visibility is the per-memory permission_mode.

### 3.3 Source attribution

`source_*` columns tag the agent / model / provider / session
that created the memory. These are advisory — we don't validate
that "claude-opus" actually wrote the row — but they give
downstream queries a useful filter (`source_agent="mnemos-desktop"`
to see laptop-originated memories vs server-resident ones).

The ETLANTIS Universe convention is to set `source_agent` to a
short stable string per-tool (`zeroclaw`, `openclaw`, `hermes`,
`mnemos-desktop`, etc.). Tool names are shorter than full URLs
and stable across version bumps.

---

## 4. DAG versioning

### 4.1 Why a DAG, not a linear log

The naive shape — a `memory_versions` table that records every
edit chronologically — is what most memory systems start with.
MNEMOS is structured as a DAG instead because of the
**dream-state divergence** layer (MORPHEUS, ARTEMIS): a single
memory can branch into multiple speculative versions, each
synthesizing a different angle on the same content. Linear-log
shapes can't represent "branch B was experimented with for two
weeks then merged back, branch C was abandoned" without ad-hoc
sentinel rows.

The DAG carries:

* `id` — version handle (UUID, separate from the memory id).
* `memory_id` — which memory this version belongs to.
* `parent_version_id` — the version this one was derived from
  (NULL for roots).
* `commit_hash` — content-addressed hash of the version's payload.
* `dream_status` — `active`, `archived`, `tombstoned`.

Tombstones are content-zero versions that record "this was
deleted" without losing the parent chain. Federation honors
tombstones — a peer pulling a tombstoned version sees the delete
and applies its own.

### 4.2 Audit chain

Separately from the DAG, there's a `graeae_audit_log` hash chain
that runs across ALL writes (not just memory edits — also GRAEAE
consultations, schema migrations, config changes). Each row
contains:

* `chain_hash` — sha256 of the prior chain_hash + this row's data.
* `prompt_hash` / `response_hash` — the inputs and outputs of the
  triggering operation.
* `prev_id` — pointer to the prior chain entry.

The genesis hash is the literal string
`MNEMOS_AUDIT_GENESIS_v3` (preserved across v4.x for chain
continuity). Verifying the chain is a server-side endpoint
(`/v1/consultations/audit/verify`) that walks every entry from
genesis forward; tamper-evidence is by construction (any single
edit invalidates every subsequent hash).

The audit chain is NOT the version DAG. Versions track WHAT changed
in a memory; the audit chain tracks WHEN something happened across
the whole system. They reference each other by id but evolve
independently.

### 4.3 Branching for dream-state

When ARTEMIS / APOLLO produces a "dream" (a speculative
synthesis of a memory under a new framing), the result lands as
a new version with `parent_version_id` pointing at the source.
Multiple branches on the same source are explicit DAG nodes;
operators can list, compare, and prune them via
`/v1/memories/{id}/branches`.

A branch ends in one of three states:

1. **Promoted** — judged better than the source; the memory's
   current_version_id moves to the branch head.
2. **Archived** — no longer active but kept for audit
   (`dream_status='archived'`).
3. **Tombstoned** — explicitly deleted; the version row remains
   for chain continuity but its content is zeroed.

---

## 5. Compression

### 5.1 The variant table

`memory_compressed_variants` is a sibling of `memories`: every
memory may have zero or more compressed variants, each produced
by a specific engine + judge run. The schema captures:

* `memory_id` — back-pointer.
* `engine_id` — which compression engine produced this
  (`apollo-narrate`, `artemis-textrank`, plugin engines).
* `engine_version` — engine version that emitted this (so we can
  tell e.g. apollo-narrate-1.0 output from -1.1).
* `compressed_content` — the compressed payload.
* `compression_ratio` — original_tokens / compressed_tokens.
* `quality_score` — judge-rated semantic preservation 0-100.
* `judge_model` — which model rated it (`local-heuristic`,
  `cross-encoder`, `llm:gpt-4`, etc.).
* `selected_at` — when (if ever) this variant was promoted as
  the canonical compressed form.

The split between `memories.content` (raw, source-of-truth) and
`memory_compressed_variants` (derived, multiple) is deliberate:
the raw content is never destroyed by compression; it remains the
fallback if a downstream consumer needs the full text. Compression
is a read-side optimization, not a storage transformation.

### 5.2 Engine + judge separation

Compression has two distinct decisions:

1. **Engine** — which algorithm produces the candidate? APOLLO
   uses LLM-driven narration; ARTEMIS uses TextRank (TF-IDF +
   networkx graph centrality). Operators register additional
   engines via the plugin ABC.
2. **Judge** — which scorer rates the candidate? Options:
   * `LLMJudge` — calls a configured LLM to score the
     candidate's semantic preservation.
   * `CrossEncoderJudge` — uses sentence-transformers cross-encoder
     for direct similarity scoring (requires explicit
     `pip install sentence-transformers` — not in default extras
     because the torch transitive is heavy).
   * Heuristic-only — token-overlap, entity-preservation, and
     length-ratio scoring without ML. Default fallback.

The compression-engine choice runs through the contest mechanism
(every registered engine produces a candidate; the best wins per
the quality-judge score), not via a single-engine env var. The
judge mode is configurable via `MNEMOS_JUDGE_MODE` (`llm` |
`cross` | `ensemble` | `heuristic` — see `mnemos/core/config.py`
`_CompressionSettings.judge_mode`). Ensemble mode uses LLM as the
primary and CrossEncoder as a secondary; the result is the
higher-confidence score.

### 5.3 Why fastembed (no torch)

The semantic-similarity scorer in `QualityAnalyzer` uses
`fastembed` (ONNX runtime). This is a deliberate departure from
the obvious `sentence-transformers` pick: fastembed ships ~10–20
MB; sentence-transformers transitively pulls torch + nvidia
binary weight totalling ~700 MB–1 GB. The same MiniLM/Nomic
embedding models are available in both ecosystems.

The fleet shape made this decision easy: of the hosts that
actually run MNEMOS,

* zero have NVIDIA discrete GPUs as their MNEMOS-running role
  (TYPHON has an RTX 5060 but doesn't run the engine);
* most have Intel iGPU (PYTHIA, PROTEUS, ARGOS) — OpenVINO is the
  match;
* Apple Silicon dev hosts use MPS, not CUDA;
* edge hosts (Pis, Jetson) have other ML stacks (CPU, TensorRT).

A torch CUDA wheel chain is dead weight on every one of those.
fastembed's ONNX-runtime supports CPU + CUDA EP + CoreML EP +
OpenVINO EP via pluggable providers, generalizing across the
fleet without bloating the install.

### 5.4 Hot-path expansion

The compression read paths preferring compressed variants over raw
content is incremental. As of v5.0.1:

* `/v1/memories/search` honors `include_compressed` to swap
  variants into the response.
* `/v1/rehydrate` and the OpenAI-compat gateway prefer compressed
  variants automatically (operator-configurable).
* Federation feed and MCP tool responses are NOT yet compressed-
  variant aware (v4.2.0a12+ candidate). When they land, peer
  bandwidth and MCP token budgets benefit immediately.

### 5.5 GPU acceleration via the [gpu] extra

For hosts with NVIDIA CUDA discrete GPUs that DO run MNEMOS
(future deployments at scale; not the current fleet), the
`mnemos-os[gpu]` extra installs `fastembed-gpu` which uses the
ONNX CUDA execution provider. This gives 5-15× embedding speedup
on a real workload without dragging in the torch+nvidia wheel.

The selection is operator-driven, not auto-detected at runtime —
`mnemos doctor` (or `python -m mnemos.runtime.hardware`) prints
the suggested extra for the host, and the operator picks it
explicitly. Auto-detection at install time was considered and
rejected: `pip install` is the wrong place to make hardware
decisions silently; the extra-name is the operator's promise.

---

## 6. Federation

### 6.1 HTTP pull as the durable path

Federation between MNEMOS instances is HTTP-based pull, not push.
Each peer exposes `/v1/federation/feed` (paginated, cursor-based)
and `/v1/federation/memory/{id}` (by-id backfill). A receiver
walks the feed periodically, applies new rows via
`_store_memories`, and persists a per-peer `last_pulled_cursor`.

We picked pull over push because:

1. **Receiver controls rate.** If a peer is slow, the puller can
   throttle. Push would require backpressure semantics in the
   event bus, which we don't have at v4.x.
2. **Simpler trust boundary.** A pulling receiver authenticates
   to the publishing peer; the publisher doesn't have to maintain
   a list of authorized receivers.
3. **Replay friendly.** A receiver can re-pull from any cursor
   to recover from a missed window — no separate replay
   protocol.

### 6.2 NATS push as the additive fast path

The current v5.0.1 build adds a NATS JetStream push consumer that delivers
memories with sub-second latency to subscribed peers. Critically,
the HTTP pull path stays the **durable** fallback — NATS is purely
a fast-path optimization. If NATS is down or a message is missed,
HTTP pull catches up at the next interval.

NATS JetStream gives us:

* `at-least-once` delivery via durable consumer + ack.
* 2-minute `duplicate_window` so re-publishes within that window
  are no-ops.
* 30-day / 10 GB retention so a peer that comes back after a
  brief outage replays from the broker (no full HTTP re-pull).
* Queue-group sharding (`MNEMOS_FEDERATION_NATS_QUEUE_GROUP`) so
  multi-replica receivers can run side by side.

The subjects are versioned: `mnemos.memory.created.{namespace}`,
`mnemos.memory.updated.{namespace}`, `mnemos.memory.deleted.{namespace}`.
Receivers can subject-filter to only the namespaces they care
about, reducing broker traffic for cross-namespace deployments.

### 6.3 Loop prevention

A naive federation can loop: A pulls from B, B pulls from A,
repeat. MNEMOS prevents loops at the source-tag layer:

* Every memory carries `federation_source` (NULL for native
  rows, peer name for federated rows).
* The feed query filters `federation_source IS NULL` — peers
  only see native rows, never re-export.
* The NATS publisher stamps `source_node = MNEMOS_NODE_NAME` on
  every event; consumers filter their own node name out before
  applying.

These two filters are independent — HTTP-feed loop prevention
works without `MNEMOS_NODE_NAME` set; NATS loop prevention works
without the federation-source filter. Either alone would be
sufficient; we have both for defense in depth.

### 6.4 Concurrent-delivery idempotency

When NATS push and HTTP pull deliver the same memory concurrently
(or two NATS consumers in a partial-fleet rollout), the
`_store_memories` upsert path is race-safe:

* INSERT race: catches `asyncpg.UniqueViolationError`, falls
  through to update-when-newer.
* UPDATE race: WHERE clause includes
  `federation_remote_updated < $9` so a stale UPDATE matches
  zero rows when a newer event has already committed.

This is content-idempotent — applying the same memory N times
produces the same final state, and concurrent applications can't
roll local state backward to an older version.

---

## 7. Persistence backends

### 7.1 Two backends, one repository surface

MNEMOS supports two persistence backends:

* **Postgres** (`postgres` profile) — the production target.
  pgvector for embeddings, asyncpg for I/O, full transactional
  semantics.
* **SQLite** (`edge` profile) — single-file deployment for
  laptops, edge appliances, single-binary builds. sqlite-vec
  for embeddings (or a Python UDF fallback when the native
  extension isn't loaded).

Both backends implement the same `PersistenceBackend` ABC
(`mnemos/persistence/base.py`). API handlers and domain code
target the abstract repository surface; the concrete backend is
swapped at startup based on the `MNEMOS_PROFILE` env var.

### 7.2 Persistence-parity discipline

The two backends ship together with strict parity tests:
`tests/test_persistence_parity.py` runs the same CRUD + search +
versioning operations against both backends and asserts identical
output. This has caught:

* asyncpg returning `Decimal` for NUMERIC columns where SQLite
  returns `float`. Fixed via `mnemos/core/numeric.py:safe_float`.
* asyncpg returning UUID objects where SQLite returns strings.
  Fixed via explicit `::text` casts at the repo seam.
* Trigger-induced version snapshots firing on PG but not SQLite
  during seed inserts. Fixed via `SET LOCAL
  mnemos.suppress_version_snapshot` in seed transactions.

The parity discipline is the load-bearing reason both backends
can ship with confidence; without it the SQLite path would
silently diverge over time.

### 7.3 Why both

Postgres is the operational blueprint. SQLite exists for
deployments where:

* **No DBA.** A laptop install or single-host edge appliance
  shouldn't need a Postgres instance.
* **Air-gapped.** SQLite ships as a single .db file; backup is
  `cp`. Postgres backup needs `pg_dump` orchestration.
* **Fast iteration.** Tests against an in-process SQLite are
  10× faster than against a Postgres docker container; the
  `tests/conftest.py` fast-test fixture uses SQLite for unit
  tests, real Postgres for parity tests.

The trade-off: SQLite serialization-level concurrency is worse
than Postgres MVCC, and pgvector's HNSW index outperforms
sqlite-vec's LSH at scale. For 10k-memory edge deployments,
SQLite is fine; for 10M-memory production, Postgres is the only
viable choice.

---

## 8. Observability

### 8.1 Four instrument tiers

MNEMOS exposes four observability surfaces:

1. **Structured logs** (always on) — single-line key=value
   format by default; JSON via `MNEMOS_STRUCTURED_LOGS=true`
   (requires the `[structlog]` extra).
2. **Prometheus metrics** (always on) — `/metrics` endpoint with
   the standard counter/histogram set.
3. **OpenTelemetry tracing** (opt-in) — `[tracing]` extra +
   `MNEMOS_TRACING_ENABLED=true`. OTLP/HTTP exporter wires into
   any compliant collector (Tempo, Honeycomb, Jaeger).
4. **Audit chain** — the SHA-256 hash chain in
   `graeae_audit_log` (see §4.2). Cryptographic, not just
   observability — tamper-evident.

### 8.2 LIFO middleware ordering

A common trap with FastAPI middleware is that the registration
order is LIFO at request time: the FIRST middleware registered
is the OUTERMOST wrapper at runtime. MNEMOS's middleware stack
puts the auth check INSIDE the trace span (so unauthenticated
requests don't pollute traces) but OUTSIDE the metric counter
(so we count auth failures as "auth_failures"). The order is:

```
register: tracing → auth → metrics → routes
runtime:  tracing wraps auth wraps metrics wraps routes
```

This is documented in `mnemos/core/lifecycle.py` (process-level
boot/shutdown + cache/pool globals) and
`mnemos/api/lifecycle_hooks.py` (FastAPI startup/shutdown hooks);
the actual `add_middleware(...)` calls live in
`mnemos/api/main.py`. The ordering is invisible from a flat
middleware list, so the spec lives next to the registration code.

---

## 9. Compatibility + portability

### 9.1 CHARON envelope format (MPF)

Every export from MNEMOS uses the **Memory Portability Format**
envelope:

```json
{
  "mpf_version": "0.1.1",
  "source_system": "mnemos",
  "source_version": "4.2.0a11",
  "source_instance": "pythia",
  "exported_at": "2026-05-01T07:01:37Z",
  "record_count": 1234,
  "records": [...]
}
```

Each record carries `kind` (`memory` | `kg_triple` | `audit_entry` | ...) and
a `payload_version` so importers can dispatch on the exact
producer version. The format is intentionally JSON-array-shaped
for streaming-friendly parsing; sidecar files (compression
manifests, KG triples) reference the parent memory by id.

The format is moving toward MIF (Memory Interchange Format)
alignment — see `project_mpf_paused_align_to_mif.md`. v0.1.1 is
the frozen MNEMOS-side shape; v1.0 will land as part of the MIF
contribution.

### 9.2 Round-trip discipline

Export → import should be **lossless** for native rows. Federation
rows survive round-trip but lose the original peer attribution
(the import lands them as native because we can't re-establish
the federation_source pointer without re-pulling from the original
peer). Tests in `test_persistence_parity.py` enforce the lossless
round-trip on the native subset.

---

## 10. What's NOT in MNEMOS (deliberately)

* **General-purpose key-value store.** Use Redis. MNEMOS's keying,
  versioning, and indexing assume textual content; storing config
  blobs as memories is wrong-shape.
* **Vector index optimization at install time.** `pgvector` HNSW
  parameter tuning is the operator's job; MNEMOS picks safe
  defaults but doesn't auto-tune.
* **GUI / web front-end.** `mnemos-web` is a separate repo,
  post-v4.0 track. The CLI + REST + MCP surface is the
  programmatic-only contract.
* **Direct LLM integration as part of memory storage.** GRAEAE
  is the LLM gateway; memory writes don't auto-summarize. If
  you want compression-on-write, register a CompressionEngine
  plugin that runs in the background.
* **Real-time multi-master replication.** Federation is
  eventually-consistent pull; MNEMOS does not promise a
  cross-instance read-your-writes guarantee. For that, run a
  single Postgres with read replicas.
* **Per-memory ACLs beyond owner/namespace/permission_mode.**
  Group ACLs + finer-grained sharing are v5.0+ candidates.

---

## 11. Where to read next

* `SPECIFICATION.md` — external API contract.
* `docs/history/V4_PLAN.md` — release roadmap.
* `COMPRESSION.md` — engine + judge selection rubric.
* `DREAM_STATE_DESIGN.md` — MORPHEUS / ARTEMIS internals.
* `KNOSSOS.md` — KG triple storage + traversal.
* `PANTHEON.md` — LLM gateway + provider routing.
* `NATS_OPERATIONS.md` — operator runbook for the v4.2 NATS
  substrate.
* `STREAMING_REPLICATION.md` — federation pull/push semantics.
* `SQLITE_PROFILE.md` — edge-tier deployment guide.
* `SCALING.md` — production sizing + horizontal scale.

---

*v1.0 — 2026-05-08. Tracks MNEMOS server v5.0.1.*

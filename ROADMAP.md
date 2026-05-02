# MNEMOS Roadmap

Forward-looking scope for MNEMOS releases beyond the current version. Current shipping version in `pyproject.toml`. Release-by-release history in [`CHANGELOG.md`](./CHANGELOG.md).

This document is kept intentionally narrow. It lists what the next release will contain, what has been consciously deferred, and why. It does not list wishlist items, speculative features, or aspirational claims.

---

## Current status — v5.0.0 shipped on 2026-05-02

v5.0 closes the v3.6 + v4.x charters and rolls up the v4.2.0a14
alpha line. Major new surfaces in this release:

- ✅ GDPR right-to-be-forgotten: deletion-request lifecycle +
  soft-delete worker + hard-delete worker.
- ✅ MORPHEUS slices 3 + 4: CONSOLIDATE + EXTRACT phases close
  the divergent dream-state pipeline.
- ✅ PERSEPHONE archival subsystem with cold-set rotation +
  zstd-compressed archive.
- ✅ PANTHEON v0.1 + v0.2: unified LLM facade with adaptive
  routing + agentic-tier caps + MNEMOS routing-log feedback.
- ✅ KRONOS v0.1: recall-pattern anomaly detection + forecasting.
- ✅ DAG wiring for compression derivations.
- ✅ NATS substrate v0.2: PANTHEON routing pub/sub bounded slice.
- ✅ MCP §6.4 cross-tenant security gates across 22 tools,
  including `pantheon_list_models`, `pantheon_route_explain`,
  `tool_kronos_anomalies`, and `tool_kronos_forecast`.
- ✅ Subsystem modularization: optional pip extras + named feature
  bundles (`[edge]`, `[server]`, `[ml]`, `[interop]`, `[full]`) so
  operators install only what they deploy. Routes for missing extras
  return 503 with install hint; MCP tools are filtered from
  `tools/list`; workers gracefully no-op.
- ✅ Document-import retry-safety with content-derived idempotency.
- ✅ Connector smoke tests across 8 surfaces.
- ✅ RFC-002 re-engagement memo + design paper draft.

PROTEUS barrage (2026-05-02) validated at 2500 concurrent writes,
2000 reads, 200 searches: 98.5% / 99.7% / 100% success.

## Historical: v4.0.0 shipped on 2026-04-29

v4.0.0 is shipped. The v4.0 work that used to be forward-looking is now current
state:

- ✅ Coherent `mnemos/` package layout: `api/routes`, `core`, `db`, `domain`,
  `persistence`, `mcp`, `webhooks`, `workers`, `hooks`, `installer`, `tools`,
  `cli`.
- ✅ `PersistenceBackend` abstraction with Postgres and SQLite implementations.
- ✅ Deployment profiles: `server`, `edge`, and `dev`.
- ✅ SQLite profile: aiosqlite + sqlite-vec + FTS5 + JSON1 + WAL.
- ✅ Single-binary distribution for linux-x86_64, linux-aarch64, and
  macos-aarch64.
- ✅ Unified `mnemos` CLI for serve / install / worker / export / import /
  consult / health / version.
- ✅ Redis-backed multi-worker support for circuit breaker, rate limiter, and
  concurrency limiter state.
- ✅ Seven import-linter contracts and Pydantic Settings singleton discipline.
- ✅ GRAEAE modes: `auto`, `local`, `external`, `all`, `single`, `debate`,
  `majority`; unknown values now 422.
- ✅ KRONOS v0.1 scaffold: CPU-only recall-pattern anomaly detection and
  recall-load forecasting in `mnemos/domain/kronos`; Tesseract/CUDA integration
  is deferred to KRONOS v0.2.

Next planning focus:

- **v4.1**: web UX in the separate `mnemos-web` frontend repo (Tier-1 MVP),
  mobile hardening for Android Termux first, iOS native client later, connector
  gallery expansion, and any targeted Rust rewrites deferred on 2026-04-29.
- **v5.0+**: hosted MNEMOS Cloud, foundation-tier OSS standardization
  (MCP-MD via LF AI & Data), and larger Rust/mobile investments once the v4
  Python architecture has settled under production use.

## v3.1 — compression platform + v3.0 unblocks

**Headline:** plugin-interfaced compression platform with competitive per-memory engine selection, a persisted audit log on every compression decision, and a first-class GPU batcher that works across integrated graphics, discrete GPUs, and remote OpenAI-compatible endpoints.

Three engines shipped under the platform in v3.1: LETHE (extractive, CPU), ALETHEIA (LLM-assisted token importance, GPU-required), and ANAMNESIS (LLM fact extraction, GPU-optional). That is historical release context, not the active v3.5.x runtime. ALETHEIA was retired from the default contest in the v3.2 tail after the 2026-04-23 benchmark, ARTEMIS and APOLLO became the active built-ins, and v3.5 removed the LETHE / ANAMNESIS / ALETHEIA modules plus the old manager/compatibility shims. The `CompressionEngine` ABC remains open for operator-registered engines.

### Tier 1 — small fixes that unblock real surfaces (shipped on master)

1. **MCP stdio server path prefix.** The published stdio MCP server in `mcp_server.py` called `/memories*` but the REST router registers `/v1/memories*`. Nine of fourteen memory-related MCP tools returned 404 against a default install. Fixed with the prefix + an end-to-end wire regression test (`tests/test_mcp_stdio_wire.py`).
2. **Installer `api_keys` schema.** Fresh installs with auth enabled failed at seed because `installer/db.py` wrote columns that no longer existed on the schema. Aligned the insert with the current `db/migrations_v1_multiuser.sql` table definition.
3. **Federation-role admin provisioning.** `api/handlers/admin.py` rejected `role="federation"` at validator time; `api/handlers/federation.py` required that role. Extended the admin validator so peer onboarding no longer requires direct DB writes.

### Tier 2 — the compression platform (v3.1.0 GA)

4. **Three-engine roster under a plugin ABC.** LETHE (extractive token/sentence filtering — honest about being rule-based, not ML), ALETHEIA (LLM-assisted semantic rewriting with swappable small-LLM judge — `gemma4:e2b` default, `gemma4:e4b` for quality-critical paths), and ANAMNESIS (LLM fact extraction — atomic facts, entities, concepts, summary). The `CompressionEngine` ABC is adapted from the plugin-interface pattern in OpenClaw's `CompactionProvider` (credited prior art). Operators can register additional engines at startup; the ABC is public and documented.
5. **Competitive selection.** The distillation worker runs every eligible engine per memory, scores each candidate via a composite function (quality × compression ratio × speed factor, with a quality floor that disqualifies damaged candidates), and keeps the winner. The manifest records both the winner and every losing candidate with its score and disqualification reason — a full audit trail of every compression decision. Scoring profile is operator-configurable (`balanced`, `quality_first`, `speed_first`, `custom`) via `~/.mnemos/compression_scoring.toml`.
6. **GPU endpoint circuit breaker + CPU-fallback coordination.** `compression/gpu_guard.py` tracks the health of each configured `GPU_PROVIDER_HOST` via a per-endpoint circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED). GPU-backed engines consult the guard before every HTTP call; when the circuit is open, they fast-fail with `reject_reason='disabled'` instead of piling doomed requests onto a dead endpoint. Each engine declares `gpu_intent` (`cpu_only` | `gpu_optional` | `gpu_required`); `gpu_required` engines (ALETHEIA, ANAMNESIS) skip when the circuit is open, `gpu_optional` engines (none in v3.1) would degrade to a CPU path if they had one, `cpu_only` engines (LETHE) never consult the guard. Endpoint is backend-agnostic — Ollama on an Intel iGPU, vLLM on an A10, a remote provider. **Actual request batching** (accumulating concurrent calls into one HTTP roundtrip) is a v3.2 optimization; modern inference servers (vLLM, Ollama) already batch internally at the model layer, so the v3.1 work is the correctness surface (fast-fail + routing) rather than the throughput surface.
7. **Manifest read endpoint.** `GET /v1/memories/{id}/compression-manifests` returns the winner + candidates + scoring trace for every compression decision, as JSON. Read-only view over `memory_compressed_variants` and `memory_compression_candidates`.
8. **Migration.** `db/migrations_v3_1_compression.sql` adds `memory_compressed_variants` (winner), `memory_compression_candidates` (full contest log), and `memory_compression_queue` (write-time task queue). Migration is idempotent and has been dry-run-validated against a real pgvector/pg16 container. **In v3.1 these tables are populated by the distillation worker; read paths continue to serve `memories.content` unchanged.** Hot-path invocation (rehydrate / gateway inject / session context reading the winner variant) is a substantial separate surface with its own audit, benchmarks, and migration story — scheduled for v3.2 alongside APOLLO.

### Shipping criteria for v3.1.0

- Every Tier 1 item already on master (verified).
- Every Tier 2 item lands with unit tests plus at least one live integration test against real infrastructure (no mocks-only coverage on the success path for any GPU-touching engine).
- End-to-end contract tests for MCP stdio wire compatibility (already shipped) and the new compression contest path.
- `docs/benchmarks/compression-2026-04-23.md` with measured numbers across a real stratified memory sample from the production install — not single-input anecdata. **Shipped.** 49 memories from PYTHIA, three engines against gemma-4-E4B-it on CERBERUS. LETHE won 30, ANAMNESIS won 18, ALETHEIA won 0 (disabled by default on the finding that its index-list prompt doesn't survive instruction-tuned models). See `docs/benchmarks/compression-2026-04-23.md` for full findings including one real bug surfaced and fixed.
- `CHANGELOG.md` entry listing every item above, with SHA references.
- `DEPLOYMENT.md` updated with the single-worker constraint and the scaling roadmap pointer.

### Consciously out of v3.1 scope (moved to later releases)

These were in earlier v3.1 plans and have been explicitly deferred to keep v3.1 tight and deliverable:

- **APOLLO engine + schema-aware dense encoding.** Moved to v3.2–v3.4 staged rollout (see "Apollo Program" below). The design needs deliberate time — not mining 1966-era NASA telemetry docs, but building on InvestorClaw's consultative-LLM pipeline as the canonical working pattern.
- **Narration endpoint** (`GET /v1/memories/{id}/narrate`). APOLLO's companion read path; deferred to v3.2 with APOLLO itself.
- **Hot-path compression-variant reads.** Making `/v1/memories/rehydrate`, the gateway inject path, and the session context injection path serve the winning compressed variant instead of raw `memories.content` is a substantial change to the read surface. The v3.1 tables hold the winners; v3.2 wires the reads.
- **Tier 3 tenancy fixes** (KG `owner_id`, namespace enforcement on memory paths, application-layer owner filter, registry-backed `/v1/models`). These deserve a dedicated tenancy-focused release. Targeted for **v3.1.1** as a follow-on patch series, with migration guides and per-fix regression coverage.
- **Horizontal scaling.** GRAEAE reliability primitives (circuit breakers, rate limiters, semaphores) are in-process singletons today; moving them to shared state is a dedicated refactor. v3.1 documents the single-worker constraint prominently in `DEPLOYMENT.md`.

---

## Apollo Program — v3.2 to v3.4 staged rollout

APOLLO is the going-forward stack's schema-aware engine: dense encoding targeted at **LLM-to-LLM wire use**, not human reading. The insight is that LETHE and ANAMNESIS both assume the final reader is human or a search-ranking pass. APOLLO assumes the final reader is a downstream LLM (a GRAEAE muse, a consultative agent, a tool-use caller) and encodes accordingly: typed key:value dense forms that LLMs parse natively in fewer tokens than the prose equivalent. Humans read through a narrator at read time; the raw dense form is never shown to them.

The canonical production pattern is InvestorClaw's consultative layer, which already demonstrates that `AAPL:100@150.25/175.50:tech` (12 tokens) is equivalent context for a downstream LLM to the 50-token prose sentence it was derived from.

Rolled out in stages, Saturn V-style — each stage delivers a usable payload on separation, not a deferred promise.

### v3.2 — S-IC (first stage: get off the pad) — **SHIPPED v3.2.0–v3.2.4**

- ✅ `APOLLOEngine` under the `CompressionEngine` ABC; `gpu_intent=gpu_optional`.
- ✅ First schema: portfolio. **v3.3 already added decision / person / event / commit / code schemas**, ahead of the original v3.3 plan.
- ✅ Rule-based detection (regex) with LLM fallback via ANAMNESIS-pattern httpx scaffolding. Fallback gated behind `MNEMOS_APOLLO_LLM_FALLBACK_ENABLED`; turned off in PYTHIA prod after v3.2.4 audit found 4.4% win rate without judge.
- ✅ Narration endpoint (`GET /v1/memories/{id}/narrate`).
- ✅ Judge-LLM scoring integrated; `MNEMOS_JUDGE_ENABLED` toggle.
- ✅ Hot-path reads wired: rehydrate / gateway / session-context paths read winner variant when present.
- ✅ ARTEMIS — CPU-only extractive engine added alongside APOLLO; LETHE retired from default contest.

### v3.3 — S-II (second stage: to upper atmosphere) — **SHIPPED in part (v3.3.0-alpha.1)**

- ✅ Additional schemas already shipped in v3.2 tail (decision, person, event, code, commit) with adversarial regression tests.
- ✅ DAG wiring for derivations: compression winners now land as `memory_versions` child rows on `distilled` / `narrated` branches with content-addressed commits via `db/migrations_v4_2_compression_dag.sql` and commit `v4.2.0a14: DAG wiring for compression derivations — distilled + narrated branches`.
- ✅ Read-path routing on `Accept` headers: `text/plain` → narrated; `application/x-apollo-dense` → raw dense — landed v4.2.0a14 round-12 on `GET /v1/memories/{id}` (negotiator at `mnemos/api/content_negotiation.py`, 19 unit + 9 handler tests). Default JSON path is unchanged so legacy clients are unaffected; `Vary: Accept` set on negotiated responses.
- ✅ **MORPHEUS dream-state subsystem (slice 1: foundation).** v3.3.0-alpha.1 ships `morpheus_runs` table + per-row `morpheus_run_id` tagging + admin/observability API + rollback contract. Synthesis logic stubbed; slice 2 fills it in. Architecture per GRAEAE consensus 2026-04-25: append-only synthesis first, mutation paths (CONSOLIDATE / EXTRACT / ARCHIVE) deferred to v3.6+.
- ✅ **MORPHEUS slice 2** — real cluster + synthesise phases, cron timer at 03:17 UTC, recall-frequency tracking columns (absorbed from OpenClaw dreaming patterns), per-cluster introspection artifact, per-namespace dream scoping. Landed before the v3.3 stable cut.

### v3.4 — S-IVB (third stage: trans-lunar injection) — **SHIPPED 2026-04-26**

**Headline: "CHARON v0.2 — agent memory now travels."** The portability surface that turns MNEMOS into something other systems can interop with.

**Shipped:**
- ✅ **CHARON v0.2 — full MPF v0.1.x sidecar surface.** Server-side import/export for `kind=memory` records plus `kg_triples`, `memory_versions`, `compression_manifest` sidecars. Root + `preserve_owner=true` admin path supports authoritative version-history restoration via the trigger-suppression GUC; non-root callers can ship `kg_triples` and `compression_manifest` without restriction. Peer adapter scaffolding for Mem0 / Letta / Graphiti / Cognee / MemPalace.
- ✅ **42 rounds of CHARON adversarial review.** 59+ exploitable findings closed across cross-tenant attacks, ID-derivation drift, retry-idempotency, COALESCE-tolerance, snapshot consistency under concurrent writes, per-surface DoS bounds.
- ✅ **APOLLO S-II schemas.** decision / person / event / commit / code schemas (originally scheduled for v3.4, landed in v3.3).
- ✅ **APOLLO S-IVB phases 1–2** — the divergent dream-state subsystem. Phase 1 = `morpheus_runs` foundation + audit + rollback; phase 2 = REPLAY → CLUSTER → SYNTHESISE → COMMIT pipeline.

  **Naming convention (locked in v3.4):** `morpheus` is the **internal** identifier — Python module (`morpheus/`), REST routes (`/v1/morpheus/*`), database tables (`morpheus_runs`, `morpheus_clusters`), and Python classes (`MorpheusRun`, etc.). `APOLLO S-IVB` is the **release / marketing / Greek-pantheon identifier** used in roadmaps, charters, announcements, and conceptual references. Both names refer to the same subsystem and will continue to coexist; no rename is planned. The dual-naming aligns with the broader pattern: release-shaped Greek-mythology framing externally, concrete code-friendly names internally.
- ✅ **Recall-frequency tracking columns** (`recall_count`, `last_recalled_at`, `unique_queries`).
- ✅ **Per-cluster introspection artifact** (`morpheus_clusters` table + `/v1/morpheus/runs/{id}/clusters`).
- ✅ **Per-namespace dream scoping** (`morpheus_runs.namespace` filter).
- ✅ **Pre-tag GUC audit (2026-04-26)** caught and fixed pool-leak + injection-shape bug at `api/handlers/versions.py:253` — plain `SET` on user-controllable input → `set_config(..., true)` (transaction-local + parameter binding). Commit `e4b41aa`.
- ✅ **Pre-tag migration idempotency verification (2026-04-26)** — `migrations_charon_trigger_guard.sql` empirically applied twice on a populated DB; triggers fire correctly on INSERT/DELETE post-reapply.
- ✅ Audit-remediation log responses to the 2026-04-25 GPT critical review (8 of 13 findings closed, 1 partial, 4 deferred-by-design or carried to v4.0 — see audit log section below).

**Carried forward (originally bundled with v3.4):**
- Distill-on-ingest as default write path → deferred after v3.5.0; compression remains operator-batched.
- Historical ANAMNESIS deprecation path → superseded by v3.5 slice 5 removal of LETHE / ANAMNESIS / ALETHEIA modules and compatibility shims.
- Full round-trip fidelity benchmark as GA gate → deferred after v3.5.0; MPF restore drills and schema-compat preflight shipped first.
- KNOSSOS phase 2 and MemPalace re-engagement → deferred after v3.5.0; v3.5.x keeps the phase-1 stdio shim.
- First wave of goodwill PRs to MemPalace → deferred after v3.5.0 (subject to the 3-4 PRs/24h-per-upstream rate-limit constraint per `~/.claude/rules/github-behavior.md`).

**See:** `docs/V3_5_CHARTER.md`, `docs/V3_6_CHARTER.md`, `docs/V4_PLAN.md`, `docs/OPERATIONS.md` for locked scopes downstream of this tag.

### v3.5.0 — audit hardening and uniform tenancy — **SHIPPED 2026-04-28**

v3.5.0 shipped the audit-driven hardening branch that followed v3.4.1. It is not the PANTHEON/IRIS feature release originally sketched in `docs/V3_5_CHARTER.md`; that charter is now historical and its unshipped feature ideas move to later roadmap work.

- ✅ **Slice 1: audit quick wins** (`a62a099`). Session history now returns the most recent rows first, pins/caps system rows deterministically, and project metadata points at `mnemos-os/mnemos`.
- ✅ **Slice 2: memory-read tenancy + DAG integrity** (`d42c475`). Shared `read_visibility_predicate` gates memory list/get/search/rehydrate/gateway context; `version_visibility_predicate` gates version/log/commit/diff paths per snapshot; DAG writers use same-memory parent checks, target-head visibility gates, advisory-lock-before-row-lock ordering, race-safe branch creation, and `MN001` to HTTP 409 reconciliation.
- ✅ **Docker existing-volume migration path** (`86f1532`, `19229d7`). `docker-compose.yml` and `docker-compose.staging.yml` run `postgres-upgrade` after Postgres is healthy so `db/migrations_v3_5_trigger_same_memory_parent.sql` applies to existing volumes, not only fresh initdb volumes.
- ✅ **#25 RLS Unix-bit fix.** `db/migrations_v3_5_rls_group_select_unix_bits.sql` replaces `mnemos_group_select` so RLS and `read_visibility_predicate` both use `((permission_mode / 10) % 10) >= 4` for group-readable rows.
- ✅ **#20 webhook retry state machine.** Persisted attempt leases, writer revisions, repair/recovery split, terminal-state repair, one-success-per-chain guards, and terminal success trigger.
- ✅ **#21 federation stable cursor tie-breaker.** `/v1/federation/feed` uses an opaque `(updated, id)` cursor and `ORDER BY updated, id` so page boundaries inside identical timestamps do not skip rows. Per-peer ACL scope remains later work.
- ✅ **#22 audit endpoint scoping + lifespan teardown.** Consultation audit list and verify routes are owner-scoped for non-root callers, root retains global audit visibility, and lifecycle shutdown drains tracked webhook send tasks.
- ✅ **#24 MCP registry split-brain.** `api/mcp_tools.py` is the canonical MCP registry for stdio and HTTP/SSE; DAG log/branch/diff/checkout and `recommend_model` are exposed through both transports, and HTTP/SSE supports per-user token maps instead of one shared backend principal.
- ✅ **OpenAI compat honesty must-fix #5/#6/#7.** Generation controls propagate through `graeae.route`, SSE streaming is implemented, tools/response_format/multimodal fields are pass-or-400 by provider capability, and `/v1/models/{model_id}` returns 404 for unregistered models.
- ✅ **#23 entity namespace conflict-key migration.** Entity uniqueness includes namespace, so the same owner can use the same entity name/type in different namespaces.
- ✅ **#19 bulk webhook parity.** `POST /v1/memories/bulk` emits the same transactional `memory.created` outbox events as single-create for successful rows.
- ✅ **Compression cleanup.** LETHE / ANAMNESIS / ALETHEIA modules, `CompressionManager`, and the `DistillationEngine` compatibility wrapper are removed; APOLLO + ARTEMIS remain the built-in contest engines.
- ✅ **Session compression cleanup.** Always-NULL `compression_ratio` columns and legacy `compression_tier` / `compressed` columns are dropped from session tables.
- ✅ **Namespace-uniform tenancy.** State, journal, entities, sessions, consultations, memory reads/history, and webhook outbox writes follow owner+namespace scoping.
- ✅ **PostgreSQL streaming-replication doctrine.** Single-site HA uses Postgres primary/standby replication; federation is reserved for remote/curated data flows.

Remaining after v3.5.0:

- 🔵 **Dedicated deletion-log / GDPR wipe workflow.** v3.5 keeps DELETE tombstone snapshots live in the version DAG, but no separate deletion-log table ships.
- ✅ **PANTHEON + IRIS v0.2 shipped.** The opt-in facade now includes per-session `consultation_only` caps, MNEMOS `pantheon_routing` memory writes, rolling-window adaptive `auto:*` routing, and expanded route explanations; Redis-backed cap sharing and full streaming/tool passthrough remain next-slice work.
- ✅ **NATS substrate v0.2 proof-of-life.** Landed on the v4.2.0a14 line: PANTHEON routing decisions can optionally publish to `mnemos.pantheon.routing`, `db/migrations_v4_2_pantheon_routing_audit.sql` adds the separate `pantheon_routing_audit` table, and `mnemos/workers/pantheon_routing_audit_consumer.py` mirrors events into that audit table when explicitly enabled. Webhook outbox migration and federation rewire remain 🔵 deferred to NATS substrate v0.3.
- ✅ **RFC-002 / MemPalace re-engagement.** Re-open memo drafted with v3.4 CHARON evidence and KNOSSOS interop framing; see `docs/RFC-002-REENGAGEMENT.md`.
- ✅ **Compression hot-path expansion.** All three v3.6 §2.5 surfaces shipped: federation feed `prefer_compressed=true` (v4.2.0a14 round-1, byte-gated to `to_json(text)::text` exact measurement), HTTP `GET /v1/memories/{id}` Accept-header negotiation (round-12, ``text/plain`` → prose, ``application/x-apollo-dense`` → raw dense), and MCP `get_memory` `format=prose|dense` parameter (round-24, surfaces the same prose/dense variants to stdio + HTTP-SSE MCP clients). `compression_applied` / `compression_metadata` on search remain reserved-but-always-false; real compressed reads use the routes above.
- ✅ **Design paper draft.** Git-like DAG + LLM-synthesized distillation/narration + judge-verified fidelity, carried from the v3.4 charter; see `docs/papers/mnemos-dag-distillation.md`.

### v3.6 — PERSEPHONE + MORPHEUS mutation paths

- ✅ **PERSEPHONE — archival subsystem.** Cold-set rotation now moves unrecalled memories into zstd-backed `memory_archive` storage with live stub pointers, explicit restore, root-gated sweeps, and federation-visible archive markers.
  - Constraint: archived memories are not auto-restored on read in this slice; callers must use `POST /admin/persephone/restore/{memory_id}` or `GET /v1/memories/{id}?restore=true` with root/owner authority.
- ✅ MORPHEUS slice 3 — CONSOLIDATE phase. Merge near-duplicate clusters into a canonical with `permission_mode=400` read-only pointers on originals (`consolidated_into:<canonical_id>`). Soft-only; never hard-delete user data. Shipped via `db/migrations_v4_2_morpheus_consolidate.sql`.
- ✅ MORPHEUS slice 4 — EXTRACT phase. LLM mining of latent KG triples from `verbatim_content` of prose memories not already triplified. Two-model split: fast/quantized for extraction, strong reasoner for synthesis (already the v3.3 slice 2 pattern).

### Deferred beyond v3.4

- Full observability surface (Prometheus metrics, OpenTelemetry traces, default Grafana dashboard).
- Secrets abstraction (unified `SecretsProvider` interface with env-var passthrough, Vault plug-in, KMS plug-in).
- DAG merge conflict resolution (three-way merge with operator-assisted resolution).
- Embedding-axis quantization beyond pgvector's built-in `halfvec` and `bit` types — revisit when official TurboQuant / PolarQuant / QJL reference implementations land with compatible licenses.
- Migration rollback tooling.

---

## v4.0 — Pluggable Monolith + Lite Profile — SHIPPED 2026-04-29

The 4.0 charter was structural, not feature-driven. Tracks 5, 5b, and horizontal
scaling shipped in v4.0.0. Connector gallery expansion moves to v4.1; hosted
cloud, MCP-MD foundation standardization, and larger Rust/mobile work move to
v5.0+ framing.

### Track 5 — modularization + persistence abstraction

Same repo, internal API boundaries enforced by tooling. Pattern: Django, SQLAlchemy, Airflow.

- ✅ `mnemos/` package layout. Subsystems are subpackages: `api/routes`, `core`, `db`, `domain`, `persistence`, `mcp`, `webhooks`, `workers`, `hooks`, `installer`, `tools`, `cli`.
- ✅ Seven `import-linter` contracts in `pyproject.toml`. CI fails on package-boundary regressions.
- ✅ Public API surfaces exposed through package modules; top-level Python script entry points retired in favor of `mnemos`.
- ✅ `mnemos/installer/` extracted from the old top-level installer flow.
- ✅ Plugin entry-points and ABCs remain for `CompressionEngine`; third-party extension work continues after v4.0.
- ✅ Optional extras: `build`, `sqlite`, `tracing`, `structlog`, `docling`, `full`, `phi`.
- ✅ **Persistence abstraction** — `mnemos.persistence.{postgres,sqlite}` swappable.

### Track 5b — SQLite "lite" profile

Same code, same API, same KNOSSOS interop. Single-binary, embeddable, MemPalace-compatible MCP from day one.

- ✅ `mnemos.persistence.sqlite` implementation: SQLite + `sqlite-vec` + FTS5 + JSON1 + WAL.
- ✅ SQL dialect translation lives behind the persistence layer.
- ✅ SQLite migration chain mirrors the Postgres conceptual schema.
- ✅ Single-binary build via PyInstaller ships MNEMOS + sqlite-vec + migrations in one executable.
- ✅ Pitch: **"Run it as a single SQLite binary on your laptop, scale it to a Postgres+pgvector+GPU stack on a fleet, anywhere in between."**

### Track 5c — KRONOS / Tesseract GPU stack

- ✅ KRONOS v0.1 scaffold ships the CPU-only forward path for recall-pattern
  anomalies, namespace drift, recall-load forecasts, and PERSEPHONE eligibility
  forecasting. Breadcrumb: `mnemos/domain/kronos`.
- 🔵 KRONOS v0.2 moves large-history EWMA computation onto the Tesseract/CUDA
  path while preserving the v0.1 NumPy implementation as fallback.

### Track 6 — surface integrations (multi-vendor MCP + REST connectors) — v4.1

MNEMOS exposes mature MCP transports (`mnemos.mcp.stdio`, `mnemos.mcp.http`) and
22 tools from one canonical registry. v4.0 keeps the working MCP surface and the
initial connector docs; broad connector-gallery packaging and bridge tooling are
v4.1 work.

| Surface | MCP support | Plan |
|---|---|---|
| **Claude Code** | ✅ native | Already working; document the registration recipe |
| **Claude Desktop** | ✅ native | Same MCP server file; document config-file path |
| **Cursor** | ✅ native | Same MCP; document `~/.cursor/mcp.json` registration |
| **Codex CLI** (OpenAI's dev tool) | ✅ native (0.125.0+) | Verify config path + ship a `codex mcp add mnemos` recipe |
| **Cline / Continue / Aider** | ✅ native | One-line MCP config snippets in docs |
| **ChatGPT Pro / Team / Enterprise / Edu (web)** | ✅ via Developer Mode | Custom-connector registration; MNEMOS MCP exposed over HTTP/SSE transport. Document the connector-config recipe |
| **ChatGPT free / Plus (consumer)** | ❌ no MCP at this tier | OpenAPI manifest + Custom GPT calling MNEMOS REST API. Bridge until OpenAI broadens MCP access |
| **ChatGPT Desktop app** | ⚠️ partial | Track app-side connector support as it stabilizes |
| **Gemini / Code Assist / IDX** | ❌ no MCP | Build `mcp-to-gemini-functions` bridge: translate MCP tool definitions to Gemini's function-calling JSON schema. Document REST-direct path as fallback |
| **OpenWebUI / LM Studio / Ollama** | ⚠️ partial | OpenAI-compat tool-call path; MNEMOS REST endpoints accessible via tool-use config |

Deliverables:
- ✅ Initial `docs/connectors/` directory exists with ChatGPT Pro Developer Mode guidance.
- 🔵 v4.1: one Markdown per surface with exact config snippets.
- ✅ v4.1: `mnemos-openapi.json` artifact for Custom GPTs and OpenAPI-aware clients — landed v4.2.0a14 round-36/37 as the new ``mnemos dump-openapi [-o PATH] [--indent N] [--title TITLE] [--target full|gpt-actions]`` CLI command. Builds the FastAPI OpenAPI spec without booting the server; suitable for CI / static-distribution workflows. ``--target gpt-actions`` truncates endpoint summary/description (300 chars) and parameter description (700 chars) per OpenAI's Custom GPT Actions production limits, so a Custom GPT importing the artifact does not silently truncate or fail. Operators with a running server can also continue to ``curl /openapi.json``.
- 🔵 v4.1: Gemini and OpenAI Actions bridge packages if demand holds.
- ✅ v4.1: smoke tests per surface where automatable — landed v4.2.0a14 in `tests/test_connector_smoke.py`, with operator runbook notes in `docs/connectors/README.md`.

---

## Audit Remediation Log

Every Codex / GRAEAE / stop-hook audit finding from the v3.2.x and v3.3.x cycles, with status. Maintained release-by-release; new findings append. ✅ = remediated, 🔵 = planned, ⏳ = deferred.

### v3.5.0 slice 1 — audit quick wins

- ✅ Session history returned the oldest 10 messages instead of the most recent 10 — fixed in `f9ea8d9`; deterministic system-row pinning refined through `e3c884c`.
- ✅ Repository metadata still pointed at `perlowja/mnemos` after the org move — swept to `mnemos-os/mnemos` in `c3092c6`.

### v3.5.0 slice 2 — memory-read tenancy + DAG integrity

- ✅ `list_memories` / `get_memory` used narrower owner+namespace checks than the intended read contract — closed by shared `read_visibility_predicate` (`api/visibility.py:40-96`) and handler adoption in `api/handlers/memories.py`.
- ✅ Search/rehydrate cache keys could collide across `None`, empty string, and caller group variation — closed with JSON serialization and group IDs in the key.
- ✅ Version and DAG read paths could expose historical private snapshots through a later-public live memory — closed by `version_visibility_predicate` (`api/visibility.py:99-137`) on version/log/commit/diff/checkout paths.
- ✅ Recursive DAG logs and parent-hash subqueries could cross memory boundaries or bridge hidden snapshots — closed with same-memory joins and immediate-parent visibility checks (`api/handlers/dag.py:117-245`).
- ✅ Merge/revert writers could race branch HEAD movement or copy stale tenancy — closed with shared branch advisory locks, row-lock ordering, target-head visibility gates, drift guards covering tenancy, and target-derived tenancy on new commits.
- ✅ HTTP/MCP branch creation had TOCTOU and duplicate-race windows — closed with `FOR SHARE` parent locks plus `INSERT ... ON CONFLICT DO NOTHING RETURNING` (`api/handlers/dag.py:341-450`, `api/mcp_tools.py:183-383`).
- ✅ `mnemos_version_snapshot()` could write a parent edge to another memory if `memory_branches.head_version_id` was corrupt — closed by `db/migrations_v3_5_trigger_same_memory_parent.sql`, which raises `MN001` and lets `handle_trigger_pgerror` map it to HTTP 409.
- ✅ `mnemos_group_select` used threshold math that admitted owner-only mode 700 for group members — closed by `db/migrations_v3_5_rls_group_select_unix_bits.sql`, which replaces the policy with the same Unix group-bit expression as `read_visibility_predicate`.
- ✅ MCP stdio and HTTP/SSE exposed different tool surfaces — closed by moving CRUD/KG/DAG/model tool metadata and dispatch behind `api/mcp_tools.py::TOOL_REGISTRY`, with parity tests for stdio, HTTP/SSE, and per-token MCP HTTP attribution.

### Codex round 1 — 9-commit deep probe (early v3.2.x cycle)

5 bugs found across the session's commit set 71b40e0..58011a9; all fixed in commit `1c56488` (compression scoring math, Artemis assembly, Apollo schema FP guards).

- ✅ All 5 bugs remediated.

### Stop-hook reviews during v3.2.1 development

- ✅ `federation.py` non-UTC ISO 8601 cursor handling — UTC-normalize before strip-tzinfo (v3.2.1).
- ✅ Startup-time GRAEAE manifest reload stalls boot + holds DB conn — moved to `_schedule_background()` with 120s `wait_for` cap; Phase 1 (DB) releases conn before Phase 2 (parallel probes) (v3.2.1).
- ✅ Background reload undone by concurrent consult overrides — overrides via `model_override` param; `_query_provider` snapshots provider config (v3.2.1).
- ✅ Override refactor broke gateway model-override path — `engine.route()` now passes `model_override`; gateway strips matching prefix (v3.2.1).
- ✅ Gateway prefix-strip breaks legitimate slash-bearing model IDs — strip only matching `<provider>/`; resolver tries bare + namespaced lookups (v3.2.1).
- ✅ Bare `claude-opus-4-7` resolves to `anthropic` not `claude` (engine key) — reverse-map `_REGISTRY_MAP`; strip accepts either name as prefix (v3.2.1).
- ✅ Resolver semantics changed without test updates — 1 test updated, 3 new tests added; 10/10 pass (v3.2.1).

### Codex deep-review of v3.2.1 (task `task-modqloxk-o6tgad`)

3 HIGH blockers + several validations.

- ✅ `mnemos_version_snapshot()` UPDATE branch wrote OLD into version rows (semantics inverted) — UPDATE now inserts NEW; migration `db/migrations_v3_2_2_version_snapshot_new_values.sql` (v3.2.2).
- ✅ Federation cursor timezone drift — `next_cursor` now emitted with explicit `Z` suffix; puller's `astimezone(UTC)` is a no-op (v3.2.2).
- ✅ Custom Query selection silently dropped Anthropic muse — `_REGISTRY_TO_GRAEAE` reverse-map applied to `_resolve_models` / `_tier_lineup` / providers-list path (v3.2.2).
- ✅ Validated: auth-gating on new endpoints, gateway resolver matrix, race-fix shape, ARGONAS cherry-picks runtime-benign.

### Codex source-vs-live audit (after v3.2.2 reconcile)

- ✅ Version-source drift: `_version.py` single literal; api_server / health / portability all import; pyproject + pip metadata + /health + /openapi.json all agree at 3.2.3 (v3.2.3).
- ✅ Docker pip metadata stale at 3.1.0: `.dockerignore` drops `*.egg-info`; Dockerfile installs the package after `COPY . .` (v3.2.3).
- ✅ `/v1/documents/import` bypass: now uses `mem_<hex12>` ids, populates `verbatim_content` / `quality_rating` / `permission_mode`, dispatches `memory.created` webhooks per chunk, invalidates search cache (v3.2.3).
- ✅ Stale docs: README current-version paragraph rewritten to v3.2.3; SPECIFICATION endpoint count 91→96; release-history extended (v3.2.3).
- 🔵 MPF portability partial (`kind=memory` only) — deferred to v3.4 CHARON v0.2.
- ✅ `/v1/memories/search` `compression_applied` / `compression_metadata` reserved-but-always-false — formally documented as reserved in v3.5.1. Real compressed reads use `/v1/memories/rehydrate` and compression manifests.

### Codex round-2 portability + APOLLO audit (after v3.2.3)

- ✅ MPF import/export rich envelopes (kg_triples, documents, facts, events, compression_manifest, memory_versions) — shipped in v3.4 CHARON v0.2.
- ✅ Legacy `/memories` POST path returned 404 against current API (`/v1/memories`) — fixed (v3.2.4).
- ✅ Adapter `payload_version` conflict handling for richer MPF sidecars — shipped in v3.4 CHARON v0.2.
- ✅ `_post_mpf` envelope missing `exported_at` (failed own validator) — added (v3.2.4).
- ✅ `tools/memory_export.py text` import error (`export_memories_text` → `export_memories_plaintext`) — fixed (v3.2.4).
- ✅ ChatGPT `--category` override ignored — now honored when set; auto-classify only when unset (v3.2.4).
- ✅ APOLLO LLM fallback wasted GPU without judge enabled (4.4% win rate on 2,146 dispatches/day) — startup warning when both enabled+judge-off; `MNEMOS_APOLLO_LLM_FALLBACK_ENABLED` flipped off in PYTHIA prod (v3.2.4 + ops).

### OpenClaw dream-architecture comparison (informational)

Pattern absorption opportunities surfaced by reading OpenClaw issues #70072, #65630, #67413, #70402, #64756.

- ✅ Recall-frequency tracking columns (`recall_count`, `last_recalled_at`, `unique_queries`) — shipped in v3.3.
- ✅ Per-cluster introspection artifact (`morpheus_clusters` table + `/v1/morpheus/runs/{id}/clusters`) — shipped in v3.3.
- ✅ Per-namespace dream scoping (`morpheus_runs.namespace` filter) — shipped in v3.3.
- ❌ Flat-file storage (`MEMORY.md` promotion target) — explicitly skipped; Postgres is canonical.
- ❌ Promotion-gate-as-primary-mechanism — explicitly skipped; MORPHEUS is synthesizer, not triage. PERSEPHONE (v3.6) covers archival decisions.

---

*This document reflects committed plans, not speculative features. Items listed here are intended to land in their scheduled release unless explicitly deferred with an ADR. Priorities may shift during the release cycle; the document will be updated in the same commit that shifts them.*

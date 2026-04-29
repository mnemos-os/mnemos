# MNEMOS v3.5 Charter — PANTHEON v0.1 + IRIS Discovery Layer + Memory Operations Expansion

**STATUS: SHIPPED.** v3.5.0 shipped 2026-04-28 as an audit-hardening and
uniform-tenancy release; v3.5.1 is the 2026-04-28 documentation-triage patch.
No product behavior changed between v3.5.0 and v3.5.1.

**Document status:** Historical charter. The original PANTHEON/IRIS feature
pitch below did not ship in v3.5.0; it is preserved as planning record and
moved to later roadmap work. The shipped v3.5.0 scope is the slice sequence
listed in §0.
**Position in roadmap:** Follows APOLLO S-IVB phases 1–2 (v3.2–v3.4); precedes full GPU stack (v4.0).
**Theme:** *Unified LLM provider facade with MCP discovery + foundational memory operations hardening.*

---

## 0. Current branch status

Closed in v3.5.0:

- **DONE in slice 1 (`a62a099`)** — session history order and pinning fixes; repository URL sweep to `mnemos-os/mnemos`.
- **DONE in slice 2 (`d42c475`)** — memory read visibility symmetry, per-snapshot version visibility, same-memory DAG parent guards, race-safe branch creation, merge/revert branch-writer serialization, target-tenancy merge semantics, `MN001` → HTTP 409 reconciliation, and Docker `postgres-upgrade` for existing volumes.
- **DONE in later slices** — webhook retry leases/outbox discipline, federation compound cursor, consultation audit endpoint scoping, MCP stdio/HTTP registry parity, faithful OpenAI-compatible gateway handling, PostgreSQL streaming-replication doctrine, namespace-uniform state/journal/entities/sessions/consultations, bulk webhook parity, compression cleanup, and audit-closure passes.

Deferred after v3.5.0:

- **PANTHEON + IRIS** — unified LLM facade and MCP model discovery layer.
- **Dedicated deletion-log / GDPR wipe workflow** — v3.5 keeps DELETE tombstone
  snapshots live in the version DAG, but it does not add a separate deletion-log
  table.
- **Federation per-peer ACL** beyond current bearer identity, role gate,
  namespace filters, and category filters.

---

## 1. Headline Pitch

Original pitch, not the shipped v3.5.0 outcome: MNEMOS v3.5 was drafted as the **integration & operations release**. It would ship PANTHEON v0.1 — a unified LLM provider facade sitting above GRAEAE and every OpenAI-compatible backend — and IRIS, an MCP server that exposes PANTHEON's model registry so agents discover and select models by capability (not hardcoded names). Together, PANTHEON + IRIS would eliminate the industry-wide problem of agents hardcoding model strings. Agents would stop asking "what model should I use?" and start asking "what are my options for a tool-calling vision model?" IRIS would respond with a ranked list including cost, latency, quality, and availability.

What shipped instead: v3.5.0 hardened the core memory infrastructure and release docs around audit closure, uniform tenancy, webhook retry correctness, MCP registry parity, faithful OpenAI compatibility, and streaming-replication operations.

The release is operationally driven: every item directly supports running MNEMOS at scale.

---

## 2. Server-side scope

### 2.1 PANTHEON v0.1 + IRIS: Unified LLM routing facade and model discovery MCP server

**Ships as:** `pantheon/` subpackage within MNEMOS (or separately; decision per integration review).

**What ships:**

1. **HTTP /v1/ OpenAI-compatible surface.**
   - `POST /v1/chat/completions` — stream or non-stream; routes based on model alias + caller policy.
   - `GET /v1/models` — extended catalog with `pantheon.*` metadata (cost_tier, usage_tier, context_window, capabilities, health, advisory).
   - `POST /v1/embeddings` — routes to free embedding tier or paid, caller-selectable.
   - Streaming requests **bypass the queue** and proxy directly to backend. Non-streaming flows through NATS JetStream queue. See `docs/PANTHEON.md` §7 for the rationale.

2. **Per-tenant tokens + usage_tier enforcement.**
   - Token carries `tenant_id`, `allowed_tiers` (agentic_ok | consultation_only | embedding_only), optional `cost_ceiling_usd_per_day`, optional `model_allowlist` / `model_denylist`.
   - `usage_tier=consultation_only` (Anthropic models only) enforced with hard cap: per-(user, session) limit on calls/hour, agentic-loop detection via repetition rate, 403 on violation (with suggested alternative).
   - All routing decisions recorded in `pantheon_routing` memory category for adaptive policy (see §2.3).

3. **HashiCorp Vault key store (optional; env-var fallback for dev).**
   - Production: `~/.pantheon/keys/*.enc` with master key; operators integrate with Vault, KMS, sealed-secrets as needed.
   - Workers unseal on startup; Pantheon frontend uses Vault API to inject keys into worker tasks.
   - Drop a new provider key = `PUT /v1/admin/provider-keys` (root token only) → workers pick up on next heartbeat.

4. **Worker pool contract (stateless, horizontally scalable).**
   - Each worker subscribes to `work.<provider>` on NATS JetStream.
   - Advertises models on startup → `catalog.advertise` subject.
   - Heartbeats every 30s → `catalog.heartbeat` with current health.
   - Token bucket per provider's stated rate limit; NAK on rate-limit hit.
   - Reference implementation ships for: vLLM (CERBERUS), Together, Groq, OpenAI, Gemini, Perplexity.

5. **Deterministic policy layer (no LLM in the routing decision).**
   - Model aliases: `auto:reasoning`, `auto:cheap-fast`, `free:embedding`, `tool:json`, `consensus:reasoning`.
   - Alias resolution at request time using: tenant policy, worker health, MNEMOS rolling stats (last 15 min per backend).
   - X-Pantheon-* headers as hints: `X-Pantheon-Cost-Tier`, `X-Pantheon-Latency`, `X-Pantheon-Capability`, `X-Pantheon-Mode`.
   - All decisions are pure functions — zero Claude/GPT calls. Improvements via operator tuning or A/B testing.

**Catalog auto-population from GRAEAE (not reimplemented):**
- PANTHEON reads GRAEAE's existing `muses_api_keys.json` + provider registry on startup.
- Catalog is a *view* over GRAEAE's data + per-worker advertisements.
- Adding a provider = one step: drop key into GRAEAE, PANTHEON picks it up next reload (or SIGHUP).
- `usage_tier` annotations per model configured once in GRAEAE; PANTHEON enforces verbatim.

**Entry points (PROPOSED — to be created in v3.5):** `api/pantheon/frontend.py`, `api/pantheon/workers/`, `api/pantheon/catalog.py`, `api/pantheon/auth.py`.

### 2.1b IRIS MCP server: Model discovery and capability-based selection

**Strategic rationale:** The entire agentic ecosystem has accepted "hardcode model name in config" as the discovery API. This is brittle and fragile. OpenAI's `GET /v1/models` has carried rich metadata since 2023 — capability flags, cost tiers, latency profiles — but agents ignore it and hardcode strings anyway. IRIS fixes this by exposing PANTHEON's model registry via MCP (the standard protocol all modern agent frameworks speak). Agents query IRIS instead of consulting config files.

**What IRIS ships:**

An MCP server (`pantheon-iris`) with two primary surfaces:

1. **Tools (for Claude Code, Cursor, Continue, etc.):**
   - `find_model(requirements: {capabilities: [tool_calling, vision, json_mode, function_calling, ...], min_context_tokens: int, max_cost_per_mtok: float, min_quality_score: float})` → returns ranked list of PANTHEON models matching all constraints, with cost/latency/availability metadata.
   - `get_model_health(provider: str, model: str)` → returns current circuit-breaker state, recent latency p99, error rate, uptime_percent.
   - `register_preference(agent_id: str, task: str, requirements: {...})` → agent declares its profile; IRIS tracks performance over time (optional; drives learning in future versions).

2. **Resources (for config-file-driven agents like zeroclaw):**
   - `iris://models` → full catalog as JSON (same shape as `GET /v1/models`).
   - `iris://models/health-snapshot` → current health rollup across all backends.
   - `iris://models/recommendations/{task}` → task-typed recommendations (task = "reasoning" | "vision" | "coding" | "embedding"); returns ranked list accounting for cost + quality.

**Integration with PANTHEON:**
- IRIS reads from PANTHEON's `model_registry` table (auto-populated from GRAEAE's provider catalog).
- IRIS queries PANTHEON's `routing_log` and `usage_log` tables for health + performance signals.
- IRIS is stateless (reads only); PANTHEON remains the source of truth.

**MCP interface:**
```python
# api/iris/server.py
class IRISServer(MCPServer):
    """MCP server for model discovery over PANTHEON catalog."""
    
    @tool
    async def find_model(self, requirements: dict) -> list[ModelMetadata]:
        """Query PANTHEON catalog by capability constraints."""
        
    @tool
    async def get_model_health(self, provider: str, model: str) -> HealthStatus:
        """Current health state from PANTHEON routing log."""
        
    @resource
    async def models_catalog(self) -> dict:
        """Full PANTHEON catalog."""
        
    @resource
    async def recommendations(self, task: str) -> list[ModelMetadata]:
        """Task-typed ranked recommendations."""
```

**Entry points (PROPOSED — to be created in v3.5):** `api/iris/server.py` (MCP server), `api/iris/discovery.py` (catalog querying), `api/iris/health.py` (health aggregation).

**Key design decision — why MCP and not just a REST API:**
MCP is already the standard protocol that Claude Code, Cursor, Continue, Codex, and all modern agentic IDEs speak natively. Adding IRIS as an MCP server means agents configure one line (`mcp_servers.iris.url`) and get native discovery. The alternative (REST-only) means each framework implements adapter code. MCP collapses the problem to a single artifact.

### 2.1c MCP-MD v0.1 — open specification posture

IRIS is the reference implementation of a broader capability-based model discovery protocol intended as an **open specification called MCP Model Discovery (MCP-MD v0.1)**. The protocol — its capability schema, query semantics, health vocabulary, and fallback rules — is independent of MNEMOS and designed for multi-implementer adoption.

**Specification & standardization path:**

- Original plan: v3.5 would ship IRIS + a draft specification at `docs/spec/MCP-MD-v0.1.md` (separate from MNEMOS feature docs to signal vendor-neutral intent). This did not ship in v3.5.0.
- The specification is explicitly draft-stage; breaking changes are possible through v3.6 and into v4.0 pending implementer feedback.
- Over v3.5–v4.0, the goal is to build adoption signals from multiple implementers (MNEMOS as reference, zeroclaw, OpenClaw, ideally external partners). Once multi-implementer adoption demonstrates merit, the specification will be **proposed to the Linux Foundation AI & Data working group** as a Sandbox project for neutral standardization.
- MNEMOS remains the reference implementation throughout; the specification itself may eventually graduate to LF-hosted neutral infrastructure once v1.0 stabilization is achieved and the proposal is accepted.
- **Explicit posture:** We intend to propose MCP-MD to LF AI & Data for open standardization, not as a shortcut to acceptance but as a natural outcome of demonstrated demand and multi-vendor interest. Success of the proposal depends entirely on the merit of the protocol and evidence of real adoption.

**Pre-public-release workflow:**

Before any public release of the MCP-MD v0.1 specification, the draft will be circulated for informal peer review with recognized experts in the OSS infrastructure and agentic systems space. This review informs quality and adoption likelihood, but proceeds with no formal commitments or priority access to standardization processes.

### 2.2 Operational hardening

All items required for v3.5 to be production-ready at scale:

0. **DONE in slice 2: memory-read tenancy + DAG integrity.**
   - Shared live read predicate: `api/visibility.py:40-96`.
   - Per-snapshot history predicate: `api/visibility.py:99-137`.
   - Trigger replacement: `db/migrations_v3_5_trigger_same_memory_parent.sql`.
   - Branch-writer lock helper: `api/handlers/dag.py:21-40`.
   - Existing-volume upgrade path: `postgres-upgrade` in both compose files.
   - RLS Unix-bit migration: `db/migrations_v3_5_rls_group_select_unix_bits.sql` (#25).

1. **HTTP body-size cap** (FastAPI Body limit).
   - `POST /v1/import` and streaming request bodies must have hard cap.
   - Default: 512 MB for imports (configurable); streaming replies inherit.
   - Return 413 on overflow. Already implemented in CHARON v0.2 round 40–41; validate ship-readiness.
   - Affected files: `api/handlers/portability.py:340+`, `api/handlers/openai_compat.py` (streaming).

2. **Per-tenant rate limiting at API gateway** (replacing in-memory default).
   - Move from GRAEAE's in-process semaphore to **shared state** (Redis, or Postgres LISTEN/NOTIFY via a simple polling loop).
   - Per-tenant window: requests/minute, optionally embeddings/minute (separate bucket), optionally cost/day (cost computed post-request from provider usage).
   - Tenants exceeding cap get 429; advisory includes reset time.
   - Affected files: `api/rate_limit.py`, `api/models.py:TenantPolicy`.

3. **Audit log table for cross-tenant operations.**
   - New table: `audit_log(id, timestamp, tenant_id, operation, resource_id, old_value, new_value, caller_identity, reason)`.
   - Triggers on: memory mutations (create/update/delete), ownership transfers, federation pulls (for audit trail across systems), tenant config changes.
   - Read path: `GET /v1/audit?tenant_id=...&resource_id=...&since=ISO8601` (scope-gated to caller's tenant).
   - Migration: `db/migrations_v3_5_audit_log.sql` (to be created in v3.5).
   - Affected files: `db/`, `api/handlers/admin.py`.

4. **Retry + circuit-breaker on `_get_embedding`.**
   - Embedding endpoint calls (for embedding-as-a-service backends like OpenAI) can be flaky.
   - Add exponential backoff (3 retries, 1s/2s/4s delays) + circuit breaker (trip after 5 consecutive failures, half-open after 30s).
   - If circuit trips: fast-fail embedding operations with a retriable 503, suggest retrying.
   - Affected files: `compression/apollo.py:_get_embedding`, `api/pantheon/workers/embedding_worker.py` (to be created in v3.5).

5. **Long-tx idle_in_transaction timeout handling.**
   - APOLLO phase 4 EXTRACT (§2.4) may run multi-minute LLM calls while holding a transaction.
   - Set Postgres `idle_in_transaction_session_timeout` to 5 minutes in `api/models.py:pool()`.
   - Guard all long-running calls with explicit `asyncpg.create_pool(..., init=_set_timeouts)`.
   - Affected files: `api/models.py`, `compression/apollo.py` (EXTRACT path).

6. **Connection pool concurrency cap.**
   - asyncpg pool size bounded to max(4, cpu_count // 2) by default.
   - Respect operator override via `MNEMOS_POOL_SIZE` env var (no upper limit; operators know their infra).
   - Don't spawn unlimited workers on high concurrency; document the pool-exhaustion scenario + remediation.
   - Affected files: `api/models.py:pool()`.

### 2.3 APOLLO S-IVB phase 4: EXTRACT latent KG triples from prose

**Scope:** Mine latent knowledge graph triples from `verbatim_content` of prose memories that are not already triplified.

**Pattern (follows v3.3 slice 2):**
- Worker process subscribes to `memory_extract_queue` (write-on-ingest for eligible memories).
- Per memory: seed with the prose + existing triple count → fast/quantized extractor ("Is there a `person:project` edge hiding in this text?").
- If extractor says yes: call strong reasoner (default: GRAEAE consensus) to synthesize the triple.
- Result: write to `kg_triples` with `extracted_from:<memory_id>` parent link + confidence score.

**Two-model split (cost optimization):**
- **Fast extractor** (quantized, CPU-capable): `mistral-7b-instruct` via CERBERUS vLLM or local Ollama.
- **Strong reasoner** (synthesis): default GRAEAE, fallback to Together `llama-4-405b` if GRAEAE busy.

**Out:** `kg_triples` rows with `source=extracted`, `confidence_score` in [0, 1], `reasoning_notes` field.

**Affected files:** `compression/apollo.py` (new EXTRACT phase 4 methods), `api/handlers/morpheus.py` (queue worker), `db/migrations_v3_5_apollo_extract.sql` (queue table).

### 2.4 PERSEPHONE archival subsystem foundation (planned, not shipped v3.5)

**Deferred to v3.6.** v3.5 prepares the substrate:
- Recall-tracking columns (added v3.3) used to identify cold-set candidates.
- Design doc for cold-rotation policy + federation-aware restore.
- **v3.5 does NOT ship the actual archival path; v3.6 does.**

### 2.5 Distill-on-ingest as default write path

**Status:** Already decided v3.4; v3.5 validates operational readiness.

- Memory `create` endpoint automatically enqueues to `memory_distillation_queue` on insert.
- Compression worker picks it up async; client sees immediate 201 (distillation = fire-and-forget background).
- `/v1/memories/{id}` read path prefers the distilled variant if present (via `Accept` header: `text/plain` → narrated; `application/x-apollo-dense` → dense form).

**Validation items for v3.5:**
- Distillation queue latency p99 < 5s (measure on PYTHIA production).
- No memory loss if worker crashes mid-distillation (idempotent re-run on restart).
- Dashboard metric: `distillation_queue_depth`, `distillation_latency_p50_ms`, `distillation_success_rate`.

### 2.6 Embedding migration: NV-EmbedQA-1B-v2 NIM deployment + reembedding strategy

**Scope:** MNEMOS currently uses `OpenAI embed-small` (1536 dim) via httpx calls to CERBERUS. v3.5 introduces a managed NIM deployment so embeddings scale with the fleet, and optionally migrate to a better open model (NV-EmbedQA-1B-v2, 768 dim, better semantic quality for memory retrieval).

**Migration decision tree (implementation choice, not blocking):**
- **Option A: augment-then-replace** — keep old embeddings until all new queries prefer new embeddings, then drop old columns.
- **Option B: alter-and-backfill** — alter column type, backfill new embeddings (batch job, ~2 hours on PYTHIA), drop old immediately.

**Original v3.5 plan:** NIM deployment Helm values + docs for CERBERUS. Reembedding logic stubbed; actual migration left to operator. This did not ship in v3.5.0.

**Affected files:** `api/pantheon/workers/embedding_worker.py` (NIM endpoint), `api/models.py` (embedding config), `docs/EMBEDDING_MIGRATION.md` (to be created in v3.5).

### 2.7 Reranker integration: NV-RerankQA-1B-v2 NIM

**Scope:** Optionally second-stage `POST /v1/memories/search` results via reranker before returning to client.

**Pattern:**
- User config: `MNEMOS_RERANKER_ENABLED=true`, `MNEMOS_RERANKER_NIM_ENDPOINT=http://cerberus:8001`.
- On search: top-K from pgvector + hybrid (200 results); rerank top-100; return top-20 to client.
- Cost: ~50ms per search, worth 5–10% quality improvement on long-tail queries.

**Affected files:** `api/handlers/memories.py:search()`, `api/pantheon/workers/reranker_worker.py` (to be created in v3.5; optional), `docs/PERFORMANCE.md` (benchmark before/after).

### 2.8 C3 search compression flags decision

**Current status:** Reserved in API but never implemented.

**v3.5 decision point:** either implement or formally document as reserved.

**Option A (implement):** 
- Compression algorithm parameter on `/v1/memories/search`: `compression=lz4` | `compression=brotli` | `compression=none`.
- Compress responses over ~1MB before transmission.
- Client responsible for decompression.

**Option B (formally reserve):**
- Document in SPECIFICATION.md: "C3 flags reserved; streaming is the preferred large-result-set mechanism."
- Remove from API spec; keep as internal note.

**Recommendation:** Option B (reserve). Streaming result sets via Server-Sent Events is cleaner and avoids client-side decompression logic.

**Affected files:** `api/handlers/memories.py`, `docs/SPECIFICATION.md`.

### 2.9 MemPalace RFC-002 re-engagement preparation

**Not shipped v3.5; prepared.**

- Ensure KNOSSOS phase 2 (v3.4) proves interop with MemPalace.
- Draft talking points: MNEMOS compression + dreams, GPU budget efficiency, federation story.
- Positive-sum framing: "Local-first + production-scale; composable not competitive."
- **v3.5 does not send RFC; v3.5 hands off to operator/maintainer for timing.**

---

## 3. Client-side: IRIS adoption strategy (REPLACES old "donations" model)

**Critical design principle:** With IRIS in place, the old strategy of "patch every framework's provider abstraction" is obsolete. Instead, agents that speak MCP simply add IRIS to their config and get runtime capability negotiation.

**The strategic shift:** v3.4 framed the problem as "seven frameworks, seven separate PRs to add PANTHEON support." v3.5 solves it by shipping IRIS, collapsing the problem to **one MCP server that all MCP-aware frameworks already know how to consume**. Frameworks don't need patches; they just need a config line.

**Original plan, deferred after v3.5.0: IRIS plus first-wave reference implementations:**

### 3.1 zeroclaw IRIS adoption (reference implementation — own repo, full control)

**Target:** zeroclaw agents discover and select models from IRIS instead of hardcoded config.

**Change shape:**
```toml
# Old: nine separate provider blocks + static model list
[providers.models.openai]
name = "openai"
apiKey = "sk-..."

[providers.models.groq]
name = "groq"
apiKey = "gsk-..."

# New: single PANTHEON endpoint + IRIS for discovery
[providers.models.pantheon]
name = "pantheon"
baseUrl = "http://pythia:5002"
apiKey = "pantheon-<tenant-token>"

# MCP server reference
[mcp.servers.iris]
command = ["python3", "-m", "mnemos.iris.server"]
env = {PANTHEON_API_KEY = "pantheon-<tenant-token>"}
```

**Agent behavior:** On startup, connect to IRIS via MCP. Call `find_model(capabilities=[tool_calling], max_cost_per_mtok=0.001)`. IRIS returns ranked models with metadata. User can see options, IRIS defaults to top-ranked. Runtime `/model query vision` dynamically re-queries and switches.

**Historical estimate:** ~2 hours (integrate IRIS MCP client + dynamic model selection logic). Did not ship in v3.5.0; carried forward with the deferred PANTHEON/IRIS scope.

### 3.2 OpenClaw IRIS adoption (medium difficulty — NVIDIA contributor, not owner)

**Target:** OpenClaw agents use IRIS MCP for provider discovery.

**Change shape:** Same as zeroclaw's IRIS config, applied to `~/.openclaw/config.toml`. Contributor-level PR to `zeroclaw-labs/openclaw`.

**Historical estimate:** ~3 hours including upstream coordination. Did not ship in v3.5.0 or the v3.5.1 doc-triage patch.

### 3.2a Rate-limiting upstream PR donations

The donation work (zeroclaw + OpenClaw IRIS adoption PRs in v3.5) must respect a hard ≤3–4 PRs/24h ceiling on `perlowja` pushes to any single upstream. Background: GitHub abuse heuristic enforcement on 2026-04-25 was triggered by 8+ PRs in 48h + force-push velocity to a single upstream; rate-limit cannot be assumed safe even when `gh` reports headroom. Pacing approach: stage donation PRs across multiple days, batch via GitLab + ARGONAS first, push final reviewable batch to GitHub. See `~/.claude/rules/github-behavior.md` for the full rule + diagnostic methodology + escalation path.

### 3.3 Second-wave IRIS adoptions (v3.6+)

With IRIS in place, other MCP-aware frameworks (Hermes, Continue, AutoGPT, CrewAI, langchain + LiteLLM) adopt it via config-only changes. No framework-specific patches needed; each just adds an `mcp_servers.iris` entry. Effort per framework drops from "4-5 hour PR + code review" to "config documentation + verification smoke test" (~1 hour each, deferred to v3.6).

**Why deferred:** v3.5's focus is shipping IRIS + proving it works on zeroclaw + OpenClaw. Once IRIS is stable and documented, adoption by other frameworks is straightforward and can happen in parallel post-release.

---

## 4. Explicitly NOT in v3.5

- **KRONOS** (Tesseract time-series integration) → deferred beyond v4.0
- **API/package consolidation** → shipped in v4.0
- **MCP memory-tool consolidation** → shipped in v4.0 under `mnemos/mcp/tools/`
- **Encrypted CHARON envelopes** (NaCl-style optional encryption) → deferred beyond v4.0
- **GDPR wipe path** (right-to-be-forgotten for compliance) → deferred beyond v4.0
- **Rust ports** → v5.0
- **Full PERSEPHONE archival** (cold-set rotation) → v3.6; foundation only in v3.5
- **APOLLO S-IVB phases 3–4** (CONSOLIDATE / ARCHIVE / EXTRACT mutation paths) → v3.6; EXTRACT mining only in v3.5

---

## 5. Estimated effort

**Server-side:**
- PANTHEON frontend + worker contract: 8–10 days (includes integration testing with real backends).
- Catalog auto-population from GRAEAE: 2 days.
- Vault integration (optional): 2 days (skip for dev; operators integrate).
- Operational hardening items (body cap, audit log, circuit breaker, timeouts, pool cap): 3–4 days (mostly already implemented; validation + shipping).
- **IRIS MCP server** (new): 2–3 days (build discovery tools + resource handlers, wire to PANTHEON catalog).
- APOLLO EXTRACT phase 4: 5–6 days (reuses compression harness).
- Embedding migration + reranker stubs: 2 days.

**Total server-side:** 6–8 days focused work.

**Client-side adoptions (IRIS):**
- zeroclaw IRIS integration + testing: 2 hours.
- OpenClaw IRIS PR + coordination: 3 hours.

**Documentation:**
- PANTHEON docs (already in `docs/PANTHEON.md`): 0 (complete).
- **IRIS docs** (new): 1 day (discovery API reference, MCP config snippets for zeroclaw + OpenClaw + future frameworks).
- v3.5 release notes + migration guide: 2 days.

**Total:** ~6–8 weeks calendar time (assuming 1–2 dev weeks/person, plus upstream coordination overhead for OpenClaw). **Net reduction vs old strategy:** framework-patch hours drop from ~7 (zeroclaw 3h + OpenClaw 4h + future donations) to ~5 (zeroclaw 2h + OpenClaw 3h), because v3.6+ adoptions are config-only.

---

## 6. Success criteria

- [x] Slice 1 audit quick wins shipped in v3.5.0 (`a62a099`).
- [x] Slice 2 memory-read tenancy + DAG integrity shipped in v3.5.0 (`d42c475`).
- [x] v3.5 trigger replacement wired into `install.py`, `installer/db.py`, `docker-compose.yml`, and `docker-compose.staging.yml`.
- [ ] PANTHEON v0.1 running on PYTHIA, accessible at `http://pythia:5002/v1/chat/completions` with extended catalog. **Deferred after v3.5.0.**
- [ ] **IRIS MCP server operational:** `iris://models` resource returns full catalog; `find_model()` tool ranks models by capability constraints; `get_model_health()` reflects PANTHEON health. **Deferred after v3.5.0.**
- [ ] zeroclaw + OpenClaw both using IRIS for model discovery (MCP connection working; runtime model selection via `iris://models/recommendations/coding`).
- [ ] Audit log table populated on memory create/update/delete + federation pulls.
- [ ] Body-size cap + per-tenant rate limit enforced, 413/429 responses working as documented.
- [ ] APOLLO EXTRACT mining at least 10% of eligible prose memories on PYTHIA production.
- [ ] PANTHEON routing log feedback loop proves adaptive policy (provider latency improvements over 24h).
- [ ] zeroclaw + OpenClaw IRIS adoptions merged upstream or documented as PRs for operator merge.

---

## 7. Shipping readiness

v3.5 GA gate:
- [x] Slice 2 tenancy/DAG regression suite merged; 768 tests passing in branch context.
- [ ] PANTHEON integration tests (vLLM, Together, Groq, OpenAI backends; happy path + fallback).
- [ ] **IRIS MCP server:** tests for `find_model()` tool with various capability constraints; health aggregation logic validated.
- [ ] CHARON v0.2 export/import round-trip (carried from v3.4 gate; required for federation audit).
- [ ] zeroclaw + OpenClaw IRIS adoption validated in integration rig (MCP connection, dynamic model selection, fallback on IRIS unavailability).
- [ ] Audit log backfill on production database (retroactive logging of recent operations for consistency).
- [ ] Reranker NIM deployment validated on CERBERUS (optional; skip if unavailable).
- [ ] APOLLO EXTRACT against 10k+ sample from production (prove cost-benefit).
- [ ] All docs updated: PANTHEON, IRIS (new), EMBEDDING_MIGRATION, release notes, contributing guide for adding providers and discovery integration.

---

## 8. Cross-references

- **PANTHEON detailed design:** `docs/PANTHEON.md` (complete, decision-ready).
- **APOLLO S-IVB phases 1–2:** Shipped v3.2–v3.4 (see `ROADMAP.md`).
- **CHARON v0.2:** Shipped v3.4 (portability sidecar system).
- **v3.5.0 slice 1:** `a62a099` audit quick wins.
- **v3.5.0 slice 2:** `d42c475` memory-read tenancy + DAG integrity.
- **v3.4 artifacts:** Compression benchmark, APOLLO S-IVB slice 2, KNOSSOS phase 2.
- **v3.6 followup:** PERSEPHONE archival, APOLLO S-IVB phases 3–4, Hermes/Continue/AutoGPT donations.

---

---

## Appendix: Greek pantheon + IRIS subsystem naming

| Subsystem | Greek | Role | v3.5.0 status |
|---|---|---|---|
| MNEMOS | titaness of memory | core memory store | ✅ core |
| APOLLO | sun god / oracle | convergent compression (S-IC, S-II, S-IVB) | ✅ current compression stack; mutation paths continue in v3.6 |
| CHARON | ferryman | cross-system portability | ✅ v0.2 |
| GRAEAE | gray sisters | multi-LLM consensus | ✅ core |
| PANTHEON | temple of all gods | unified LLM gateway | 🔵 deferred after v3.5.0 |
| **IRIS** | **messenger of the gods** | **MCP discovery layer over PANTHEON's catalog** | **🔵 deferred after v3.5.0** |
| PERSEPHONE | queen of the underworld | archival subsystem | 🔵 v3.6 |

*Charter opened 2026-04-25. Status reconciled 2026-04-28 after v3.5.0 GA and the v3.5.1 documentation patch.*

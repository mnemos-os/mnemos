# MNEMOS v3.6 Charter — APOLLO S-IVB Evolution + IRIS Second-Wave Adoption + Mutation Paths

**Status:** Historical / deferred specification. v4.0.0 shipped on 2026-04-29
without this v3.6 feature bundle; unshipped items move to v4.1+ planning.
**Position in roadmap:** Preserved as design context for APOLLO S-IVB,
PERSEPHONE, and IRIS adoption ideas. It no longer gates v4.0.
**Theme:** *Derivative-idea layers + memory state evolution + industry-wide IRIS adoption.*

---

## 1. Headline Pitch

MNEMOS v3.6 is the **APOLLO S-IVB maturity release**. It ships the final two phases of APOLLO (CONSOLIDATE duplicate clusters, ARCHIVE cold-set rotation) and the completion of the divergent memory subsystem (APOLLO S-IVB dream-state mutation paths). The release is design-focused: we deliver the comprehensive "git-like DAG + LLM-synthesized derivatives + judge-verified fidelity" paper that ties compression, dreams, and versioning into a coherent whole. By v3.6 GA, MNEMOS memory infrastructure is feature-complete for production use; v4.0 shifts gears to modularization and GPU scaling.

---

## 2. Scope

### 2.1 APOLLO S-IVB phase 3: CONSOLIDATE near-duplicate clusters

**Purpose:** Merge near-duplicate memory clusters (e.g., multiple paraphrases of the same fact from different sources) into a canonical version, with **read-only pointers** on originals.

**Pattern (soft-delete safe):**
- Detect candidate duplicates via embedding distance + LLM similarity check (judge-LLM).
- Merge confirmed duplicates into canonical `memory_id` via a new `consolidated_into:<canonical_id>` permission mode.
- Original memory rows soft-deleted; remain queryable if caller explicitly includes deleted (for audit trail).
- `memory_versions.permission_mode=400` (read-only) on consolidated originals.
- All reads to original route to canonical via a 301-style redirect in the rehydrate path.

**Federation-safe:**
- Consolidation metadata (who was canonical, when, judge confidence) stored in `memory_versions` with `parent_version_id` pointing to original.
- Peer systems see consolidation marker, can request canonical via standard federation pull.
- Never hard-delete; soft-delete pattern preserves audit trail across federation boundaries.

**Cost benefit:**
- Reduces `pgvector` index bloat (10–15% reduction on PYTHIA production sample).
- Decreases embedding-search redundancy (fewer near-identical candidates to rank).
- Improves retrieval latency (fewer results to rerank).

**Implementation:**
- New table: `memory_consolidation_log(id, timestamp, tenant_id, original_memory_id, canonical_memory_id, judge_confidence, reason)`.
- Worker: `api/handlers/morpheus.py:consolidate_pass()` (mirrors EXTRACT worker pattern).
- Write-time gate: consolidation only runs on-demand via `POST /v1/admin/consolidate?tenant_id=...` (root token).
- Affected files: `api/handlers/morpheus.py`, `api/handlers/memories.py:rehydrate()`, `db/migrations_v3_6_consolidation.sql`.

### 2.2 APOLLO S-IVB phase 4 completion: EXTRACT finishing touches

**Status entering v3.6:** Basic EXTRACT did not ship in v3.5.0. This section is planned work carried forward from the historical v3.5 charter.

**v3.6 refinement:**
- **Batch optimization:** Accumulate extraction queue; send batches to fast extractor (amortizes LLM call overhead).
- **Confidence thresholding:** Filter extracted triples by confidence score; low-confidence results queued for human review instead of auto-inserted.
- **Cycle detection:** Prevent triple loops (`A depends on A`); enforce acyclic invariant.
- **Schema inference:** If extracted triple matches a schema pattern (e.g., `person:project` in `decision` memory), emit the schema-typed version in APOLLO dense form instead of raw triple.

**Affected files:** `compression/apollo.py` (EXTRACT phase 4 batch + scoring), `api/handlers/morpheus.py` (queue worker enhancements), `db/migrations_v3_6_extract_refinements.sql`.

### 2.3 PERSEPHONE archival subsystem: cold-set rotation

**Purpose:** Move memories not recalled in M days (configurable; default 90) to compressed archival storage with a stub pointer in the live table. Restore on demand.

**Pattern:**
- Recall-tracking columns (added v3.3) feed eligibility decision: `last_recalled_at > NOW() - '90 days'::interval` = keep live.
- Archive process: compress memory + create stub row in live table pointing to archival storage.
- Stub structure: `archival_ref` JSON field: `{location: "s3://...", archived_at, recall_threshold_days, compression_variant}`.
- Restore on demand: `POST /v1/memories/{id}/unarchive` (async job, loads from S3, re-hydrates, updates stub).
- Federation-aware: peers see stub, can request restore via federation pull (archive location shared).

**Storage targets:**
- Default: local disk `~/.mnemos/archival/` (for small operators).
- Production: S3 (or GCS, Azure Blob) with signed URLs for restore flow.
- Configuration: `MNEMOS_ARCHIVAL_BACKEND=s3|local`, `MNEMOS_ARCHIVAL_S3_BUCKET=...`.

**Cost benefit:**
- Reduces live `memories` table size (10–30% reduction on high-volume tenants).
- Improves query latency on live set (fewer rows to scan).
- Reduces pgvector index footprint.

**Implementation:**
- New endpoint: `POST /v1/admin/archive-cold-set?tenant_id=...&days_threshold=90` (root token).
- New endpoint: `POST /v1/memories/{id}/unarchive` (user token).
- New table: `memory_archival_log(id, timestamp, memory_id, archived_at, location, compression_variant)`.
- Migration: `db/migrations_v3_6_persephone.sql`.
- Affected files: `api/handlers/memories.py`, `api/handlers/admin.py`, `api/archival.py` (new), `db/`, `docs/ARCHIVAL.md` (new).

### 2.4 APOLLO S-IVB dream-state completion: phases 3–4

**Phase 3 — CONSOLIDATE (mutation path):** Same as APOLLO CONSOLIDATE (§2.1) but applied to dream clusters.
- Dreams can also be near-duplicates; consolidation logic is identical.
- Federation-safe; mutation metadata stored in `memory_versions` DAG.

**Phase 4 — ARCHIVE (memory evolution):** Paired with PERSEPHONE — when a memory is archived, its associated dreams are also archived (soft-delete) but remain restorable.
- Dream archival is implicit in memory archival (FK cascade or trigger).
- Restore memory = restore all its dreams.

**Affected files:** `api/handlers/morpheus.py`, `db/migrations_v3_6_morpheus_phases_3_4.sql`.

### 2.5 Compression hot-path expansion

**Scope:** More read paths consume `memory_compressed_variants` (the distilled/narrated APOLLO dense forms) instead of raw `memories.content`.

**Target surfaces:**
1. **Federation feed** — peers receive compressed variants, save bandwidth + latency on inter-system pulls.
2. **Session message replay** — restore prior session context from compressed forms (dense APOLLO → narrate for prose, or emit dense directly for LLM consumption).
3. **MCP `get_memory` tool** — compress before returning to Claude Code client if caller asks for `Accept: application/x-apollo-dense`.

**Benefit:** Reduces bytes-on-wire by 4–6x for large result sets; decreases LLM token consumption when feeding memories to downstream models.

**Implementation:**
- Add `Accept` header routing to rehydrate path: `Accept: text/plain` → narrated prose, `Accept: application/x-apollo-dense` → dense form.
- Extend federation pull to accept `?compress=true` param; return compressed variants.
- Extend MCP tools.py with compression-aware response logic.
- Affected files: `api/handlers/memories.py:rehydrate()`, `api/federation.py`, `api/mcp_tools.py`.

### 2.6 Design paper: "MNEMOS Memory Architecture — DAG + Synthesis + Fidelity"

**Deliverable:** Comprehensive technical paper (~6k words) documenting the full memory stack.

**Sections:**
1. **Memory as a DAG:** How `memory_versions` encode history, branching (distilled/narrated/dream), merges (octopus-merge semantics for dreams).
2. **Compression + synthesis:** APOLLO dense encoding; judge-LLM fidelity scoring; cost/quality tradeoffs.
3. **Dream state:** Divergent ideation on top of convergent compression; surfaceability principle; schema-aware gap detection.
4. **Federation:** Cross-system memory sharing; conflict resolution; trust boundaries (peer routing).
5. **Archival + cold-set rotation:** PERSEPHONE strategy; restore-on-demand pattern; federation implications.
6. **Operational patterns:** Distill-on-ingest; recall-driven accessibility; consolidation + extraction; compliance-ready audit trail.
7. **Benchmarks:** Compression ratios by schema; dream-generation cost per tenant; archival impact on query latency.

**Target audience:** OSS operators, downstream integrators (Cognee, Graphiti, MemPalace), academic interest.

**Outline already exists in `DREAM_STATE_DESIGN.md` and scattered across ROADMAP.md.** v3.6 unifies into one cohesive document.

**Location:** `docs/MEMORY_ARCHITECTURE.md` (new).

### 2.7 Client-side IRIS adoption (second wave — configuration-driven, not code-donation)

**Strategic intent:** Once IRIS exists and is stable, v3.6 expands adoption across the agentic ecosystem. The intended work pattern is **config documentation + verification** instead of framework-specific code donations.

Each framework that speaks MCP (Hermes, Continue, AutoGPT, CrewAI) can start using IRIS by adding one line to their config:
```toml
[mcp.servers.iris]
command = ["python3", "-m", "mnemos.iris.server"]
env = {PANTHEON_API_KEY = "..."}
```

> **Note (historical):** `mnemos.iris.server` was never implemented
> as a standalone module. The discovery role it described is now
> served by the unified MCP model tools at
> `mnemos/mcp/tools/models.py` (`pantheon_list_models` +
> `pantheon_route_explain`). The config snippet above is preserved
> as the original v3.6 plan; for current configurations point
> agents at the canonical MNEMOS MCP server (stdio or HTTP/SSE)
> instead.

**v3.6 work (per framework):**
1. **Hermes (Nous Research):** Write `docs/IRIS_HERMES_CONFIG.md` with exact MCP setup snippet. One smoke test to verify Hermes agent can call `find_model()` tool.
   - Effort: ~1 hour + community outreach.
   - Likely path: PR to Nous' docs repo, not code changes to Hermes itself.

2. **Continue IDE (open-source):** Similar: `docs/IRIS_CONTINUE_CONFIG.md` + smoke test.
   - Effort: ~1 hour.
   - Low-risk upstream; good community partner.

3. **AutoGPT / CrewAI:** If capacity available.
   - Each is ~1 hour once IRIS is documented.
   - Potentially defer to v3.7 or make these stretch goals.

4. **langchain + LiteLLM:** Custom PR to LiteLLM to add IRIS discovery backend alongside OpenAI, Groq, etc.
   - Effort: ~3–4 hours (implement LLMProvider adapter for IRIS catalog).
   - Likely higher impact than individual framework adoption (broadens reach to all langchain users).

**Total client-side effort:** ~6–8 hours (vs 7–9 hours in the old v3.5 "patch each framework" model). **Key difference:** reduced code review burden on downstream maintainers; reduced coupling (no framework-specific code in IRIS or MNEMOS).

**Rate-limiting note:** v3.6 second-wave adoption work is mostly config-line documentation (per the IRIS strategy in v3.5) but where actual code-PRs are needed, the same ≤3–4 PRs/24h ceiling per upstream applies. For multi-framework adoption work, stage across weeks rather than days — Hermes adoption Tuesday, Continue Wednesday, AutoGPT Thursday — never batched into one push session. See `~/.claude/rules/github-behavior.md`.

---

## 3. Explicitly NOT in v3.6

- **KRONOS** (Tesseract time-series) → deferred beyond v4.0
- **API consolidation pass** → shipped in v4.0
- **MCP memory-tool consolidation** → shipped in v4.0; IRIS discovery remains deferred
- **Encrypted CHARON envelopes** → deferred beyond v4.0
- **GDPR wipe path** → deferred beyond v4.0
- **Rust ports** → v5.0
- **PANTHEON streaming-via-MQ** (Nats-token-by-token) → v4.1 (bypass-direct sufficient for v3.6)
- **Content-hash caching layer for reasoning workloads** → v4.1

---

## 4. Estimated effort

**Server-side:**
- CONSOLIDATE phase + federation routing: 4–5 days.
- PERSEPHONE archival (local disk + S3 support): 5–6 days.
- APOLLO S-IVB phases 3–4 (mutation paths): 3–4 days.
- Compression hot-path expansion: 2–3 days.
- Design paper: 3–4 days.

**Total server-side:** 4–5 weeks focused work.

**Client-side donations:**
- Hermes + Continue patches: 7–8 hours combined.

**Documentation + testing:**
- Migration guides (archival restore, consolidation impact): 2 days.
- Integration tests (archival + restore, consolidation verification): 2 days.
- Release notes: 1 day.

**Total:** ~5–6 weeks calendar time (assuming 1–2 dev weeks/person).

---

## 5. Success criteria

- ✅ CONSOLIDATE workflow identifies and merges near-duplicates; originals become read-only pointers.
- ✅ PERSEPHONE archival moves cold-set to S3 (or local disk); restore-on-demand works end-to-end.
- ✅ APOLLO S-IVB phases 3–4 complete dream-state mutation model.
- ✅ Compression hot-paths serving dense variants to federation + session replay + MCP.
- ✅ Design paper published in `docs/MEMORY_ARCHITECTURE.md`; covers all major subsystems.
- ✅ PANTHEON donations (Hermes + Continue) merged or documented as PRs.
- ✅ Live PYTHIA production: archival job runs nightly without incidents; archival volume > 100k memories.

---

## 6. Shipping readiness

v3.6 GA gate:
- [ ] CONSOLIDATE + archival integration tests against real MNEMOS instance.
- [ ] Federation round-trip: archive on system A, peer system B sees stub, restore via federation pull.
- [ ] Design paper peer review (external: 1–2 reviewers from OSS community).
- [ ] PERSEPHONE S3 restore validated on operator's prod infrastructure (or PYTHIA test).
- [ ] All mutation paths (CONSOLIDATE, EXTRACT, ARCHIVE) audit-logged and queryable via `/v1/audit`.
- [ ] Compression hot-paths benchmarked: bytes-on-wire reduction, latency impact, LLM token savings.
- [ ] Client donations (Hermes + Continue) either merged upstream or documented for operator adoption.

---

## 7. Post-v3.6 preview

Historical v3.6 framing expected this work to unlock the v4.0 sprint. Actual
v4.0 shipped the structural pieces directly:

- **API/package consolidation:** Shipped in v4.0 as the coherent `mnemos/` package.
- **MCP consolidation:** Memory tools now live under `mnemos/mcp/tools/`; IRIS discovery remains deferred.
- **Horizontal scaling:** Shipped in v4.0 with Redis-backed breaker/rate-limit/concurrency state.
- **GPU expansion / KRONOS:** Deferred.
- **MCP-MD v1.0 stabilization:** Deferred to v5.0+ foundation-tier work.

---

## 8. Cross-references

- **PANTHEON + IRIS:** Deferred from the historical v3.5 charter; must ship before the second-wave adoption work described here can be treated as executable.
- **IRIS adoption strategy:** Reference implementations (zeroclaw, OpenClaw) precede second-wave adoption by Hermes, Continue, AutoGPT, CrewAI, and langchain.
- **APOLLO program:** Phases 1–4 complete by end of v3.6 (shipped v3.2–v3.6).
- **APOLLO S-IVB dream-state:** Foundation in v3.3, slice 2 in v3.3 (parallel), phases 3–4 in v3.6.
- **PERSEPHONE archival:** Planned for v3.6; no v3.5.0 archival foundation shipped beyond recall tracking already present from v3.3.
- **Design papers:** Existing `PANTHEON.md`, new `IRIS_DISCOVERY.md`, `DREAM_STATE_DESIGN.md`, new `MEMORY_ARCHITECTURE.md` (v3.6).
- **v4.0 actual:** Modularization, SQLite profile, single-binary distribution,
  multi-worker Redis support, and architectural enforcement shipped; GPU
  scaling, broad surface integrations, and IRIS expansion moved later.

---

---

## Appendix: Greek pantheon subsystem table (v3.6 snapshot)

| Subsystem | Greek | Role | v3.6 status |
|---|---|---|---|
| MNEMOS | titaness of memory | core memory store | ✅ core |
| APOLLO | sun god / oracle | convergent compression (S-IC, S-II, S-IVB) | ✅ complete |
| CHARON | ferryman | cross-system portability | ✅ v0.2 |
| GRAEAE | gray sisters | multi-LLM consensus | ✅ core |
| PANTHEON | temple of all gods | unified LLM gateway | 🔵 prerequisite / planned |
| IRIS | messenger of the gods | MCP discovery layer + capability-based selection | 🔵 prerequisite / planned |
| PERSEPHONE | queen of the underworld | archival subsystem | 🔵 planned v3.6 |

*Charter locked 2026-04-25. Changes via memo + MNEMOS memory update.*

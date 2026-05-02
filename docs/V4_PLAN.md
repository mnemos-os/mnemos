# MNEMOS v4.0 Plan — ETLANTIS Universe Consolidation + GPU Stack

**STATUS: SHIPPED v4.0.0 on 2026-04-29.**
**Position in roadmap:** Historical v4 planning record. Tracks 5, 5b, and 8
shipped in v4.0.0; the body is preserved as planning context, not current
commitment.
**Theme:** *Structural refactoring + multi-backend persistence + horizontal scaling.*

## 0. Shipped / Deferred Summary

Shipped in v4.0.0:

- ✅ Track 5: API/package consolidation into the coherent `mnemos/` package,
  with 7 import-linter contracts enforcing architecture.
- ✅ Track 5b: persistence abstraction, Postgres + SQLite backends, `server` /
  `edge` / `dev` profiles, and PyInstaller single-binary distribution.
- ✅ Track 8: Redis-backed horizontal scaling primitives and removal of the
  production `workers=1` pin.
- ✅ Unified `mnemos` CLI and Pydantic Settings singleton.

Deferred / reframed after the 2026-04-29 v4.0 ship decision:

- 🔵 Track 3 IRIS operations discovery: v4.1/v5.0 timeframe.
- 🔵 Track 6 connectors gallery expansion: v4.1.
- 🔵 Track 7 MCP-MD LF AI & Data standardization: v5.0+ foundation-tier work.
- 🔵 KRONOS/GPU-stack integration, hosted MNEMOS Cloud, Rust rewrites, web UX,
  and mobile clients: v4.1/v5.0+ depending on product pressure. Rust rewrites
  are explicitly deferred per user direction on 2026-04-29.

---

## 1. Headline Pitch

MNEMOS v4.0 is the **infrastructure maturity + federation release**. It consolidates two years of incremental feature work into a legible, modular, horizontally scalable architecture. v4.0 is when **MNEMOS becomes the authoritative memory layer of the ETLANTIS Universe** — a coherent stack of repos (zeroclaw, zterm, MNEMOS, nclawzero, meta-nclawzero, pi-gen-nclawzero) united by memory sharing, cost-aware routing, and GPU-efficient agentic patterns.

The release spans three coupled work streams:

1. **API consolidation** — unify security, pooling, ID-derivation patterns scattered across 9 handler files into reusable modules.
2. **GPU stack integration** — deploy KRONOS (Tesseract time-series) on CERBERUS/TYPHON; measure anomaly detection + forecasting ROI; wire into the PANTHEON audit log.
3. **MCP + surface integrations** — bring MNEMOS memory to Claude Code, Cursor, Continue, ChatGPT, Gemini via native MCP or bridged HTTP.

Post-v4.0, MNEMOS is production-ready for enterprise deployment; v5.0 pivots to foundation-tier visibility work + Rust ports.

---

## 2. Track 5: API consolidation pass — SHIPPED in v4.0.0

### 2.1 `api/security.py` — unified authorization

**Current state:** Security checks scattered across 9 handler files.
- `api/handlers/dag.py:28` — `is_root()`, `scope_owner()` defined inline.
- `api/handlers/kg.py:45` — `assert_caller_owner_match()` defined inline.
- `api/handlers/federation.py:120` — separate federation-scoped checks.
- `api/handlers/portability.py:80` — import checks spread across 3 functions.
- `api/handlers/memories.py:60` — similar pattern.
- (Repeated in entities.py, journal.py, narrate.py, sessions.py, state.py)

**What ships:**
```python
# api/security.py
def is_root(bearer_token: str) -> bool:
    """Check if caller is root (master token)."""
    
def scope_owner(bearer_token: str) -> UUID:
    """Extract owner_id from bearer token."""
    
def assert_caller_owner_match(caller_owner_id: UUID, resource_owner_id: UUID, reason: str = None) -> None:
    """Raise 403 if mismatch."""
    
def assert_namespace_access(caller: Identity, requested_namespace: str) -> None:
    """Raise 403 if tenant not authorized."""
    
def assert_root_or_owner(bearer_token: str, resource_owner_id: UUID) -> bool:
    """Return True if caller is root OR owner; raise 403 else."""
    
class SecurityPolicy:
    """Encapsulate tenant-level policy (cost cap, model allowlist, etc.)."""
    @staticmethod
    def enforce_usage_tier(tier: str, allowed_tiers: list[str]) -> None:
        ...
```

**Refactoring:** Replace all 9 inline implementations with imports from `api/security.py`.

**Affected files:**
- New: `api/security.py`
- Modified: `api/handlers/{dag,entities,kg,journal,memories,narrate,portability,sessions,state}.py` (9 files)

**Effort:** 2 days (write module, run tests, refactor each handler).

### 2.2 `api/pool.py` — unified database pooling

**Current state:** asyncpg pool initialization + transactional context managers duplicated.
- `api/models.py:pool()` — single pool factory (OK).
- Each handler re-implements `async with pool.acquire()` + transaction setup.
- `api/lifecycle.py:_schedule_background()` has custom connection acquisition.
- Retry logic on connection failures differs per caller.

**What ships:**
```python
# api/pool.py
class PoolManager:
    """Singleton wrapper around asyncpg pool with sensible defaults."""
    
    async def transactional(self, isolation: str = "serializable") -> AsyncGenerator:
        """Context manager: acquire conn, start tx, commit or rollback."""
        
    async def query(self, sql: str, *args) -> list[Record]:
        """Execute query; auto-retry on transient failures."""
        
    async def execute(self, sql: str, *args) -> int:
        """Execute non-query; auto-retry."""
        
    async def call(self, func: Callable, *args, isolation: str = "serializable") -> Any:
        """Run arbitrary async func inside tx."""
```

**Benefit:** Consistent timeout handling, retry policy, isolation levels.

**Affected files:**
- New: `api/pool.py`
- Modified: `api/models.py` (delegate to PoolManager), all 9 handler files (use PoolManager)

**Effort:** 2 days.

### 2.3 `api/ids.py` — ID derivation + validation

**Current state:** UUID parsing + caller-scoped ID derivation scattered.
- `api/models.py:caller_scoped_id()` — deterministic id from owner_id + content hash.
- Each handler re-implements the pattern slightly differently.
- KG triple IDs, memory_versions IDs, etc. all follow similar logic.

**What ships:**
```python
# api/ids.py
def parse_uuid_or_raise(s: str) -> UUID:
    """Parse string as UUID; raise 400 if invalid."""
    
def caller_scoped_id(owner_id: UUID, salt: str, extra: str = "") -> UUID:
    """Derive deterministic UUID from owner_id + salt + optional extra."""
    
def memory_id_from_content(owner_id: UUID, content: str, verbatim: str = "") -> UUID:
    """Deterministic memory_id from owner + content hash."""
    
def kg_triple_id_from_edges(owner_id: UUID, subject: str, predicate: str, obj: str) -> UUID:
    """Deterministic KG triple id."""
    
class IDNamespace:
    """Per-subsystem namespace for derived IDs (memories, kg_triples, dreams, etc.)."""
    MEMORY = "mem"
    KG_TRIPLE = "kgt"
    DREAM = "drm"
    VERSION = "ver"
    COMPRESSION_MANIFEST = "cpm"
```

**Benefit:** Uniform ID derivation; easier to audit determinism across subsystems.

**Affected files:**
- New: `api/ids.py`
- Modified: `api/models.py`, all handler files

**Effort:** 1–2 days.

### 2.4 Module-level CI enforcement

**Tool:** `import-linter` in `pyproject.toml`.

**Config (example):**
```toml
[tool.import-linter]
contracts = [
    { type = "forbidden", name = "memories_cannot_import_compression", modules = ["api.handlers.memories"], forbidden = ["compression.*"], allow_indirect = false },
    { type = "independence", name = "federation_and_compression_independent", modules = ["api.federation", "compression.*"], },
]
```

**Effect:** CI fails if a handler imports from a different subsystem's internal API (enforce boundaries).

**Effort:** 1 day.

---

## 3. Track 7: MCP-MD v1.0 stabilization + Linux Foundation AI & Data proposal track — DEFERRED to v5.0+

**Strategic context:** This assumed MCP-MD (MCP Model Discovery) and IRIS would
ship before v4.0. They did not. Treat this section as a v5.0+ foundation-tier
standardization record.

**v4.0 deliverables:**

- **MCP-MD v1.0 specification locked:** All breaking changes resolved; interface is now stable (semantic versioning applies; breaking changes require v2).
- **Conformance test suite:** `tests/mcp_md_conformance.py` — other implementers can validate their MCP-MD implementations against a canonical suite.
- **Formal proposal to LF AI & Data:** Submitted to the working group with evidence of:
  - Multi-implementer adoption (MNEMOS + zeroclaw + OpenClaw + at least one external partner).
  - Specification quality review (peer review by senior OSS infrastructure practitioners before proposal).
  - Use-case breadth (model discovery + operations discovery across multiple subsystems).

**Proposal process:**

The proposal follows LF's standard Sandbox project intake — no expedited track, no special access, no bypass. Merit of the protocol and demonstrated adoption are the only drivers. The process typically involves:
1. Initial review by LF AI & Data TAC (Technical Advisory Committee).
2. Community feedback period (4–6 weeks).
3. TAC vote for Sandbox incubation.
4. Post-acceptance: MNEMOS and spec may remain separate repos (MNEMOS continues under `github.com/perlowja`, spec lives in LF-hosted neutral infrastructure).

**Explicit constraints:**

- We do not seek or expect expedited treatment.
- Relationships with senior LF practitioners may inform navigation of the formal process (e.g., understanding which TAC member chairs the relevant domain, common pitfalls in prior proposals), but no shortcuts or special consideration are sought or appropriate.
- Success is entirely merit-driven: the protocol must be genuinely valuable and adoption signals must be real.

**Outcome:** If accepted, MCP-MD becomes a foundation-tier standard for capability-based discovery in agentic systems. MNEMOS remains the production reference implementation; other projects can implement the spec under their own timelines.

---

## 3. Track 3: IRIS expansion to full ETLANTIS operations discovery — DEFERRED to v4.1/v5.0

**Strategic context:** Once IRIS exists for model discovery (capability-based
selection of PANTHEON models), later releases can generalize IRIS to expose
operations beyond models. This did not ship in v4.0.

### 3.1 Extend IRIS tools + resources beyond models

**New tools:**

1. `find_compression_engine(constraints: {compression_ratio_min: float, latency_p99_ms_max: int, cost_per_gb: float})` → returns ranked APOLLO compression engines (judge-verified quality scoring included).
2. `find_dream_generator(constraints: {ideation_style: str, synthesis_confidence_min: float})` → returns available APOLLO S-IVB dream generators.
3. `find_anomaly_detector(constraints: {metric_type: str, model_family: str, latency_ms_max: int})` → returns available KRONOS anomaly detectors.
4. `find_forecaster(constraints: {metric: str, horizon_hours: int, accuracy_metric: str})` → returns forecasting models + accuracy estimates.
5. `get_operation_health(operation_type: str, operation_name: str)` → health rollup across all ETLANTIS subsystems.

**New resources:**

1. `iris://operations` → catalog of all available ETLANTIS operations (models, compressors, dreamers, anomaly detectors, forecasters).
2. `iris://operations/performance-snapshot` → latency + cost + accuracy metrics across all subsystems.
3. `iris://operations/recommendations/{task}` → task-typed recommendations spanning multiple operation families (e.g., "for cost-optimized anomaly detection" → recommends specific detector + fallback).

**Entry points:** `api/iris/discovery.py` (new: operations discovery beyond models), `api/iris/health.py` (unified health aggregation), `api/iris/server.py` (updated MCP tool definitions).

**Effort:** 4–5 days (extend discovery logic to all subsystems; aggregate health from KRONOS, APOLLO S-IVB dream-state, APOLLO compression metrics tables).

### 3.2 ETLANTIS operations discovery integration

**Pattern:** All ETLANTIS subsystems (APOLLO compression worker, APOLLO S-IVB dream worker, KRONOS forecaster, archival worker) expose telemetry to a shared operations registry.

**New table: `etlantis_operations_log(id, timestamp, operation_type, operation_name, tenant_id, input_size, output_size, latency_ms, cost_usd, quality_score, status)`**

This is the single source of truth for IRIS's operations discovery. Each subsystem writes to it on task completion.

**Affected files:** All worker modules (`api/handlers/morpheus.py`, `compression/apollo.py`, future `api/kronos/`, `api/archival.py`).

---

## 3.3 SQLite "lite" profile (persistence abstraction first) — SHIPPED in v4.0.0

### 3.1 Persistence abstraction layer

**Current state:** All SQL is Postgres-specific (pgvector, jsonb, generated columns, partial indexes, `<=>` operators).

**What ships:**
```
src/mnemos/persistence/
├── abstract.py        # Interface: MemoryStore, KGStore, CompressionStore, etc.
├── postgres.py        # Postgres implementation
└── sqlite.py          # SQLite implementation (new)
```

**Example interface:**
```python
class MemoryStore(ABC):
    @abstractmethod
    async def create(self, memory: Memory) -> Memory:
        ...
    
    @abstractmethod
    async def search(self, query: str, limit: int, owner_id: UUID) -> list[SearchResult]:
        """Vector search via `<=>` on Postgres, custom scoring on SQLite."""
```

**Postgres impl:** Thin wrapper over existing code (minimal change).

**SQLite impl:**
- Replace pgvector with `sqlite-vec` (pure SQLite, same embedding interface).
- Replace `tsvector` with FTS5.
- Replace `jsonb` with JSON columns (JSON1 support in SQLite 3.9+).
- Replace generated columns with triggers.
- Replace partial indexes with equivalent FTS5 scopes.

**Benefit:** MNEMOS runs on a laptop (single SQLite binary) or scales to petabyte Postgres+pgvector+GPU cluster. Same API everywhere.

**Affected files:**
- New: `src/mnemos/persistence/abstract.py`, `src/mnemos/persistence/sqlite.py`
- Modified: `src/mnemos/persistence/postgres.py` (refactor existing code into impl), `api/models.py` (use abstract interface)
- All handler files (no change; they use the abstract interface)

**Effort:** 6–8 days (Postgres refactor, SQLite impl, dialect translation, testing).

### 3.2 Migration parity

**Current state:** `db/migrations/` folder has ~20 Postgres SQL files.

**What ships:**
```
db/
├── migrations/
│   ├── postgres/
│   │   ├── 001_initial.sql
│   │   └── ...
│   └── sqlite/
│       ├── 001_initial.sql  (equivalent, different SQL dialect)
│       └── ...
```

**Planned automation:** `tools/migrate_schema.py --dialect=sqlite --source-version=3.5.x` generates SQLite migrations from canonical Postgres once that future tool exists.

**Effort:** 2–3 days (migrate 20 files, validate equivalence).

### 3.3 Single-binary build (optional; depends on adoption)

**Once persistence abstraction + SQLite impl ship, operators can:**
```bash
pip install mnemos-os[sqlite,minimal]
pyinstaller --onefile mnemos-cli.spec
→ mnemos-cli (single 80MB executable, SQLite + MNEMOS + MCP server)
```

**Effort:** 1 day (Dockerfile + pyinstaller config).

---

## 4. Track 6: Surface integrations + MCP consolidation — PARTIAL, remainder v4.1

### 4.1 MCP consolidation (unified tool source + IRIS integration)

**Current state:**
- `mcp_server.py` and `mcp_http_server.py` — MNEMOS stdio and HTTP/SSE MCP
  transports sharing the same 18-tool registry.
- `api/mcp_tools.py` — current canonical MNEMOS MCP tool registry.
- `api/iris/server.py` — planned separate MCP server for IRIS once IRIS exists
  (model + operations discovery).
- Drift risk: future IRIS work could reintroduce multiple MCP servers and
  separate tool implementations if it is not consolidated deliberately.

**Strategic decision:** Instead of two separate MCP servers (MNEMOS + IRIS), consolidate into **one canonical MCP server** with two tool families.

**What ships:**
- Single source of truth: `api/mcp/tools.py` (unified MNEMOS + IRIS tool implementations).
- New: `api/iris/mcp_tools.py` → merged into `api/mcp/tools.py`.
- `mcp_server.py` → single entry point serving all tools (memory operations + discovery).
- CI test: every MCP tool has a corresponding HTTP endpoint; `test_mcp_http_parity.py` runs the same payloads against both.
- `pantheon-iris` separate MCP server → **deprecated**; users point to unified `pantheon-mnemos` server instead.

**Tool organization in unified server:**

```
[tool] Memory operations (original MNEMOS tools)
  - memories_search
  - memories_create
  - memories_retrieve
  - memories_delete
  - kg_search
  - kg_create
  - sessions_start
  - sessions_retrieve
  - audit_query

[tool] Model + Operations discovery (IRIS tools)
  - find_model
  - get_model_health
  - find_compression_engine
  - find_dream_generator
  - find_anomaly_detector
  - find_forecaster
  - get_operation_health

[resource] Model catalog
  - iris://models
  - iris://operations
  - iris://operations/performance-snapshot

[resource] Recommendations
  - iris://models/recommendations/{task}
  - iris://operations/recommendations/{task}
```

**Benefit:** Agents connect once and get both memory operations + discovery. Simpler config; reduced MCP server proliferation.

**Affected files:**
- Refactor: `api/mcp_tools.py` → `api/mcp/tools.py`
- Merge: `api/iris/mcp_tools.py` → `api/mcp/tools.py`
- Simplify: `mcp_server.py` → delegate to `api/mcp/tools.py`
- Update: `api/iris/server.py` → no longer a separate MCP server; IRIS tools served from unified server
- New test: `tests/test_mcp_unified_parity.py` (memory ops + discovery in single tool family)

**Effort:** 3 days (consolidate + test parity).

### 4.2 Connectors gallery (`docs/connectors/`)

**Deliverable:** One Markdown per supported surface, with exact config snippets to paste.

**Surfaces:**

| Surface | MCP support | Deliverable |
|---|---|---|
| **Claude Code** | ✅ native | Doc: register `~/.claude.json` MCP entry. Smoke test: connect, list memories. |
| **Claude Desktop** | ✅ native | Doc: add to `~/Library/Application Support/Claude/claude_desktop_config.json`. |
| **Cursor** | ✅ native | Doc: `~/.cursor/mcp.json` entry. Smoke test: run `/memory list` in Cursor. |
| **Codex CLI** | ✅ native (0.125.0+) | Doc: `codex mcp add` recipe. Test via `codex exec --mcp=mnemos`. |
| **Continue / Cline / Aider** | ✅ native | One-liner MCP config snippets; test via live IDE. |
| **ChatGPT Pro (Developer Mode web)** | ✅ via SSE bridge | Custom connector; MNEMOS exposed over HTTP/SSE. Snippet: copy connector config. |
| **ChatGPT consumer (free/Plus)** | ❌ no MCP | OpenAPI manifest + Custom GPT calling REST API. Fallback. |
| **Gemini / Code Assist** | ❌ no MCP | `mcp-to-gemini-functions` bridge (new package). Transform MCP tools to Gemini JSON schema. |
| **OpenWebUI / LM Studio / Ollama** | ⚠️ partial | OpenAI-compat tool-call path. Document REST-direct fallback. |

**Deliverables per surface:**
1. **Config snippet doc** (`docs/connectors/<surface>.md`) — copy-paste ready.
2. **Smoke test** (if automatable) — `tests/test_connectors_<surface>.py`.
3. **Troubleshooting guide** — common issues + remediation.

**Implementation:**

| Item | Effort | Notes |
|---|---|---|
| Claude Code, Desktop, Cursor, Codex | 1 day | Already working; document + smoke tests. |
| Continue, Cline, Aider | 1 day | MCP already supported; verify + document. |
| ChatGPT Developer Mode SSE bridge | 2 days | New FastAPI endpoint `/mcp/sse`; stream MCP events as SSE. |
| ChatGPT Custom GPT (consumer) | 1 day | Generate `mnemos-openapi.json`; document Custom GPT manifest. |
| Gemini functions bridge | 2 days | New package: `mnemos-bridges/gemini/` — translate MCP schemas to Google functions format. |
| Connectors gallery docs | 2 days | Write 8–10 Markdown files. |

**New artifacts:**
- `mnemos-openapi.json` (auto-generated from FastAPI, published as CI artifact).
- `docs/connectors/<surface>.md` (8–10 files).
- `api/mcp/sse.py` (new; SSE event streaming for ChatGPT Developer Mode).
- `mnemos-bridges/gemini/` (new package; transforms MCP tool definitions to Gemini-compatible JSON).
- `mnemos-bridges/openai-actions/` (new; Custom GPT manifest + OAuth scaffold).

**Effort:** 7–10 days total (depends on surface complexity).

### 4.3 Observability dashboards (deferred to v4.1)

**Why deferred:** observability is important but not on the critical path for v4.0 GA. v4.0 ships the infrastructure; v4.1 adds Prometheus metrics + Grafana dashboards.

---

## 5. GPU stack: KRONOS + Tesseract integration (now integrated with IRIS operations discovery)

### 5.1 Tesseract deployment architecture

**KRONOS purpose:** Time-series anomaly detection + forecasting for memory usage, retrieval patterns, embedding quality.

**Tesseract choice:** NVIDIA Tesseract NIM (Normalized Inference Module) — production-grade time-series models.

**Models:**
- `nv-tesseract:ad-diffusion-1.1.0` (anomaly detection via diffusion) — CPU-capable, 46s per 45-row window on CERBERUS.
- `nv-tesseract:forecasting-1.0.1` (univariate forecasting) — GPU-recommended, 16GB VRAM on TYPHON.

**Deployment:**
- CERBERUS: AD-Diffusion NIM in Triton container (shares vLLM inference port or separate Triton instance).
- TYPHON: Forecasting NIM, solo deploy (8GB + head room; requires Tensor RT 10 minimum).

**Cost:** ~9 NIMs total across the fleet; ~$900–1200/year at current NVIDIA licensing.

### 5.2 MNEMOS audit log → KRONOS feed (with IRIS integration)

**Pattern:**
- Every memory operation (create, update, retrieval) records to the planned v4 operational audit log. v3.5.x already has GRAEAE hash-chain audit, webhook delivery audit rows, compression contest audit, and version DAG snapshots, but not one generic `audit_log` table for every memory operation.
- Every APOLLO/APOLLO S-IVB dream/archival operation records to `etlantis_operations_log` (v4.0, see §3.2).
- KRONOS worker subscribes to both via NATS JetStream (or polls periodically; cheaper).
- Per-tenant windows: `memory_creates_per_minute`, `memory_retrievals_per_minute`, `embedding_quality_score`, `compression_ratio_mean`, `dream_generation_cost_usd`, etc.
- AD-Diffusion scores windows; if anomaly detected → write to `anomaly_log` + alert operator + update IRIS health state.

**IRIS integration:**
- Anomalies automatically reflected in `iris://operations/performance-snapshot` (operators query IRIS to see degradation).
- `get_operation_health(operation_type, name)` checks anomaly status; if anomalous, flags as `health=degraded`.
- Agents using IRIS discovery adapt: if APOLLO compressor is flagged as degraded, `find_compression_engine()` deprioritizes it.

**Alerting:**
- Operator sets thresholds: "alert if > 3σ deviation from baseline."
- Anomalies bubble up via `/v1/admin/anomalies?tenant_id=...&since=ISO8601`.
- Webhook optional: `POST https://operator-alerting-system/webhook` with anomaly details + IRIS health state.

**Tables:**
- `anomaly_log(id, timestamp, tenant_id, window_start, anomaly_type, anomaly_score, baseline, actual, operator_acknowledged_at, iris_health_state)` (new column).
- `forecast_log(id, timestamp, tenant_id, metric, horizon_hours, forecast_values, confidence_interval)`.

### 5.3 Forecasting integration

**Pattern:** Forecast operator's daily load (memory creates + retrieval volume) to pre-warm caches and schedule batch jobs.

**Example flow:**
1. Forecast model predicts peak at 15:00 UTC (+/- 2h confidence).
2. Operator schedules distillation batch for 14:30 UTC (run before peak).
3. Operator schedules archival scan for 02:00 UTC (off-peak).

**API:** `GET /v1/admin/forecast?tenant_id=...&metric=memory_creates&horizon_hours=24`.

**Effort:** 3–4 days (integrate Tesseract, hook up audit log, alerting webhooks).

### 5.4 GPU resource placement

**Before v4.0:** CERBERUS has 9.3 GB in use (Apollo Q6 5.1 GB, Apollo Q4 2.8 GB fallback, unused).

**v4.0 planning:**
- Drop Apollo Q4 fallback (systemd auto-restart on Q6 failure; Q4 was redundancy overkill).
- Add EmbedQA-1B NIM (2 GB, handles all embedding).
- Add RerankQA-1B NIM (2 GB, ranks search results).
- Add AD-Diffusion NIM (1 GB, anomaly detection).
- **CERBERUS budget:** ~5.1 (Q6) + 2 (EmbedQA) + 2 (RerankQA) + 1 (AD-Diff) = **10.1 GB** (0.1 GB over current 10GB, acceptable margin).

**TYPHON GPU (RTX 5060, 24 GB):**
- Forecasting NIM: 8 GB.
- Headroom for operator experiments: 16 GB free.
- No dedicated allocation; Forecasting is lower priority than agentic batch work.

**Monitoring:**
- Prometheus metrics: `nvidia_smi` polling every 30s → `gpu_memory_used_mb`, `gpu_utilization_percent`, `gpu_temperature_c` per device.
- Grafana dashboard: GPU health + memory headroom.

**Effort:** 2 days (Helm charts, monitoring integration).

---

## 6. Security + Compliance

### 6.1 API consolidation security audit

**Scope:** Review all 9 handler files for auth gaps now that security logic is centralized in `api/security.py`.

**Checklist:**
- [ ] Every POST/PATCH/DELETE checks ownership or root.
- [ ] Every federation operation logs caller identity.
- [ ] Every mutation (create, update, delete) recorded in audit log.
- [ ] Cross-tenant data access impossible (no accidental queries on wrong tenant).

**Affected files:** All handler files (as part of consolidation).

**Effort:** 1 day (included in Track 5 consolidation).

### 6.2 Bearer token rotation + revocation

**Current state:** Tokens never expire, no revocation path.

**What ships:**
- Token versioning: tokens include `version` field. Operator can bump global version → all old tokens invalid.
- Per-token revocation: `POST /v1/admin/revoke-token?token=...` (root only). Invalidated immediately.
- Token rotation API: `POST /v1/admin/rotate-bearer-token?current_token=...` → returns new token (backward-compatible 48h grace period on old).

**Table:**
- `bearer_token_versions(id, version_number, created_at, revoked_at, revoke_reason)`.
- `revoked_tokens(token_hash, revoked_at, reason)` (for immediate revocation lookup).

**Effort:** 2 days.

### 6.3 MCP tool surface security audit

**Scope:** 13+ MCP tools, each a potential attack surface. Formal audit + fixes.

**Items:**
- [ ] Tool input validation (no injection attacks via memory_id, KG triple edges, etc.).
- [ ] Identity propagation (every tool respects caller's owner_id; no cross-tenant leaks).
- [ ] Quota enforcement (tool calls don't enable DOS-via-bulk-operations; rate limits apply).
- [ ] Audit trail (every tool call logged with caller identity + parameters).

**Audit methodology:** Codex adversarial review (Codex probes each tool for "can I call this and harm another tenant's data?").

**Effort:** 3–4 days (audit + fixes).

### 6.4 GDPR right-to-be-forgotten path

**Current state:** `memory_versions` survives memory deletion by design (for audit trail). Compliance issue for GDPR.

**What ships:**
- `POST /v1/admin/wipe-tenant?tenant_id=...` (root only, requires confirmation).
- Wipe process: soft-delete all memories + versions + KG triples + audit entries for tenant.
- Retain: archival references (prove deletion happened) + anonymized counts (operational metrics).
- Restore: 30-day grace period (can restore from backup); after 30 days, permanent deletion.

**Table:** `deletion_requests(id, tenant_id, requested_at, requested_by, confirmed_at, deleted_at, restore_by)`.

**Effort:** 2 days.

---

## 7. MCP tool scope security audit (deep dive)

**Tools (from `api/mcp_tools.py`):**

| Tool | Risk | Mitigation |
|---|---|---|
| `memories_search` | Info disclosure (peer tenant data) | Enforce owner_id filter; return 403 on cross-tenant. |
| `memories_create` | Quota DOS | Rate-limit per tenant and record decisions in the planned v4 operational audit log. |
| `memories_retrieve` | Info disclosure | Enforce owner_id; 404 on other tenant's memory. |
| `memories_delete` | Data loss | Require confirmation token; soft-delete only. |
| `kg_search` | Info disclosure | Enforce owner_id filter. |
| `kg_create` | Quota DOS | Rate-limit; validate triple format. |
| `sessions_start` | Quota DOS | Rate-limit sessions per tenant. |
| `sessions_retrieve` | Info disclosure | Enforce owner_id. |
| `morpheus_recall_memory` | Logic loop | Add cycle detection in dream generation. |
| `federation_list_peers` | Info disclosure | Return only peers authorized for this tenant. |
| `federation_pull` | Data loss / integrity | Validate peer identity; checksums on received data. |
| `audit_query` | Info disclosure | Enforce scope (only audit entries for caller's tenant). |
| `admin_*` | Complete system compromise | These MUST require root token + confirmation. |

**Audit work:** Codex review of each tool's parameter validation + auth check logic.

**Effort:** 2 days (audit + fixes).

---

## 8. Horizontal scaling - shipped (v4.0 C.1 + C.2)

### 8.1 GRAEAE breaker + rate-limit state → Redis

**Shipped state:** GRAEAE's circuit breaker, rate limiter, and concurrency guard
have Redis-backed implementations with in-process fallback. The `workers=1`
operational pin has been removed; multi-worker startup warns when Redis is not
configured.

**What ships:**
- `mnemos/core/resilience.py:RedisCircuitBreaker` - circuit breaker backed by Redis.
- `mnemos/core/resilience.py:RedisRateLimiter` - token bucket backed by Redis.
- `mnemos/core/resilience.py:RedisConcurrencyLimiter` - Redis slot leases for shared concurrency limits.
- MNEMOS startup checks: if Redis available, use it; else fall back to in-process and warn when workers > 1.

**Benefit:** Deploy multiple MNEMOS API workers; they coordinate breaker + rate-limit state via Redis.

**Affected files:**
- Modified: `mnemos/core/resilience.py` (Redis-backed primitives).
- Modified: `mnemos/core/lifecycle.py` (Redis client ownership + multi-worker warning).
- Modified: `mnemos/cli/main.py`, `mnemos/api/main.py` - remove `workers=1` pin.
- New: `docs/SCALING.md` - Redis-backed multi-worker deployment doctrine.

**Effort:** shipped across v4.0 C.1 + C.2.

### 8.2 Documented scaling playbooks

**Deliverables:**
- `docs/SCALING.md` - how to run MNEMOS on Kubernetes / docker-compose at scale.
- Example: 3x API workers + 1x distillation worker + 1x morpheus worker + Postgres + Redis on k8s.
- Helm values file: `deploy/mnemos-k8s/values.yaml`.

**Effort:** 2 days.

---

## 9. Architectural gaps roundup (carried from v3.x audits)

All items from the Audit Remediation Log in ROADMAP.md that remain open post-v3.6:

- ✅ SET LOCAL/SET GUC audit — validate no dangerous GUCs set per-session (audit in v3.5; shipping in v4.0 is reinforcement).
- ✅ Migration idempotency test on populated DB — run all migrations against real data; validate no silent failures.
- ✅ Response-size memory pressure (streaming responses) — document how streaming helps; add backpressure metrics.
- ✅ TIMESTAMP/TIMESTAMPTZ audit outside CHARON — all internal timestamps must be TIMESTAMPTZ UTC; audit + fix anywhere else.
- ✅ FK cascade rule consistency — audit all FKs; ensure cascades are intentional (soft-deletes preferred).
- ✅ Cross-version restore runbook — document how to restore MNEMOS from backup at version N and upgrade to version M.
- ✅ Webhook delivery guarantees doc/test — MNEMOS sends webhooks on memory mutations; document retry policy + guarantee level.

**All 7 items ship as part of v4.0 integration testing + documentation.**

**Effort:** 2 days (audit + fixes + docs).

---

## 10. Documentation + Governance

### 10.1 SECURITY.md

**Scope:** How MNEMOS handles auth, data isolation, audit trails, key management.

**Sections:**
1. Auth model (bearer tokens, scopes, root vs tenant).
2. Data isolation (multi-tenancy guarantees).
3. Key management (provider keys in Vault, master key rotation).
4. Audit trail (operations logged, queryable, immutable).
5. Threat model (who we defend against, what we don't).
6. Compliance (GDPR, SOC 2, audit readiness).

**Effort:** 2 days.

### 10.2 GOVERNANCE.md

**Scope:** Project decision-making, release process, contributor roles.

**Sections:**
1. Maintainer roles (user/jperlow, committers, reviewers).
2. RFC process (how architectural decisions are made).
3. Release cadence (v3.x every ~2 months, v4.0 as breakpoint).
4. Contribution guidelines (code review, tests, docs).
5. Security vulnerability response (coordinated disclosure).

**Effort:** 1 day.

### 10.3 CONTRIBUTING.md update

**Changes:**
- Adding a provider to PANTHEON (3-step recipe).
- Adding a custom compression engine (implement `CompressionEngine` ABC).
- Adding a federation backend (implement `FederationAdapter` ABC).
- Testing checklist (unit + integration + operator scenario).

**Effort:** 1 day.

### 10.4 PROJECT_POSTURE.md

**Scope:** Clear statement of what MNEMOS is + isn't.

**Sections:**
1. **Is:** Memory infrastructure for agentic systems; compression + routing + federation.
2. **Isn't:** A chat application, a vector database (uses pgvector), a replacement for enterprise document stores.
3. **Design philosophy:** Boring proven tech (Postgres, NATS, FastAPI), focus on the novel layer (memory DAG + synthesis).
4. **Supported deployment:** Single-box SQLite, VPC Postgres, Kubernetes multi-worker, federation across regions.
5. **Post-v4.0 vision:** Foundation-tier visibility (scientific papers, talks), Rust ports (v5.0+), ETLANTIS Universe integration.

**Effort:** 1 day.

### 10.5 Compression architecture semantics doc

**Scope:** How compression decisions are made, judge-LLM scoring, cost-quality tradeoffs.

**Sections:**
1. Compression contest (multiple engines race; winner emerges).
2. Judge-LLM fidelity scoring (semantic equivalence, not byte-for-byte).
3. Cost model (tokens cost money; compression saves tokens).
4. Narration (dense form → prose for humans; dense form → LLM for agents).
5. Audit trail (every decision logged, queryable, explainable).

**Effort:** 1 day.

### 10.6 CHARON peer adapter trust boundary doc

**Scope:** What CHARON assumes about peer systems; what guarantees it gives.

**Sections:**
1. Peer identity (how we identify a peer MNEMOS instance).
2. Trust model (peers are cooperative; we don't defend against Byzantine peers).
3. Envelope signing (optional; not required for v4.0).
4. Namespace isolation (peer's data stays in peer's namespace).
5. Federation pull semantics (eventual consistency; may lag).

**Effort:** 1 day.

**Total docs effort:** 9 days.

---

## 11. Effort estimate (historical)

| Work stream | Effort (days) | Dependencies |
|---|---|---|
| Track 5: API consolidation | 7–8 | ✅ shipped in v4.0.0 |
| Track 3: IRIS operations discovery expansion | 4–5 | 🔵 deferred to v4.1/v5.0 |
| Track 5b: Persistence abstraction + SQLite | 8–10 | ✅ shipped in v4.0.0 |
| Track 6: MCP consolidation (unified MNEMOS + IRIS server) | 3 | 🔵 non-IRIS MCP surface shipped; IRIS consolidation deferred |
| Track 6b: Connectors gallery | 7–10 | 🔵 v4.1 |
| KRONOS / Tesseract GPU stack (with IRIS integration) | 5–6 | ✅ v0.1 CPU scaffold shipped; Tesseract/CUDA deferred to v0.2 |
| Security + compliance (token rotation, GDPR, audits) | 6–7 | Consolidation items first (so we know where security logic lives) |
| Horizontal scaling (Redis) | 3–4 | ✅ shipped in v4.0.0 |
| Docs + governance | 9 | All other items mostly done |

**Total:** ~55–70 days focused work. **Calendar:** 7–9 weeks (2 dev-weeks/person in parallel). **Team:** 1–2 engineers.

**Net vs pre-IRIS plan:** +4–5 days for IRIS expansion + operations discovery logging, +1 day for unified MCP consolidation (simpler than separate servers). Offset by reduced framework-donation work in v3.6 (config docs instead of code PRs).

---

## 12. v4.0.0 Success Criteria

- ✅ Codebase lives under coherent `mnemos/` package boundaries.
- ✅ Persistence abstraction works with Postgres and SQLite.
- ✅ `server`, `edge`, and `dev` profiles are selectable through settings or CLI.
- ✅ Single-binary artifacts build for the official v4.0 matrix.
- ✅ MCP memory tool surface remains unified across stdio and HTTP/SSE.
- ✅ Horizontal scaling primitives use Redis when configured; in-process fallback warns for multi-worker.
- ✅ Seven import-linter contracts enforce the package architecture.
- 🔵 Connector gallery, IRIS, KRONOS, GDPR wipe path, and foundation-standardization work are explicitly deferred.

---

## 13. Post-v4.0 vision (v5.0+)

**After v4.0 GA:**

1. **Foundation visibility work** — academic papers on MNEMOS memory architecture + APOLLO compression (target: NeurIPS 2027, ICML workshops).
2. **Sustained-quality upstream contributions** — small, high-value patches to MemPalace, Cognee, Graphiti, langgraph.
3. **Rust ports** — `mnemos-rs` and selected hot-path rewrites are deferred until the Python v4 architecture has production soak.
4. **ETLANTIS Universe federation demo** — zeroclaw + zterm + MNEMOS + nclawzero agents sharing memory across system boundaries.
5. **Foundation exploration** — once multi-implementer adoption is established, scope conversations with senior LF AI & Data practitioners about MNEMOS or MCP-MD as a potential LF-hosted project. No fast-track sought.

---

---

## Appendix: Greek pantheon subsystem table (v4.0 snapshot, full ETLANTIS unified)

| Subsystem | Greek | Role | v4.0 status |
|---|---|---|---|
| MNEMOS | titaness of memory | core memory store | ✅ production |
| APOLLO | sun god / oracle | convergent compression (S-IC, S-II, S-IVB) | ✅ complete + hot-paths |
| CHARON | ferryman | cross-system portability | ✅ v0.2 |
| GRAEAE | gray sisters | multi-LLM consensus | ✅ core |
| PANTHEON | temple of all gods | unified LLM gateway | 🔵 prerequisite / planned |
| IRIS | messenger of the gods | MCP discovery + capability-based selection (models + all ETLANTIS ops) | 🔵 deferred to v4.1/v5.0 |
| KRONOS | titan of time | time-series anomaly + forecasting | ✅ v0.1 CPU scaffold; GPU deferred |
| PERSEPHONE | queen of the underworld | archival subsystem | 🔵 deferred |

---

## 15. Cross-references

- **v3.5 shipped reality:** audit hardening, uniform tenancy, webhook retry/outbox discipline, MCP registry parity, faithful OpenAI compatibility, streaming-replication doctrine, compression cleanup, and documentation triage in v3.5.1. PANTHEON/IRIS and APOLLO EXTRACT did not ship in v3.5.x.
- **v3.6 charter:** CONSOLIDATE, PERSEPHONE, APOLLO S-IVB phases 3–4, IRIS second-wave adoption.
- **v4.0 shipped reality:** package/API consolidation, persistence abstraction,
  SQLite profile, single-binary distribution, multi-worker Redis support, and
  architectural enforcement. IRIS operations discovery, connector gallery
  expansion, MCP-MD LF work, and KRONOS integration moved beyond v4.0.
- **ROADMAP.md:** Full history + audit log.
- **Existing docs:** PANTHEON.md, IRIS_DISCOVERY.md (new v4.0), DREAM_STATE_DESIGN.md, MEMORY_EXPORT_FORMAT.md, SPECIFICATION.md.
- **ETLANTIS Universe repos:** zeroclaw, zterm, nclawzero, meta-nclawzero, pi-gen-nclawzero.

---

*Plan locked 2026-04-25. Changes via memo + MNEMOS memory update.*

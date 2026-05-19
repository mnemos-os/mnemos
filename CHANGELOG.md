# Changelog

All notable changes to MNEMOS are documented here.

## Pre-release history

- ✅ **Webhook subscriptions** — outbound notifications on memory write, consultation completion. HMAC-signed delivery, retry with exponential backoff.
- ✅ **OAuth/OIDC authentication** — browser-based login via Google, GitHub, Azure AD, or custom OIDC providers. Coexists with existing API-key auth.
- ✅ **Cross-instance memory federation** — pull-based peer sync with Bearer-authenticated peers. Federated memories stored locally with `federation_source` metadata, `fed:{peer}:{remote_id}` id prefix, and a background worker that respects per-peer sync intervals.
- ✅ **Plugin `CompressionEngine` ABC** — open extension point; operators register additional engines alongside the built-ins (APOLLO + ARTEMIS).
- ✅ **Competitive-selection compression contest** — every eligible engine runs per memory; highest composite_score wins; every loser recorded with its reject_reason. Scoring profile is operator-configurable (`balanced` | `quality_first` | `speed_first` | `custom`).
- ✅ **Persisted audit log** — three new tables (`memory_compression_queue`, `memory_compression_candidates`, `memory_compressed_variants`) with full history queryable via `GET /v1/memories/{id}/compression-manifests`.
- ✅ **GPU circuit breaker** — per-endpoint three-state breaker (CLOSED → OPEN → HALF_OPEN → CLOSED); gpu_required engines fast-fail during outages instead of piling requests onto a dead endpoint.
- ✅ **Admin enqueue endpoints** — `POST /admin/compression/enqueue` (specific memory IDs) and `POST /admin/compression/enqueue-all` (bulk with filters) for operators to drive the contest from the API layer.
- ✅ **Optional too-short content gate** — `MNEMOS_CONTEST_MIN_CONTENT_LENGTH` skips memories below a threshold before spending GPU time on content that can't be meaningfully compressed.
- ✅ **v2 versioning trigger bytea fix** — the `mnemos_version_snapshot()` trigger no longer crashes on memories containing backslash sequences (common in code, paths, regex, logs).
- ✅ **CHARON federation schema preflight** — peers exchange schema signatures before sync and return 409 on incompatible strict-mode pairings.
- ✅ **Dev↔prod MPF restore drill** — `docs/RESTORE-DRILL.md` is validated on the PYTHIA → PROTEUS path.
- ✅ **Slice 1: audit quick wins** (`a62a099`) — session history returns the most recent messages first with deterministic system-row pinning, and project URLs now point at `mnemos-os/mnemos`.
- ✅ **Slice 2: memory-read tenancy + DAG integrity** (`d42c475`) — shared memory read visibility, per-snapshot history visibility, same-memory DAG guards, race-safe branch creation, `MN001` to HTTP 409 reconciliation guidance, and a compose `postgres-upgrade` service for existing volumes.
- ✅ **Webhook retry state machine + leases + outbox discipline** — persisted leases, one-success-per-chain guards, repair worker separation, bulk-create parity, and terminal success trigger.
- ✅ **MCP unified registry** — stdio and HTTP/SSE expose the same 23 tools from `mnemos/mcp/tools/`, including CRUD, KG, DAG, bulk create, stats, deletion-request management, model recommendation, KRONOS observability, and the PANTHEON model facade.
- ✅ **Faithful OpenAI-compatible gateway** — propagated generation controls, OpenAI-format SSE, registry-honest model discovery, and explicit 400/404 responses when the selected provider cannot honor a requested feature.
- ✅ **Namespace-uniform tenancy** — state, journal, entities, sessions, consultations, webhooks, and memory read/history paths use the owner+namespace discipline.
- ✅ **PostgreSQL streaming-replication doctrine** — single-site HA uses Postgres primary/standby replication; MNEMOS federation is for remote or curated data flows.
- ✅ **Compression cleanup** — live compression is APOLLO + ARTEMIS through the contest worker; retired compatibility shims and vestigial session compression columns are gone.
- ✅ **GDPR right-to-be-forgotten** — deletion-request lifecycle (`requested → confirmed → soft_deleted → restored | hard_deleted | cancelled`) plus soft-delete worker (Phase B) and hard-delete worker (Phase C). 30-day restore window; trigger-suppressed hard delete preserves the audit chain.
- ✅ **MORPHEUS slices 3 + 4** — CONSOLIDATE phase merges near-duplicate clusters into a canonical with read-only pointers (`permission_mode=0o400`, `consolidated_into`); EXTRACT phase mines latent KG triples from prose `verbatim_content`. Both phases opt-in, namespace-scoped, rollbackable via `morpheus_run_id`.
- ✅ **PERSEPHONE archival subsystem** — cold-set rotation moves rarely-recalled memories into a zstd-compressed `memory_archive` table with stub-pointer in `memories`. Restore on demand. Federation-aware (peers see archive marker via the version trigger).
- ✅ **PANTHEON + IRIS unified LLM facade** — OpenAI-compat `/pantheon/v1/{models,chat/completions,embeddings,route/explain}`. Auto-populated catalog from GRAEAE muses; alias prefix resolver (`auto:reasoning`, `auto:cheap`, `auto:fast`, `consensus:<task>`); per-(user,session) caps on `consultation_only` tier; rolling-window adaptive routing. IRIS exposes `pantheon_list_models` + `pantheon_route_explain` MCP tools.
- ✅ **KRONOS v0.1** — recall-pattern anomaly detection (z-score over `recall_count` history), namespace drift detection, recall-load forecasting (EWMA), PERSEPHONE eligibility forecast. CPU-only via numpy; Tesseract GPU integration deferred to v5.1.
- ✅ **DAG wiring for compression derivations** — every successful compression contest persists a child row in `memory_versions` parented to the source memory's `branch='main'` HEAD on `branch='distilled'` or `branch='narrated'`; `change_type='compress'` extends the CHECK constraint; commit hash is content-derived.
- ✅ **NATS substrate v0.2** — bounded next slice. PANTHEON routing-log → `mnemos.pantheon.routing` opt-in publish; `pantheon_routing_audit` table fed by an optional consumer worker.
- ✅ **MCP §6.4 cross-tenant security gates** — uniform error-shape normalization across all 23 tools, parameter-shape audit log (no raw values), per-tool rate buckets, role + namespace validation in the dispatcher, root-bypass logged as warning, generic error messages from `_safe_path_*` helpers (no value echo).
- ✅ **Document-import retry-safety** — content-derived `import_chunk_key` prevents duplicate chunk insertion on retry; ON CONFLICT (key) DO UPDATE returns canonical row id.
- ✅ **Connector smoke gallery** — end-to-end smoke per surface (Claude Code, Cursor, Codex CLI, Continue, Cline, Claude Desktop, ChatGPT) with mechanically-validated JSON snippets.
- ✅ **Rust hot-path accelerator (mnemos_hot v0.2)** — Rust implementations of cosine, top_k, batch cosine, embedding parse, embedding L2-normalize, composite search re-rank, deterministic judge scoring, and SHA-256 batch hashing. All wired with MNEMOS_HOT_RS_ENABLED=1 opt-in plus identical Python fallback.
- ✅ **Coherent package layout** — production code now lives under `mnemos/` with `api/routes`, `core`, `db`, `domain`, `persistence`, `mcp`, `webhooks`, `workers`, `hooks`, `installer`, `tools`, and `cli` subpackages.
- ✅ **Persistence abstraction** — `PersistenceBackend` owns the contract; `PostgresBackend` uses asyncpg + pgvector + RLS + LISTEN/NOTIFY, and `SqliteBackend` uses aiosqlite + sqlite-vec + FTS5 + JSON1 + WAL.
- ✅ **Deployment profiles** — `server`, `edge`, and `dev` select safe defaults through `MNEMOS_PROFILE` or `mnemos serve --profile`.
- ✅ **Multi-worker support** — Redis-backed circuit breaker, rate limiter, and concurrency limiter coordinate API workers; in-process fallback remains for single-worker dev and edge installs.
- ✅ **Single-binary distribution** — PyInstaller artifacts for linux-x86_64, linux-aarch64, and macos-aarch64 bundle sqlite-vec and the migration chain.
- ✅ **Unified CLI** — `mnemos serve / install / worker / export / import / consult / health / version` replaces the old top-level Python entry points.
- ✅ **Architectural enforcement** — seven import-linter contracts keep API, domain, db, core, persistence, MCP, and webhook boundaries honest in CI.
- ✅ **GRAEAE mode validation** — routing modes plus `single`, `debate`, and `majority` are modeled as a `Literal`; unknown modes 422 instead of falling through.
- `bulk_create_memories` now runs through the backend transaction and webhook outbox surface, so it works on SQLite-backed edge profiles as well as Postgres-backed server profiles.
- The SQLite-backed `edge` profile intentionally exposes a narrower HTTP API: sessions, entities, state, and MORPHEUS telemetry routes return 503 because those surfaces still depend on server-profile Postgres SQL.
- MORPHEUS run and cluster endpoints are operator-only telemetry. They require root credentials because responses can include namespaces, configs, errors, and memory IDs across tenants.
- v5.0 still does not ship the separate web frontend, mobile clients, or hosted MNEMOS Cloud; those remain roadmap items.
- The PROTEUS barrage exposed long-tail latency under sustained 50-concurrent writes (p99 ~33s). Search and read paths held up well (search p99 ~300ms; reads p50 ~120ms). Tuning the worker / pool budget is a v5.1 target.
- PANTHEON v0.2 caps live in an in-process bucket; horizontal scaling needs a Redis-backed cap store (deferred to v5.1+).
- Web UX in the separate `mnemos-web` frontend repo
- Mobile clients: Android Termux hardening first, iOS native later
- Hosted MNEMOS Cloud and foundation-tier OSS standardization work (MCP-MD via LF AI & Data) in the v5.x+ frame
- Hatchet workflow-engine integration alongside the NATS substrate (deferred from v5.0)
- KRONOS Tesseract GPU integration (deferred from v5.0)

## [Unreleased]

### Fixed — Webhook deliveries limit cap + capabilities GIN index (#205)

Two LOW audit findings (`mem_1778221719390_8cb1ba`) bundled.

(a) `mnemos/api/routes/webhooks.py:list_deliveries` — the
`limit: int = 50` parameter had no caller-side bound. A caller
could pass `limit=10_000_000` and pull a multi-million-row
delivery dump in one round-trip. Capped at 200 with
`Query(50, ge=1, le=200)` matching adjacent listing endpoints
(`memories.py` uses `ge=1, le=500`; the webhook endpoint is the
operational shape with smaller pages).

(b) `model_registry.capabilities @> $3` containment queries (used
by `providers.py:106` for provider/model discovery) had no GIN
index. As the registry grows (provider syncs, model retirements)
this can degrade to seq-scan. Added
`migrations_v5_3_5_model_registry_capabilities_gin.sql`
(`CREATE INDEX IF NOT EXISTS ... USING GIN (capabilities)`),
registered in `mnemos/installer/db.py` canonical loader and in
both `docker-compose.yml` + `docker-compose.staging.yml`
(initdb mount + `postgres-upgrade` apply step) so existing
volumes get the index too. SQLite path is unaffected (no
TEXT[] column there).

Pinned by `tests/test_webhooks_limit_and_capabilities_gin.py`
(4 tests): the FastAPI `Query(...)` `ge`/`le` bounds, the
migration file exists with `CREATE INDEX ... USING GIN`,
registration in installer's canonical list, ordering after
`v5_3_4`. The pre-existing
`tests/test_migration_lists_sync.py` was updated to include
the new migration.

### Fixed — GC stale `(principal, tool)` keys in MCP rate-limit buckets (#204)

Audit LOW finding (`mem_1778221719390_8cb1ba`) at
`mnemos/mcp/tools/_security.py:16`: ``_TOOL_RATE_BUCKETS`` was a
``defaultdict(deque)`` that pruned timestamps inside each deque on
every touch but never dropped the (principal, tool) keys themselves.
A high-churn principal flow (CI matrix, rotating tokens) leaked
memory — small per-entry, monotonically growing for the lifetime
of the process.

Fix: amortized periodic sweep (`_gc_stale_buckets`) every 256
touches. Drops buckets whose newest timestamp is past the cutoff
(principal stopped calling) plus any empty buckets (a
``defaultdict`` lookup that created the entry but the touch raised
before the append). A hard cap at 4096 triggers a fallback
`_evict_oldest_buckets` pass that drops the oldest-by-last-
timestamp entries until the dict is at half-cap.

Pinned by `tests/test_mcp_rate_bucket_gc.py` (6 tests):
constants in sane range, sweep drops past-cutoff buckets, sweep
drops empty buckets, beyond-cap eviction drops the oldest by
last-timestamp, periodic sweep actually fires every Nth touch,
hot-path rate-limit semantics unchanged.

### Fixed — MCP principal context cache TTL + size bound (#203)

Audit MED finding (`mem_1778221719390_8cb1ba`) at
`mnemos/mcp/http.py`: `_principal_context_cache` was an unbounded
`dict[str, MCPUserContext]` that NEVER expired. Operator role /
namespace changes were hidden for the lifetime of the process,
and high-churn principal-id flow (token rotation, CI matrix)
could grow the cache without bound.

Fix: TTL = 300s + size cap = 1024. New `_principal_cache_get`
and `_principal_cache_set` helpers handle expiry on read,
half-oldest eviction on cap-overflow, and a `_monotonic` clock
indirection so tests can advance time without sleeping.

Backward compat: tests under
`tests/test_mcp_user_passthrough.py` and
`tests/test_mcp_nats_sse.py` historically did
`cache[key] = MCPUserContext(...)` (bare entry, not tuple). The
get helper treats bare entries as never-expires so those tests
keep passing without churn.

Pinned by `tests/test_mcp_principal_cache_ttl.py` (5 tests):
TTL/cap constants in sane range, set + within-TTL get returns
the context, past-TTL get evicts and returns None, beyond-cap
set evicts oldest and stays at-or-below cap, bare-context
backward compat still works.

### Fixed — Postgres `semantic_search` embedding-dim guard (#202)

Audit MED finding (`mem_1778221719390_8cb1ba`) at
`mnemos/persistence/postgres.py:semantic_search`: the path cast
arbitrary-length embeddings to `vector` without validating
against the configured dim. `SqliteMemoryRepository` already had
`_require_dim` for this exact case; the Postgres path was the
asymmetric gap.

Without the guard, an embedding-endpoint switch (e.g. operator
points `INFERENCE_EMBED_HOST` at a different model whose dim
doesn't match the schema-sized column) surfaces as a generic
asyncpg `DataError` from the `<=>` cast layer. With the guard,
the path raises a Python `ValueError` naming the actual vs
expected dim and the remediation steps (verify
`INFERENCE_EMBED_HOST` / restart with matching
`MNEMOS_EMBEDDING_DIM` / swap the embedding endpoint back).

Mirrors the SQLite-path `_require_dim` shape so MNEMOS surfaces
the same operator-facing message regardless of profile.
`PostgresBackend.__init__` now plumbs
`settings.database.embedding_dim` into the memory repo (with a
defensive fallback to None for stripped-down test settings).

Pinned by `tests/test_postgres_semantic_search_dim_validation.py`
(6 tests): no-op when unset, short/long vector raises with
operator-friendly message, exact-match passes, message names
remediation steps, source-level guard that the
`_require_dim` call is the first statement of `semantic_search`.

### Fixed — `BulkCreateRequest.memories` capped at 1000 (#201)

Audit MED finding (`mem_1778221719390_8cb1ba`):
`BulkCreateRequest.memories` had no `max_length` cap, unlike
newer hardened request fields. The `/v1/memories/bulk` handler
iterates the list with one transaction per memory (N+1 writes +
publishes), so an unbounded request can open thousands of round-
trips through dedup + insert + version trigger + webhook outbox.

Cap matched to the compression-enqueue admin pattern (1000 ids
max). Validation rejects over-cap requests at the Pydantic
boundary as 422 before any auth/RLS work happens. The deeper
fix — bulkifying into a single SQL statement — is tracked
separately because it changes partial-failure semantics across
the batch.

Pinned by `tests/test_bulk_create_max_length.py` (4 tests):
1001 items rejected, exactly 1000 accepted, 1/100 under cap
accepted, source-level guard that the literal `max_length=1000`
remains in the model definition.

## [5.0.1] — 2026-05-08


### Fixed — Webhook DNS validation now bounded by WEBHOOK_DNS_TIMEOUT (#200)

Audit MED finding (`mem_1778221719390_8cb1ba`): `mnemos/webhooks/
validation.py:54` `_resolve_addrs()` called `loop.getaddrinfo()`
without `asyncio.wait_for`, so slow DNS could stall the async
webhook validation path indefinitely. The configured timeout
(`WEBHOOK_DNS_TIMEOUT`, default 10.0s, lives in
`_WebhookSettings.dns_timeout`) was used by
`_derive_lease_defaults` for lease-budget arithmetic but never
applied to the actual resolution.

Fix: wrap the `getaddrinfo` call in `asyncio.wait_for(...)` with
the configured timeout. Translate the resulting
`asyncio.TimeoutError` into a 422 `HTTPException` with a
distinct `"url host DNS resolution timed out"` detail so
operator logs distinguish slow-DNS from un-resolvable-host.

NB: the timeout `except` clause must come BEFORE the
`(socket.gaierror, OSError)` catch — in Python 3.11+
`asyncio.TimeoutError` aliases builtin `TimeoutError` which is
an `OSError` subclass. Without the explicit ordering the timeout
path silently degrades into the generic
`"url host could not be resolved"` message.

Pinned by `tests/test_webhook_dns_timeout.py` (3 tests):
hang→TimeoutError under tiny `dns_timeout`, TimeoutError→422
translation with the specific detail string, fast resolution
still returns the address list.

### Fixed — Starlette 1.0 compat + pydantic-settings precedence pin (#199)

Surfaced by a fresh-install barrage on PROTEUS (Python 3.13 +
Postgres 17 + clean `pip install -e .[dev,server,edge,...]`).
Two real downstream regressions hidden by stale local
dependency pins:

1. **Starlette 1.0 removed `on_shutdown=`** — `mnemos/mcp/http.py:686`
   passed `on_shutdown=[_drain_audit_tasks_on_shutdown]` to
   `Starlette(...)`. That kwarg was deprecated in 0.x and removed
   in 1.0; fresh installs pull 1.0 → `TypeError:
   Starlette.__init__() got an unexpected keyword argument
   'on_shutdown'` → 9 hard failures across `test_mcp_nats_sse`,
   `test_mcp_http_health`, and `test_connector_smoke`. Local env
   had Starlette 0.x where the deprecation warning fired but
   tests passed.

   Fix: converted the audit-drain hook to an `@asynccontextmanager`
   `_mcp_http_lifespan(app)` that yields then awaits
   `_drain_audit_tasks_on_shutdown()` on exit. Starlette
   constructor now uses `lifespan=_mcp_http_lifespan`. Behavior
   identical — drain still runs on shutdown — but compatible
   with Starlette 1.0+.

   Updated `tests/test_mcp_audit_log.py::
   test_http_transport_registers_drain_on_shutdown` to assert the
   new shape (`lifespan=_mcp_http_lifespan` + `await
   _drain_audit_tasks_on_shutdown()` inside the context manager).

2. **pydantic-settings 2.12 inverted env vs init-kwarg precedence**
   — between 2.11 and 2.12, the rule for `validation_alias` env
   vars vs constructor kwargs flipped. MNEMOS instantiates
   `_DatabaseSettings(**db_section)` from TOML (line 724) and
   relies on `PG_BACKEND` / `PG_DSN` / etc. env vars to override
   empty/inherited TOML values. On 2.12+ env vars no longer
   override init kwargs.

   Fix: pinned `pydantic-settings>=2.0.0,<2.12` in pyproject.toml
   with an inline comment explaining why. The runtime override
   path is preserved; lifting the pin requires either upgrading
   to a precedence-agnostic config-loading shape or accepting
   the new precedence rule as the documented behavior.

   Test:
   `tests/test_postgres_embedding_dim.py::test_runtime_settings_pg_backend_overrides_toml_init`
   was already pinning the expected behavior — it just failed
   silently against modern pydantic-settings on the local box
   because the local pin was stale.

**Barrage results on PROTEUS** (HEAD `110651c` + this slice):
- All 2521 unit/integration tests pass on Python 3.13 + clean
  install + the 23-tool MCP registry from `_TOOL_ORDER`.
- ruff clean.
- Live serve on :5202 against fresh `mnemos_v532_barrage`
  Postgres DB. /health 200, auth probes 401/200, POST/search/
  bulk-create/export/stats all return correct shapes. 100
  OpenAPI paths.
- Server log shows only expected configuration warnings (no
  SESSION_SECRET, no INTERNAL_AUDIT_TOKEN, Redis fallback) and
  zero ERROR-level events, zero tracebacks.

### Fixed — Aspirational endpoint references marked historical (#198)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`). Two doc
surfaces described REST endpoints that don't exist on the daemon:

- `docs/DREAM_STATE_DESIGN.md` (the original Jungian-framed
  divergent-ideation design) describes
  `/v1/dreams/{version_id}/promote`,
  `/v1/dreams/{version_id}/acknowledge`, and
  `/admin/dreams/run`. None of those exist; the shipped MORPHEUS
  subsystem uses `/v1/morpheus/runs*` + `/admin/morpheus/runs`
  (`mnemos/api/routes/morpheus.py:156, 190, 282, 334`). Added an
  inline "did not ship as designed" callout at §9 so the
  promotion-workflow section's endpoint names are clearly
  forward-looking, plus a "[Shipped as `/admin/morpheus/runs`]"
  inline mark on §X's bullet list.
- `docs/connectors/chatgpt-pro-developer-mode.md` and
  `docs/connectors/README.md` describe an experimental
  `mnemos-tunnel-setup` helper that calls daemon-side
  `/admin/tunnels/*` endpoints. As of v5.3.2 the
  `mnemos.tunnels.ngrok_bridge` module + the daemon-side
  endpoints are NOT implemented; the script is aspirational.
  Reworded both doc references to make this explicit and steer
  operators to the manual `mnemos serve mcp-http` + ngrok path
  that works today.

This is the deferred half of the #194 endpoint-name slice — at
that time the audit-driven scope was doc-name correction, while
this slice surfaces the underlying script + missing-endpoint
contract. Keeping the design content as-is preserves the
intentional historical narrative; only the framing was tightened
so future readers don't grep for non-existent routes.

Pinned by `tests/test_doc_aspirational_endpoints_marked.py`
(4 tests): "did not ship as designed" callout near `/v1/dreams/*`
+ live `/admin/morpheus/runs` named; "currently inert" /
"not implemented" warnings near `/admin/tunnels/*` in both
connector doc surfaces; live morpheus routes still in
`mnemos/api/routes/morpheus.py`.

### Fixed — MCP tool-count + hooks-package doc drift (#197)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`). Live MCP
`TOOL_REGISTRY` has 23 tools as of HEAD `07e1154`, but the docs
were stuck in two prior eras:

- 18-tool claims pre-dated the addition of `pantheon_list_models`,
  `pantheon_route_explain`, `kronos_anomalies`, `kronos_forecast`,
  and `recommend_model`.
- 22-tool claims pre-dated the addition of `list_deletions`.

Fixed:

- `README.md` — top-level package layout dropped the (removed)
  `hooks` subpackage; current-state v5.x description; MCP tool
  enumeration now includes `list_deletions`; "18 tools" claims
  on lines 682 + 701 → 23.
- `ROADMAP.md` — cross-tenant security gates "across 22 tools"
  → 23 (with `list_deletions` added to the example list);
  "22 tools from one canonical registry" line → 23.
- `docs/SPECIFICATION.md` — Module-tree dropped `hooks/` (already
  removed in #182); "18 tools from `mnemos/mcp/tools/`" → 23;
  "MCP (stdio and HTTP/SSE, 18 tools)" section heading → 23.
- `docs/connectors/README.md` — "Source of truth" path now
  includes `kronos.py` and `deletions.py` alongside
  memory/kg/dag/models; "canonical 18-tool registry" → 23.

Historical `hooks` mentions in v4.0.0/v4.1.1 "what shipped"
sections of README + ROADMAP are deliberately preserved — they
describe what was real at those tagged releases.

Pinned by `tests/test_doc_mcp_tool_count.py` (4 tests):

- No stale "X tools" / "X MCP tools" count in operator docs
  (live count read at runtime via `len(TOOL_REGISTRY)`).
- `docs/connectors/README.md` "source of truth" line names all
  six canonical MCP tool modules.
- `README.md` MCP enumeration includes `list_deletions`.
- `TOOL_REGISTRY` keeps the canonical tool set.

### Fixed — Doc module-path drift + broken cross-links (#196)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`). Operator
+ architecture docs referenced module paths that no longer exist
in the v4 package layout, plus several broken cross-links to
docs that were never written.

Module-path corrections:

- `docs/MEMORY_ARCHITECTURE.md` named `mnemos/api/lifecycle.py`.
  Updated to describe the actual split:
  `mnemos/core/lifecycle.py` (boot/shutdown + globals),
  `mnemos/api/lifecycle_hooks.py` (FastAPI startup/shutdown),
  and `mnemos/api/main.py` (`add_middleware` registration).
- `docs/OPERATIONS.md:841` named `mnemos/api/observability.py`;
  corrected to `mnemos/core/observability.py`.
- `docs/OBSERVABILITY.md:249` named `mnemos.api.lifecycle._cache`;
  corrected to `mnemos.core.lifecycle._cache`.
- `docs/OBSERVABILITY.md:251` named `mnemos.domain.graeae.providers`
  (no such module). Replaced with the live
  `mnemos.domain.graeae.provider_worker` +
  `mnemos.domain.graeae.provider_sync`.
- `docs/MEMORY_EXPORT_FORMAT.md:594` referenced `mnemos.mpf`.
  Real portability code lives under `mnemos.domain.portability`.
  Same file's `tools/mpf_dump.py` / `tools/mpf_load.py` updated to
  `mnemos/tools/memory_export.py` / `mnemos/tools/memory_import.py`
  / `mnemos/tools/mpf_validate.py`.
- `docs/history/V3_5_CHARTER.md:328` + `docs/history/V3_6_CHARTER.md:141` show
  `python3 -m mnemos.iris.server` as a planned MCP server. The
  module was never implemented; added a "historical" note next
  to each block pointing readers at the live MCP model tools at
  `mnemos/mcp/tools/models.py`.

Broken cross-links:

- `DOCUMENT_IMPORT_GUIDE.md:339-340` linked to `./API.md#memories`
  and `./SEMANTIC_SEARCH.md` — neither file exists. Replaced with
  `API_DOCUMENTATION.md` (root) and `docs/SPECIFICATION.md`.
- `docs/OPERATIONS.md:838-839` named `docs/ARCHITECTURE.md`,
  `docs/API.md`, and `examples/` in the contributor reference;
  none of those paths exist. Replaced with the live docs:
  `README.md`, `docs/MEMORY_ARCHITECTURE.md`,
  `docs/SPECIFICATION.md`, `API_DOCUMENTATION.md`, and the live
  FastAPI OpenAPI spec at `/docs` on a running instance.

Pinned by `tests/test_doc_module_paths_match_code.py` (10 tests):
5 forbidden module-path strings × no-references-anywhere; 2
unqualified pre-restructure prefixes (`api/observability.py`,
`api/auth.py`); no `mnemos.mpf`; live module paths actually exist;
charter docs keep the "never implemented" callout near
`mnemos.iris.server`.

### Fixed — Doc env-var drift (PG_* runtime + MNEMOS_API_KEY) (#195)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`).

- `docs/SPECIFICATION.md` listed `MNEMOS_DB_HOST/PORT/NAME/USER/
  PASSWORD` and `MNEMOS_KEY` as runtime config. The runtime
  `_DatabaseSettings` class in `mnemos/core/config.py` uses
  `env_prefix="PG_"`, so the canonical runtime names are
  `PG_HOST/PG_PORT/PG_DATABASE/PG_USER/PG_PASSWORD`. The API key
  is `MNEMOS_API_KEY` (`MNEMOS_KEY` is only used by
  `tests/test_live_e2e.py`, not fleet config). Updated the
  Bind+DB and Auth subsections to match.
- `DEPLOYMENT.md` showed `PG_POOL_SIZE=50` in the production
  `.env` example; replaced with `PG_POOL_MIN=5` + `PG_POOL_MAX=50`
  per the actual `_DatabaseSettings.pool_min_size` /
  `pool_max_size` validation aliases.
- `docs/OBSERVABILITY.md` named `MNEMOS_DB_POOL_MAX_SIZE` as the
  pool cap; corrected to `PG_POOL_MAX`.
- `docs/OPERATIONS.md` restore-test command set
  `MNEMOS_DB_NAME=mnemos_restore_test`; corrected to
  `PG_DATABASE=...`.
- `docs/MEMORY_ARCHITECTURE.md` claimed operators select the
  compression engine via `MNEMOS_COMPRESSION_ENGINE`. No such
  env var is read anywhere; the engine choice runs through the
  contest mechanism (every registered engine produces a
  candidate; best-by-quality wins). Reworded to describe the
  contest, kept the `MNEMOS_JUDGE_MODE` reference (real, lives
  at `_CompressionSettings.judge_mode`).

Note: `MNEMOS_DB_*` is a legitimate INSTALLER alias accepted by
`mnemos/installer/__main__.py` for env-only deploys; it is not
the runtime-config shape. The CHANGELOG entry from a4eaf5b that
mentions both names side-by-side is therefore historically
accurate and kept as-is.

Pinned by `tests/test_doc_env_vars_match_config.py` (3 tests):
PG_POOL_MIN/MAX validation aliases live; `_DatabaseSettings`
keeps `env_prefix="PG_"`; no forbidden env names
(`MNEMOS_DB_POOL_MAX_SIZE`, `PG_POOL_SIZE`, `MNEMOS_KEY`,
`MNEMOS_COMPRESSION_ENGINE`) appear in operator/runtime docs.

### Fixed — Endpoint-name corrections in connector + portability docs (#194)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`).

- `docs/MEMORY_EXPORT_FORMAT.md` (lines 377, 579) said
  `POST /v1/export`. The live route is `GET /v1/export` per
  `mnemos/api/routes/portability.py:41`.
- `docs/connectors/openai-custom-gpt.md:184` referenced
  `/v1/health` for an auth probe. Health is unversioned
  (`/health`).
- `docs/connectors/{claude-desktop,cline,continue,cursor}.md`
  + `docs/connectors/README.md` all referenced
  `/v1/mcp/discovery`. That route does not exist — MCP
  discovery is the protocol's `tools/list` JSON-RPC method
  over SSE/stdio, not a REST endpoint. Replaced the curl
  examples with two practical checks: `/health` for server-up
  + a Python one-liner against the canonical `TOOL_REGISTRY`
  (`python3 -c 'from mnemos.mcp.tools import TOOL_REGISTRY;
  ...'`). The connectors README's reference to a now-missing
  `mnemos serve mcp-stdio --print-schema` flag was also
  replaced with the same Python-side registry check.

Pinned by `tests/test_doc_endpoints_match_routes.py` (3 tests):
the live `@router.get("/export"...)` shape, the absence of any
`/mcp/discovery` route in `mnemos/api/routes/` AND in any doc
under `docs/`, and that `/health` is in the unversioned
`mnemos/api/routes/health.py` router (not behind `/v1`).

### Fixed — Doc version drift across operator surfaces (#193)

Surfaced by the deep documentation-sweep codex audit at HEAD
`de13b51` (saved as MNEMOS `mem_1778221719446_2cdcad`). Both
`pyproject.toml` and `mnemos/_version.py` were at 5.3.2, but
~10+ operator-facing docs still claimed "current is v5.0.0" or
worse — `SYSTEM_REQUIREMENTS.md` + `QUICK_START_REQUIREMENTS.md`
+ `SECURITY.md` were claiming current is v4.0.0.

Updated "current state" claims (left historical mentions alone):

- `README.md` — header, install commands, docker pull, single-
  binary URL, "current GA line" summary paragraph.
- `DEPLOYMENT.md`, `API_DOCUMENTATION.md` — header status lines +
  install pins.
- `SYSTEM_REQUIREMENTS.md`, `QUICK_START_REQUIREMENTS.md`
  — release-line headers (4.0.0 → 5.3.2!) + install pins +
  download URLs.
- `ROADMAP.md` — "Current status" header.
- `docs/history/EVOLUTION.md` — "current vX.Y release line" line.
- `docs/OPERATIONS.md` — header + §11.1 Architecture banner.
- `docs/INSTALL.md` — install matrix + bundle commands.
- `docs/SPECIFICATION.md` — header version + "Authoritative for
  the checked-out vX.Y tree" line.
- `docs/GRAEAE_FEATURES.md` — header status.
- `SECURITY.md` — "current release line" paragraph + "as of"
  marker.
- `docs/papers/mnemos-dag-distillation.md` — IRIS reference
  paragraph.

Pinned by `tests/test_doc_version_pins_match_code.py` (13 tests):

- `pyproject.toml` ↔ `mnemos/_version.py` agreement
- "current vX" phrase across 10 operator docs
- no stale `==<old>` install pins in install/quick-start docs
- no stale `releases/download/v<old>/` URLs in download docs

The test file reads `__version__` at run time so future bumps
auto-update without churning the test against a literal.

### Removed — 7 audit-flagged dead helpers + 2 orphan fixtures (#192)

Surfaced by the deep cross-code codex audit at HEAD `de13b51`
(saved as MNEMOS `mem_1778221719390_8cb1ba`). All confirmed zero
callers across the corrected #186-onwards scope (mnemos/, tests/,
scripts/, systemd/, deploy.sh, pyproject.toml).

Helpers:

- `ProviderResponse` Pydantic class (mnemos/domain/models.py) —
  declared but no route used it as `response_model=`.
- `ProviderResponse` `@dataclass` (mnemos/domain/graeae/engine.py)
  — declared but never instantiated. Live shape is
  `ProviderQueryResponse` (used by `_provider_worker_payload`).
- `ModelRecommendation` Pydantic class (mnemos/domain/models.py) —
  duplicate of the live dataclass at
  `mnemos/persistence/types.py` (which is re-exported via
  `mnemos/persistence/__init__.py`).
- `JournalEntry` (mnemos/api/routes/journal.py) — Pydantic
  response model; routes return raw dict/list.
- `_sha256_hex` (mnemos/db/deletion_log.py) — PostgreSQL
  `digest(..., 'sha256')` is the live hashing path inside the
  deletion-log SQL.
- `_looks_like_sqlite_conn` (mnemos/db/deletion_log.py) —
  duplicate of the live function in
  `mnemos/db/mcp_audit_repo.py`.
- `_row_get` (mnemos/db/deletion_log.py) — declared but never
  called inside the module.
- `drain_routing_log_queue_for_tests`
  (mnemos/domain/pantheon/routing_log.py) — exported in
  `__all__` but no test/script ever called it. `__all__` entry
  also removed.

Plus 2 orphan pytest fixtures:

- `event_loop` in `tests/__init__.py` — pytest only collects
  fixtures from `conftest.py`, not package `__init__`, so this
  was silent dead code since the v4.0 restructure.
- `event_loop` (session scope) in `tests/test_e2e.py` — no
  test in the file requested it as a parameter; pytest-asyncio
  `mode=Mode.STRICT` auto-manages the loop.

`hashlib` import in `deletion_log.py` removed (only consumer
was `_sha256_hex`); `asyncio` import in `tests/test_e2e.py`
auto-removed by ruff (only consumer was the dropped fixture).

Pinned by `tests/test_dead_audit_helpers_192_removed.py` (18
tests). Helpers with intentional duplicates in other modules
(`ModelRecommendation`, `_looks_like_sqlite_conn`, `_row_get`)
are pinned by per-file `definition_removed` checks; the
external-caller scan skips them to avoid false positives on
the live duplicates.

### Removed — Dead memory-tier API + stale README claim (#191)

127-line `mnemos/domain/memory_categorization/tiers.py` was an
entire dead API: `MemoryTier` dataclass, `TIER_1..4` instances,
`TIERS` registry, `TIER_NAMES`, `get_tier`, `get_tier_by_name`,
`list_tiers`. After #188 removed `JournalManager` and
`TierSelector` (the only modules that knew about the hot/warm/
cold/archive tier model), the entire tier API was orphaned.

`memory_categorization/__init__.py` slimmed: now only exports
`EntityManager` + `StateManager` (the two classes with live
callers in tests + `mnemos/api/routes/state.py`).

README:641-643 also corrected — claimed the package "still
exposes a hot/warm/cold/archive selector for hook-side prompt
budgeting." Hooks were removed in #182, the selector itself had
no callers, and the claim painted a feature that no longer
existed. Section dropped from README. Surfaced by the deep
codex audit at HEAD `de13b51`.

Pinned by `tests/test_dead_tier_api_removed.py` (14 tests:
file-absence × 1, __all__-entries × 1, no-imports-anywhere × 10,
no-bare-module-import × 1, README-claim-absent × 1).

### Removed — 4 dead compat-shim / placeholder modules (#190)

Four modules with zero imports anywhere — three were thin
re-export shims over the canonical `mnemos.core.resilience`
location, one was an empty placeholder docstring:

- `mnemos/db/repositories.py` (1 line) — empty placeholder
  ("Repository placeholders for future SQL extraction work.").
  No symbols, no callers.
- `mnemos/domain/graeae/_concurrency.py` (10 lines) — re-exported
  `ConcurrencyLimiterPool` / `ProviderConcurrencyLimiter` from
  `mnemos.core.resilience`.
- `mnemos/domain/graeae/_circuit_breaker.py` (11 lines) —
  re-exported `CircuitBreaker` / `CircuitBreakerPool` /
  `CircuitState`.
- `mnemos/domain/graeae/_rate_limiter.py` (10 lines) —
  re-exported `RateLimiter` / `RateLimiterPool`.

Live callers (engine.py, _cache.py, _quality.py,
tests/test_resilience.py) all import from
`mnemos.core.resilience` directly. Sibling `_cache.py` and
`_quality.py` ARE imported and remain.

Pinned by `tests/test_dead_compat_shim_modules_removed.py`
(8 parametrized cases: file-absence × 4 + no-imports × 4).

### Removed — Dead `GraeaeEngine._query_provider` wrapper (#189)

Thin pass-through wrapper at `mnemos/domain/graeae/engine.py:1196`
over `_call_provider_worker` with no callers. The 3 real call
sites (lines ~695, ~1046, ~1132) all invoke
`_call_provider_worker` directly. The wrapper added a function-
call layer with no behavior of its own.

Two stale doc references in the same module (`_load_providers`
docstring and `_probe_model` docstring) updated to point at
`_call_provider_worker`. One stale reference in
`MQ_INTEGRATION.md` (migration step 3) likewise updated.

Pinned by `tests/test_dead_query_provider_wrapper_removed.py`
(3 tests: method-absence, no-references-anywhere with engine.py
slice-marker allowlist, allowlist-exception-must-be-comment-only
to prevent regression doorway).

### Removed — Dead JournalManager + TierSelector classes (#188)

Two entire classes dead since v4.0 A.1 (commit 72508a5): both
re-exported from `mnemos/domain/memory_categorization/__init__.py`'s
`__all__` but never imported by any other module.

- `JournalManager` in `mnemos/domain/memory_categorization/
  journal.py` (224 lines, full file removed) — date-partitioned
  journal entry management. Sibling `StateManager` in
  `state.py` is the live state-tracking class (imported in
  `tests/test_state_manager_durability.py` and used by
  `mnemos/api/routes/state.py`).
- `TierSelector` in `mnemos/domain/memory_categorization/
  tier_selector.py` (156 lines, full file removed) — task-to-
  tier mapping prototype. (At the time of #188 the live tier
  accessors `get_tier`, `get_tier_by_name`, `list_tiers` in
  `tiers.py` were retained; #191 then removed those too once
  it was confirmed they had no callers.)

`__init__.py` `__all__` updated. Sibling `EntityManager`
(entities.py) and `StateManager` (state.py) ARE imported in
tests and remain.

Pinned by `tests/test_dead_categorization_managers_removed.py`
(6 parametrized cases: file-absence × 2, __all__ entry × 2,
no-imports-anywhere × 2).

### Removed — 3 dead installer helpers (#187)

First slice run with the corrected dead-code scan scope (#186
lesson): now also greps `scripts/*.py`, `systemd/*.service`,
console_scripts in `pyproject.toml`, and shell scripts.

- `pgvector_installed(config)` in `mnemos/installer/db.py` —
  defined but never called. Installer `__main__` imports
  `run_migrations`, `setup_database`, `setup_sqlite_database`,
  `create_api_key`, `verify_connection` from this module — but
  not `pgvector_installed`. Pgvector is installed unconditionally
  via `CREATE EXTENSION IF NOT EXISTS vector` in `setup_database`.
- `service_status(service_name)` in `mnemos/installer/service.py`
  — defined but never called. Installer `__main__` imports
  `create_service_user`, `enable_service`, `install_launchd`,
  `install_systemd`, `start_service` from this module — but not
  `service_status`. Operators use `systemctl is-active mnemos`
  / `launchctl list ai.mnemos` directly when post-install status
  is needed.
- `_which_exists(name)` in `mnemos/installer/service.py` —
  was only called by `service_status`; cascade-dead.

Pinned by `tests/test_dead_installer_helpers_removed.py` (6
parametrized cases). Test scope explicitly covers
`scripts/*.py`, `scripts/*.sh`, `systemd/*.service`, `deploy.sh`,
and `pyproject.toml` to prevent the #186-style false positive.

### Removed — 3 small dead public helpers (#185)

Continuing the dead-code audit:

- `publish_federation_memory_upsert(row)` (row-form overload) in
  `mnemos/persistence/nats_events.py` — defined but never called.
  Live callers use the `_event(event)` variant directly
  (`postgres.py` + integration tests).
- `get_tier_compression_budget` and `get_tier_compression_ratio`
  in `mnemos/domain/memory_categorization/tiers.py` — single-
  line accessors over `get_tier(level).token_budget` /
  `.compression_ratio`. Dead since the v4.0 package restructure.

Larger update_graeae_config / update_openclaw_models functions
in `mnemos/domain/graeae/model_registry.py` (also dead since v4.0)
deferred — those are 100+ lines each and need a careful review of
whether they're stale prototypes worth removing or in-progress
features worth wiring up. `register_task_classifier` (public-API
setter pair where the getter IS used by openai_compat router)
also kept — possible contract preservation rather than dead code.

### Removed — 3 more dead helpers (#184)

Continuing the #183 dead-code audit:

- `_get_db()` in `mnemos/core/lifecycle.py` — async helper that
  returned `_pool.acquire()`. No callers; everything uses
  `get_pool_manager().acquire()` directly (with the
  `require_postgres_pool_or_503` guard).
- `_executemany()` in `mnemos/persistence/sqlite.py` — wrapped
  `conn.executemany` with sqlite-value normalization. No callers;
  multi-row writes go through individual `await conn.execute(...)`
  calls. Sibling `_executescript` is still live.
- `log_deleted_memory_row()` (public-named, single-row variant)
  in `mnemos/db/deletion_log.py` — superseded by the set-scope
  `log_target_memory_deletions` (used by the deletion request
  worker) and `log_morpheus_run_memory_deletions` (used by the
  morpheus runner). The single-row form had no callers.

### Removed — 5 dead private helper functions (#183)

AST scan flagged 16 module-level non-decorated functions with
single-occurrence names. Five underscore-prefixed (private) ones
were genuine dead code:

- `_read_visibility_predicate` in `api/routes/memories.py`
- `_federation_tombstone_filters` in `api/routes/federation.py`
- `_metadata_has_key` in `domain/morpheus/runner.py`
- `_select_cheapest` in `domain/pantheon/aliases.py`
- `_reset_row_for_infra_retry` in
  `domain/compression/worker_contest.py` (its own docstring
  admitted "kept for backward compatibility with the round-32
  single-site call shape" — exactly the kind of stale shim
  CLAUDE.md says to avoid)

Public-API candidates (e.g. `register_task_classifier`,
`pgvector_installed`, `get_tier_compression_*`) deferred to a
separate audit — those need extra-careful "is anyone importing
this externally?" review before removal.

### Removed — Dead mnemos/hooks/ package (#182)

The entire `mnemos/hooks/` tree (640 lines: `hook_registry.py`,
`prompt_submit.py`, `session_start.py`, `__init__.py`) was dead
since v4.0 — no imports anywhere in `mnemos/`, `tests/`, or
`docs/`. Only reference was `pyproject.toml`'s
`setuptools.packages.find` list (circular: listing it for
packaging doesn't make it used).

Removed the directory + the pyproject.toml entry. 3 regression
tests in `tests/test_dead_hooks_package_removed.py` pin:
- directory does not exist
- pyproject.toml doesn't list `"mnemos.hooks"`
- no source file imports from `mnemos.hooks` (catches a merge
  that re-introduces a stale import)

If a hook system is needed in the future, build it deliberately
rather than recovering this dead variant — the patterns it used
will be out of date with the rest of the codebase.

### Removed — 7 dead module-level constants (#181)

AST scan for module-level `UPPER_CASE = ...` assignments with
only one occurrence in the entire `mnemos/`+`tests/` tree (the
definition itself) found 8 candidates. 7 were genuine dead code
and removed:

- `TUNNEL_PREDICATE_PREFIX` in `mnemos/tools/knossos_mcp.py`
- `DEFAULT_NATS_SSE_SUBJECT` in `mnemos/mcp/http.py`
- `QUEUE_GROUP` in `mnemos/webhooks/nats_trigger.py`
- `_STRUCTURAL_EDGES` in `mnemos/tools/adapters/cognee.py`
- `_VALID_REASONS` + `_VALID_PROFILES` in
  `mnemos/api/routes/admin.py` (kept speculatively in #170 "for
  any operator-introspection callers"; no such callers exist)
- `_INFRA_RESET_SQL` (single-row variant) in
  `mnemos/domain/compression/worker_contest.py` (replaced by the
  batch variant `_INFRA_RESET_BATCH_SQL`)
- `_SYMBOL_RE` in `mnemos/domain/compression/apollo_schemas/code.py`

`_REGISTRY_LOCK` in `gpu_guard.py` was also flagged but kept —
defensive code in a single-threaded asyncio context where the
lock would never serialize a real race anyway.

8 parametrized tests in
`tests/test_dead_module_constants_removed.py` pin the removals.

### Removed — Dead ProviderListResponse model (#180)

`ProviderListResponse` was declared in `mnemos/domain/models.py`
but never used by any route — only `OAuthProviderListResponse`
appears in the codebase. Removed; same dead-model shape as
`SessionHistoryRequest` in #179.

Found by extending the #178/#179 declared-vs-consumed audit to
*Response* models. Test pins the removal so a future
re-introduction without wiring fails loudly.

### Removed — Dead Pydantic request fields (#179)

Continuing the #178 declared-vs-consumed audit, two more silent
gaps closed:

- `ConsultationRequest.context` was declared but never read by
  the `consult_graeae` route handler. Removed.
- `SessionHistoryRequest` was an entire dead model — route uses
  `Query()` params directly, model was never imported. Removed.

3 tests in `tests/test_dead_pydantic_fields.py`: pin both
removals + best-effort scan over all `*Request` models in
`mnemos/domain/models.py` for fields not referenced in
`mnemos/api/routes/`. Fields consumed indirectly (via
`model_dump()` or `**body.dict()`) are listed in a
`_KNOWN_INDIRECT_REFERENCES` allowlist; a future model field that
silently isn't consumed AND isn't in the allowlist will fail the
test, prompting a manual review.

### Removed — Unused MemoryUpdateRequest.quality_rating field (#178)

`MemoryUpdateRequest.quality_rating` was declared in the Pydantic
model but never consumed by the `update_memory` route handler.
Clients setting it expected updates that silently never happened —
a doc-vs-behavior gap. Removed the field.

Pydantic v2's default `extra="ignore"` means existing clients
that still pass `quality_rating` parse through unchanged; the
field simply no longer appears in the OpenAPI schema as a
"supported" update.

3 tests in `tests/test_memory_update_request_no_quality_rating.py`
pin the removal so a future re-introduction without handler
wiring fails loudly.

### Changed — Universal exc_info=True regression covers entire mnemos/ tree (#177)

Replaced the four parametrized regression tests added in
#173/#174/#175/#176 (which covered routes, workers, domain+core,
nats+mcp = 122 modules) with a single universal sweep over the
ENTIRE `mnemos/` tree (212 modules). Empty/trivial modules pass
the guard via `assert not missing`; only modules with an
`except Exception as <name>:` block calling `logger.error(...)`
without `exc_info=True` fail.

A new module added anywhere under `mnemos/` that doesn't follow
the contract will fail this test the moment its file lands —
even in trees that didn't have any matching handlers when the
sweep was originally written (installer, persistence, federation,
hooks, etc.).

### Fixed — Final exc_info=True sweep across nats + mcp (#176)

8 except-block logger.error calls across `mnemos/nats/` and
`mnemos/mcp/` lacked `exc_info=True`:
- `nats/client.py`: 2 (drift detection + transient probe failure)
- `mcp/tools/__init__.py`: 1 (generic tool failure)
- `mcp/tools/dag.py`: 4 (log_memory, branch_memory,
  diff_memory_commits, checkout_memory)
- `mcp/tools/models.py`: 1 (recommend_model)

Combined with #161/#172/#173/#174/#175, the entire codebase under
`mnemos/api/routes/`, `mnemos/workers/`, `mnemos/domain/`,
`mnemos/core/`, `mnemos/nats/`, and `mnemos/mcp/` now consistently
uses `exc_info=True` for except-block logger.error calls.

Extended the parametrized regression test to cover nats + mcp (14
new test parameters). Total coverage: 119 modules across 4 trees.

### Fixed — exc_info=True sweep across domain + core (#175)

10 except-block logger.error calls across `mnemos/domain/` and
`mnemos/core/` lacked `exc_info=True`:
- `domain/graeae/api_keys.py`: 1 (PRF parse failure)
- `domain/graeae/engine.py`: 1 (provider routing failed)
- `domain/openai_compat/providers.py`: 1 (routing failed)
- `domain/openai_compat/router.py`: 2 (streaming preflight + sync)
- `domain/openai_compat/streaming.py`: 2 (route + response)
- `core/lifecycle.py`: 3 (persistence init + pgvector + FTS fallback)

Combined with #161/#172/#173/#174, the entire codebase under
`mnemos/api/routes/`, `mnemos/workers/`, `mnemos/domain/`, and
`mnemos/core/` now consistently uses `exc_info=True` for
except-block logger.error calls. Extended the parametrized
regression test to cover all four trees (75 new domain+core test
parameters).

### Fixed — Workers exc_info coverage + extended regression test (#174)

`mnemos/workers/distillation.py`'s nested DB-reconnect except
handler logged without `exc_info=True`. Fixed and extended the
parametrized regression test from #173 to cover all worker modules
in addition to routes — same logging contract applies (operators
need stack traces in worker logs, doubly so for retried/async
batch processing).

### Fixed — All route except handlers now log with exc_info=True (#173)

10 `except Exception as e:` blocks across 5 route modules
(`document_import.py`, `health.py`, `ingest.py`, `providers.py`,
`sessions.py`) called `logger.error(...)` without `exc_info=True`.
Combined with #161 (entities) + #172 (dag), the entire
`mnemos/api/routes/` tree now uses `exc_info=True` consistently.

A new parametrized regression test in
`tests/test_routes_exc_info_coverage.py` walks every route file in
`mnemos/api/routes/` and asserts the contract — any future "polish"
pass that drops `exc_info=True` from any route gets caught.
Implementation uses a line-based scan rather than a multiline regex
(catastrophic backtracking made the regex variant hang on dag.py).

### Fixed — dag.py except blocks now log with exc_info=True (#172)

5 `except Exception as e:` blocks in `mnemos/api/routes/dag.py`
called `logger.error(f"... {e}")` without `exc_info=True`.
Operators saw the exception's `__str__` in logs but no stack
trace — hard to diagnose where the failure originated.

Added `exc_info=True` to all 5 (DAG Log, Branches, Branch
creation, Commit fetch, Merge failures), matching the #161
entities-route pattern.

2 tests in `tests/test_dag_route_error_logging.py`: source-level
guard that every `except Exception as e:` block in dag.py
calling `logger.error` includes `exc_info=True`; module-level
guard that `logger` is defined at module scope.

### Changed — EntityCreateRequest.entity_type tightened to Literal (#171)

Last remaining `if request.X not in ENUM_CONSTANT` runtime check.
`EntityCreateRequest.entity_type` is now `Literal["person",
"project", "concept", "document", "decision", "event"]` mirroring
the `ENTITY_TYPES` constant. Removed the runtime check at
`create_entity` (was `HTTPException(400)`).

Because `Literal[*ENTITY_TYPES]` (PEP 646 unpacking) isn't
supported in stable Python yet, the values are duplicated. A
parity test in `tests/test_entities_create_literal.py` reads the
Literal annotations via `typing.get_type_hints` and asserts they
equal `set(ENTITY_TYPES)` — a future addition to one without the
other will fail loudly.

13 tests: parity guard + 6 parametrized accepts (one per
documented type) + 6 parametrized rejects (uppercase, trailing
whitespace, trailing newline, undocumented value, empty,
comma-injected).

### Changed — Remaining enum-string fields tightened to Pydantic Literal (#170)

Continuing the #168/#169 pattern, three more `str` fields validated
at the route handler against fixed enums are now Literal[...]:

- `MergeRequest.strategy` → `Literal["latest-wins", "manual"]`
  (in mnemos/api/routes/dag.py)
- `CompressionEnqueueRequest.reason` →
  `Literal["on_write", "manual", "scheduled", "reprocess"]`
- `CompressionEnqueueRequest.scoring_profile` →
  `Literal["balanced", "quality_first", "speed_first", "custom"]`
- Same Literal types applied to `CompressionEnqueueAllRequest`
  (alias the type so the two models stay in sync).

Removed 5 redundant runtime checks in
`mnemos/api/routes/dag.py::merge_branch` and
`mnemos/api/routes/admin.py::compression_enqueue` +
`compression_enqueue_all`.

4 existing tests updated from `HTTPException(422)`-from-handler to
`ValidationError`-at-parse-time semantics. Two `_VALID_REASONS` /
`_VALID_PROFILES` sets retained for any operator-introspection
callers, with new public type aliases `CompressionReason` /
`CompressionProfile` mirroring the Literal enums.

### Changed — UserCreate.role + OAuthProvider.kind tightened to Pydantic Literal (#169)

- `UserCreateRequest.role` was `str` validated at the route handler;
  fixed comment said "user or root" but runtime accepted three
  values. Tightened to `Literal["user", "root", "federation"]`.
- `OAuthProviderCreateRequest.kind` was `str` validated at the
  route handler. Tightened to `Literal["oidc", "oauth2"]`.
- Removed the redundant runtime checks in
  `mnemos/api/routes/admin.py`'s `create_user` and
  `oauth_create_provider` handlers.
- 1 existing test (`test_admin_still_rejects_arbitrary_roles`)
  updated to expect `ValidationError` at model parse time instead
  of `HTTPException` from the handler. 2 new tests in
  `tests/test_oauth.py`: kind accepts "oidc"/"oauth2"; rejects
  invalid (saml, uppercase, trailing whitespace, empty, unknown).

### Changed — compat_mode tightened to Pydantic Literal (#168)

- `FederationPeerCreateRequest.compat_mode` was `str` validated at
  the route handler via `if request.compat_mode not in ("strict",
  "permissive")`. Moved to
  `Literal["strict", "permissive"]` so Pydantic auto-422s before
  the handler runs, with field-level error detail and a proper
  enum in the OpenAPI schema. Same tightening applied to
  `FederationPeerUpdateRequest.compat_mode` (now
  `Optional[Literal["strict", "permissive"]]`).
- Removed the redundant runtime checks in
  `mnemos/api/routes/federation.py`'s register_peer + update_peer
  handlers.
- 4 tests in `tests/test_federation.py`: accepts strict +
  permissive; rejects invalid (loose, STRICT, trailing-space, "",
  "off"); update path accepts None + valid value; update path
  rejects invalid.

### Fixed — Enforce FederationPeerCreateRequest.name constraint (#167)

- The Pydantic field's docstring claimed "lowercase alnum + dash,
  3-64 chars" but the field had no actual validator. Even though
  only root can register peers, peer names get spliced into
  federated memory IDs downstream — weird chars (newlines,
  underscores, slashes) leaked into IDs.
- Added `pattern=r"\A[a-z][a-z0-9\-]{2,63}\z"` (Pydantic v2 uses
  Rust regex; `\z` is the end-of-string anchor) plus
  `min_length=3`/`max_length=64` so the docstring's claim is now
  enforced.
- 2 tests in `tests/test_federation.py`: 5 valid shapes accepted
  (peer-1, alpha-beta, p123, etc.); 13 invalid shapes rejected
  (empty, too-short, too-long, uppercase, underscore, period,
  whitespace, newline, null byte, leading digit/dash, slash,
  non-ASCII).

### Fixed — _sql_identifier / _sql_cast reject trailing newline (#166)

- Added 45 boundary tests for `_sql_identifier` and `_sql_cast` in
  `mnemos/core/security.py` — these helpers prevent SQL injection
  via dynamic table/column/cast names interpolated into f-string
  queries (used by `assert_owned_context`). They previously had no
  direct test coverage; only their integration paths were
  exercised.
- The new tests caught a real validator gap: the regexes used
  `^...$` anchors, but Python's `$` matches before a trailing
  newline by default. So `_sql_cast("uuid\n")` returned without
  raising. Postgres treats `\n` as whitespace so the immediate
  exploit surface was limited, but the validator's contract is
  "no whitespace, no control chars" — fixed by switching to `\A`
  and `\Z` anchors which match only at start/end of string.

### Added — Verify warning emission on bounded-backlog drop (#165)

- `test_schedule_audit_persist_bounded_backlog` previously only
  asserted the `_INFLIGHT_AUDIT_TASKS` set count didn't grow when
  the cap was reached. Without verifying the warning log line, a
  future refactor could silently drop the diagnostic — operators
  hitting the cap would have no signal that audit rows were being
  dropped. Test now uses `caplog` to assert the warning fires AND
  names the dropped tool + caller (so operators can tell which
  invocations they're losing).

### Added — Parametrized writer coverage for all VALID_OUTCOMES (#164)

- `tests/test_mcp_audit_log.py` parametrized
  `test_insert_audit_record_accepts_each_valid_outcome` over all 6
  values of `VALID_OUTCOMES` (called/success/failure/error/denied/
  root_bypass). Earlier coverage only exercised "error" + the
  garbage-rejection case; the new emission paths from #154
  (rate-limit → "denied"), #156 (context-mismatch → "denied"), and
  #157 (handler-failure → "failure") would have silently regressed
  if the schema CHECK or repo validation drifted out of sync with
  the dispatcher.

### Fixed — Stdio bridge logs drained audit task count on shutdown (#163)

- `mcp/stdio.py`'s drain finally-block discarded the
  `drain_pending_audit_tasks()` return value, so stdio operators
  had no observable signal that audit writes were waiting at
  shutdown. The HTTP/SSE bridge already logged this; stdio now has
  parity (`drained N pending mcp_audit_log persist task(s) on
  shutdown`).
- Source-level test in `tests/test_mcp_audit_log.py` pins the
  capture+log pattern so a future refactor can't silently drop the
  observability.

### Fixed — Reject empty owner_id/namespace at /v1/export route (#162)

- Added `min_length=1` constraint on `owner_id` and `namespace`
  query parameters in `mnemos/api/routes/portability.py`. Empty
  strings now fail-fast with HTTP 422 at the FastAPI validator
  rather than silently reaching `export_memories` and producing
  empty result sets (the SQL would filter `owner_id=""` /
  `namespace=""`, which never matches a real memory).
- Defense-in-depth alongside #159 (cursor decoder rejects empty
  strings) and #160 (cursor encoder rejects empty strings). Now
  the entire pipeline — request → validate → encode → decode —
  rejects empty-string scope at every layer.
- 2 tests in `tests/test_portability_v02_emission.py`: empty
  owner_id rejected with 422; empty namespace rejected with 422.

### Fixed — Entities route 500s now log the underlying exception (#161)

- 4 `except Exception:` blocks in `mnemos/api/routes/entities.py`
  raised HTTP 500 with no log entry. Operators saw "Internal
  server error" in the response without any breadcrumb to diagnose
  the underlying cause (DB connection drop, asyncpg error, etc.).
  Added `logger.error(..., exc_info=True)` calls to all 4 (matching
  the pattern that the create-entity handler already used).
- 2 tests in `tests/test_entities_route_error_logging.py`: source-
  level regression guard that every `except Exception:` block
  contains `logger.error(..., exc_info=True)`; module-import guard
  that `logger` is defined at module scope.

### Fixed — Encoder also rejects empty-string scope (#160)

- Defense-in-depth follow-up to #159: `_encode_deletion_log_cursor`
  now also raises `ValueError` when `effective_owner=""` or
  `effective_ns=""` is passed. Without this, the encoder would
  happily pack an empty string into a cursor that the decoder
  (post-#159) then rejects on the next page — symmetrical
  validation at the source prevents the bug from propagating into
  a cursor in the first place.
- 2 tests in `tests/test_portability_v02_emission.py`: encoder
  rejects empty string for both fields; encoder + decoder
  round-trip continues to work for null scope (regression).

### Fixed — Reject empty-string scope in deletion_log cursor decode (#159)

- `_decode_deletion_log_cursor` previously accepted
  `effective_owner=""` / `effective_ns=""` because the type check
  was `isinstance(val, str)` and empty string passed. Cursors
  produced by this server use `null` for unscoped, never an empty
  string — an attacker constructing a cursor with empty-string
  scope bypassed the per-tenant guard for root callers (the SQL
  query would filter on `owner_id=""` / `namespace=""`, an
  unexpected query shape that never matches a real memory but
  shouldn't be reachable).
- Tightened the type check to also reject empty strings with HTTP
  400 ("must be a non-empty string or null"). Null remains the
  documented value for unscoped root exports.
- 3 new tests in `tests/test_portability_v02_emission.py`: empty
  effective_owner rejected, empty effective_ns rejected, null scope
  accepted (regression — must continue working for unscoped exports).

### Added — Boundary tests for parameter_shape size limits + dead-code cleanup (#158)

- 5 new tests in `tests/test_mcp_audit_log.py` covering the
  `_validate_parameter_shape` size limits that previously had no
  coverage:
  - rejects key names over `_MAX_PARAMETER_SHAPE_KEY_LENGTH` (128)
  - accepts key names at exactly the limit (boundary)
  - rejects `item_types` lists over `_MAX_PARAMETER_SHAPE_ITEM_TYPES` (16)
  - accepts `item_types` at exactly the limit (boundary)
  - asserts `_MAX_PARAMETER_SHAPE_TYPE_NAME` was removed
- Removed `_MAX_PARAMETER_SHAPE_TYPE_NAME = 32` — defined but never
  read. The closed `_ALLOWED_SHAPE_TYPE_NAMES` allowlist is strictly
  more restrictive than any 32-char ceiling would be, so the
  constant was dead code. The new test pins the cleanup so a future
  reintroduction can't go uncovered.

### Changed — Handler "failure" return distinguished from raised "error" (#157)

- `execute_tool` now emits `outcome="failure"` (instead of "error")
  when a handler returns `{"success": False, ...}` without raising.
  A raised exception continues to emit `outcome="error"`. The
  schema's CHECK constraint already permitted both values; mapping
  them to distinct outcomes lets operators query
  `WHERE outcome = 'failure'` ("tool ran but said no") vs
  `WHERE outcome = 'error'` ("tool raised") cleanly.
- 3 tests in `tests/test_mcp_tool_security.py`: handler-returning-
  False maps to "failure" with error_class="ToolError"; raised
  exception maps to "error" with the raised type's name as
  error_class; successful return maps to "success".

### Changed — Context-mismatch denials emit outcome="denied" (#156)

- The three `ContextMismatch` early-return paths in `execute_tool`
  (caller_id, role, and namespace mismatch between transport-side
  context and authenticated user) now audit with `outcome="denied"`
  instead of `outcome="error"`. These represent attempted
  cross-tenant or privilege-escalation requests, not internal
  errors — operators querying denials should see them alongside
  rate-limit denials.
- Combined with #154 (rate-limit denials), this makes
  `SELECT * FROM mcp_audit_log WHERE outcome = 'denied'` a
  one-stop query for "any tool call that was rejected by policy" —
  rate-limits + context-mismatches.
- 3 tests in `tests/test_mcp_tool_security.py`: caller-id mismatch,
  role mismatch, namespace mismatch.

### Changed — Rate-limit denials in audit log emit outcome="denied" (#154)

- `execute_tool`'s `PermissionError` handler now distinguishes
  rate-limit denials from generic permission errors. When the
  exception text contains `rate limit` (case-insensitive), the
  audit row is recorded with `outcome="denied"` instead of
  `outcome="error"`. The schema's CHECK constraint already permitted
  both values (`called/success/failure/error/denied/root_bypass`),
  but the dispatcher previously only emitted `error`, leaving
  operators with no way to query rate-limit events distinctly.
- After this change:
  ```sql
  SELECT * FROM mcp_audit_log WHERE outcome = 'denied'
   ORDER BY created_at DESC;
  ```
  cleanly returns rate-limit denials. Generic
  `PermissionError`/Resource-not-found cases continue to map to
  `outcome="error"` (no semantic change).
- 3 tests in `tests/test_mcp_tool_security.py`: rate-limit
  PermissionError → "denied"; non-rate-limit PermissionError →
  "error"; case-insensitive match (e.g. "Rate Limit Exceeded") →
  "denied".

### Added — Route-level trust-boundary tests for /v1/internal/mcp_audit (#153)

- 8 tests in `tests/test_mcp_audit_route_trust_boundary.py` covering
  the FastAPI dependency-injection wiring of
  `_require_internal_audit_token` end-to-end via TestClient:
  legacy mode (token unset) accepts authenticated bearer with no
  audit header; legacy mode ignores even garbage audit headers;
  locked-down mode (token set) accepts correct
  `X-Mnemos-Audit-Token`, rejects missing / wrong / whitespace-only
  values; constant-time-compare guard catches prefix-match attempts;
  source-level guard ensures the route signature still wires the
  trust-boundary dependency.
- Existing `test_mcp_audit_log.py` covered the writers (insert,
  persist_via_pool, persist_via_http) but didn't exercise the
  dependency-injection wiring at the FastAPI route level — a future
  refactor that dropped `Depends(_require_internal_audit_token)`
  would have been caught only by manual inspection.

### Added — Length-floor warning for short audit tokens (#155)

- `_warn_if_audit_token_unset` now also warns when the configured
  `[server].internal_audit_token` is set but shorter than
  `_AUDIT_TOKEN_MIN_LENGTH` (32 chars / 128 bits). The lockdown
  itself uses `hmac.compare_digest`, which doesn't care about
  length — so a short token still locks down the endpoint, but a
  4-char value is trivially brute-forceable. The autogen path
  produces 64-char hex; anything dramatically shorter is almost
  certainly an operator typo or placeholder.
- Distinct message from the legacy-mode warning so operators can
  disambiguate from log records. Names a copy-paste-ready remediation
  (`python -c 'import secrets; print(secrets.token_hex(32))'`).
- 5 new tests in `tests/test_audit_token_startup_warning.py`: warns
  when too short, silent at exactly the floor, warns just below the
  floor, distinguishes unset-vs-short, remediation guide included.

### Added — Startup warning for legacy audit-token mode (#152)

- **`_warn_if_audit_token_unset(settings)`** helper in
  `mnemos/api/main.py` emits a one-time WARN at API import / boot
  when `[server].internal_audit_token` is empty. After #150/#151 the
  installer makes the autogen default-on, so an unset token at
  runtime usually means an operator-initiated downgrade or a
  partial/incomplete upgrade — surface it so nobody is surprised
  that `/v1/internal/mcp_audit` is operating in legacy mode (any
  authenticated bearer-token caller can POST audit rows).
- **Remediation paths in the message.** Names BOTH the
  `python -m mnemos.installer --upgrade` autogen route and the
  `MNEMOS_INTERNAL_AUDIT_TOKEN` env-var override so operators can
  pick whichever matches their deployment shape.
- 6 tests in `tests/test_audit_token_startup_warning.py` (including
  a source-level guard that the helper is invoked at import time).

### Added — MCP audit token autogen on --upgrade path (#151)

- **Surgical patch helper `_patch_config_toml_internal_audit_token`**
  mirrors the embedding-dim helper: parse with tomllib, surgically
  insert into the `[server]` block, atomic `install -m` write
  preserving owner/group/mode. Idempotent — second call is a no-op
  when the token is already populated.
- **Wired into `--upgrade` flow** after the embedding-dim refresh.
  Brings legacy v5.3.4-era installs (where #148 added the env var
  but operators rarely set it) up to the #150 default-on posture
  without rewriting profile-derived defaults the operator may have
  tuned. Matches the same `MNEMOS_CONFIG_PATH` resolution semantics
  as the embedding-dim patch — patches the file the runtime reads.
- **Soft failure mode.** If the patch can't run (unreadable config,
  malformed TOML, atomic-write failure), the upgrade still
  succeeds and a warning explains how to hand-edit the token in.
  This is intentional: the upgrade itself completed, and the audit
  endpoint operates in legacy mode without the token — degraded
  but functional.
- **Honors `MNEMOS_INTERNAL_AUDIT_TOKEN` env** at upgrade time so
  operators can rotate the token as part of the upgrade.
- 11 tests in `tests/test_installer_audit_token_upgrade_patch.py`
  covering: insertion when missing, no-op when populated, replace
  empty/quoted placeholder, append section when missing, env-var
  rotation, preservation of unrelated [server] settings (port,
  profile, base IPv6 URL, workers), unparseable-config soft fail,
  missing-file soft fail, file-mode preservation, idempotency on
  repeated runs, source-level guard that the upgrade dispatcher
  invokes the patcher AFTER the embedding-dim patch.

### Added — MCP audit token autogen at install time (#150)

- **Installer auto-populates `[server].internal_audit_token`** in
  config.toml on first install (or re-install where the field is
  empty), via `secrets.token_hex(32)` (256-bit hex). The trust
  boundary on `/v1/internal/mcp_audit` is now default-on for new
  installs rather than requiring operators to manually set the env
  var first — without the autogen, most operators would leave the
  endpoint in legacy mode (any authenticated caller can POST audit
  rows).
- **Resolver priority**: `MNEMOS_INTERNAL_AUDIT_TOKEN` env (operator
  override / rotation) → existing token in the runtime-resolved
  config (honors `MNEMOS_CONFIG_PATH` so we read the same file the
  service reads, avoiding API/bridge token skew) → existing token in
  the in-memory config being patched → fresh `secrets.token_hex(32)`.
- **`_resolve_config_write_target` honors MNEMOS_CONFIG_PATH.** The
  writer now patches the file the runtime actually reads when the
  env var is set, instead of dumping the autogen token into a stale
  `repo_path/config.toml` the service will never load.
- **`_set()` regex hardened** with line-anchored section + key
  detection — `(?:^|\n)\[<sec>\]` for headers, `\n[ \t]*` consumed
  before the key (preserves indented TOML keys; rejects
  commented-out `# key = ...` lines because `\n[ \t]*#` followed by
  the key char fails). Also rejects mid-line `[` (IPv6 URL literals
  like `base = "http://[::1]:5002"` are content, not boundaries).
  Affects ALL fields the installer patches, not just
  internal_audit_token.
- **TOML-aware parsing + post-write validation.**
  `_read_existing_internal_audit_token` uses `tomllib` (stdlib,
  py3.11+) for the read side; the writer validates the patched
  content with `tomllib.loads()` AND asserts `parsed[server].
  internal_audit_token` is non-empty before replacing the file. A
  regex slip that lands malformed TOML or an empty key fails
  loudly with the original file unchanged.
- Wired into both `_write_config_toml` (existing config path) and
  `_render_minimal_config` (no-example minimal config). config.toml
  is already written with mode `0o600`, so the secret stays sensitive.
- 5 codex rounds, 6 commits (`748548e..8e8fac6`); 29 tests in
  `tests/test_installer_audit_token_autogen.py`; full suite 2080.

### Added — MCP audit Phase-D shutdown drain (#149)

- **Tracked audit tasks** in `_INFLIGHT_AUDIT_TASKS` set; each
  scheduled task is added on creation and removed via
  `add_done_callback` so the set always reflects in-flight writes.
- **`drain_pending_audit_tasks(timeout)` helper.** Called from all
  three transport teardowns: API FastAPI lifespan
  (`register_lifespan_cleanup_hook("mcp audit drain", ...)`), MCP
  stdio `main()` finally block, and MCP HTTP/SSE Starlette
  `on_shutdown=[...]`. Drain timeouts log a warning with the
  still-pending count but don't propagate — shutdown must complete.
- **Bounded backlog.** `_MAX_INFLIGHT_AUDIT_TASKS = 1024`. At the
  cap, `_schedule_audit_persist` refuses new schedules with a
  warning log so an audit-DB outage doesn't unbounded-grow the set.
  The Python logger entry is still emitted (always-on surface), so
  the call isn't a silent loss.

### Added — MCP audit `/v1/internal/mcp_audit` lockdown (#148)

- **`MNEMOS_INTERNAL_AUDIT_TOKEN` env** (`_ServerSettings.
  internal_audit_token`). When set, `/v1/internal/mcp_audit`
  requires `X-Mnemos-Audit-Token: <value>` matching the env
  (constant-time `hmac.compare_digest`). Any token holder who
  doesn't share the env can't POST audit rows. When unset, the
  endpoint operates in legacy bearer-token mode for phased rollout.
- **Bridge auto-includes the header.**
  `persist_audit_record_via_http` reads
  `settings.server.internal_audit_token` and adds the header when
  configured. Bearer auth on the underlying tool calls still
  establishes caller_user_id/role attribution; the new token is
  purely a trust-boundary gate.

### Added — MCP audit Phase-D durable surface (#146)

- **New `mcp_audit_log` table** (`db/migrations_v5_3_4_mcp_audit_log.sql`)
  with idempotent migration + sqlite parallel + `GRANT SELECT, INSERT
  TO mnemos_user` for installer-managed Postgres upgrades. Schema:
  `id, caller_user_id, role, tool, parameter_shape JSONB, outcome,
  error_class, created_at`. Outcome `CHECK` constraint covers
  `called/success/failure/error/denied/root_bypass`. Three indexes
  for the common operator queries (`created_at DESC`,
  `(caller_user_id, created_at DESC)`, `(tool, created_at DESC)`).
- **New `mnemos.db.mcp_audit_repo`** with three writers:
  `insert_audit_record(conn, ...)` (postgres-only, sqlite skipped
  like `deletion_log`), `persist_audit_record_via_pool` (in-process
  API path), `persist_audit_record_via_http` (standalone MCP bridge
  fallback against `/v1/internal/mcp_audit`). The combined
  `persist_audit_record` entry point tries pool first, falls back
  to HTTP — so both API-process and standalone bridge deployments
  persist transparently.
- **New internal route `POST /v1/internal/mcp_audit`** in
  `mnemos/api/routes/mcp_audit.py`. Bearer auth via existing MCP
  token mechanism. `caller_user_id` and `role` derived from the
  auth context (NOT from body fields) so bridges cannot forge
  attribution. Strict `_validate_parameter_shape` validator rejects
  raw values: closed allowlist for `type` and `item_types`
  (`str/bool/int/float/list/dict/none/bytes/tuple/set/frozenset/
  NoneType`), max 64 keys, max 16 item_types per entry, no nested
  dicts. The redaction guarantee documented for this table is
  enforced at the trust boundary, not just at the dispatcher.
- **Per-user MCP token preferred over global** in the http
  fallback. `MNEMOS_MCP_TOKENS=user:mcp_token:api_key` mode sets
  the per-call backend api_key in MCP context;
  `persist_audit_record_via_http` reads
  `current_mcp_backend_api_key()` first, falls back to
  `settings.server.api_key` for single-user / API-process
  deployments.
- **Root-bypass entries tagged.** `_mcp_log_root_bypass` writes
  with `outcome='root_bypass'`, supporting operator queries like
  `SELECT * FROM mcp_audit_log WHERE outcome = 'root_bypass'`
  for elevation-event review.
- **KNOWN_LIMITATIONS.md updated** to reflect Phase-D shipped with
  documented residuals: trust boundary on `/v1/internal/mcp_audit`
  (any token can POST self-attributed rows; needs bridge-only
  credential or mTLS in v5.3.5+) and untracked audit task on
  shutdown (stdio bridge can exit before HTTP POST completes;
  needs bounded lifecycle queue + drain in v5.3.5+).

### Added — MPF v0.2 deletion_log keyset cursor pagination (#142)

- **Composite index on `deletion_log (executed_at, id)`** for
  keyset performance (`migrations_v5_3_3_deletion_log_export_index.sql`).
- **Keyset cursor pagination** in
  `mnemos.domain.portability.export.export_memories`. Tuple
  comparison `(executed_at, id) > ($cursor::timestamptz, $id::uuid)`
  splits >50k tied-timestamp tombstone buckets that the round-141
  time-window pagination couldn't.
- **Self-contained opaque cursor** carrying keyset position
  (executed_at, id), snapshot anchor (`export_as_of` from DB-side
  `SELECT now()`, NOT app clock), original window
  (`deletion_log_from/to`, with explicit `null` for unbounded
  sides), and tenant scope (`effective_owner`, `effective_ns`).
  Subsequent pages derive window AND scope SOLELY from cursor;
  combining `deletion_log_cursor` with `owner_id`/`namespace`/
  `deletion_log_from`/`deletion_log_to`/empty-string-cursor is
  rejected at the route with HTTP 400.
- **Cursor anti-forgery for non-root callers.** Cursor scope is
  unsigned base64-JSON, so a non-root attacker could mint a cursor
  with a victim's owner/namespace. Non-root callers' cursor
  `effective_owner` and `effective_ns` MUST equal
  `user.user_id`/`user.namespace`, else HTTP 403. Root accepts
  cursor scope as canonical (root has explicit cross-tenant
  authority).
- **Scope binds ALL envelope surfaces.** Cursor decode happens
  BEFORE the read-only transaction, so records, KG triples,
  memory_versions, compression_manifest, AND deletion_log all use
  the cursor-bound scope on subsequent pages. Round-3 caught the
  earlier shape where only deletion_log used cursor scope.
- **Legacy pre-round-4 cursors rejected** with HTTP 400 ("restart
  pagination from page 1"). No silent fallback to request-derived
  scope (which the route also rejects).
- **Route guard:** `deletion_log_cursor` / `deletion_log_from` /
  `deletion_log_to` require `include_sidecars=true` AND
  `mpf_version=0.2`; otherwise 400 instead of silent no-op (which
  could let an operator's pagination loop terminate prematurely).
- **Late-commit caveat documented.** `executed_at <= export_as_of`
  is best-effort, not a true cross-call MVCC snapshot. A deletion
  transaction that begins before page 1 and commits after page 1
  has its row become visible on later pages, but its `executed_at`
  may sort before the cursor and be silently skipped. Operational
  mitigations (quiesce deletes, run pages back-to-back, cross-check
  by count) documented in `export_memories` docstring; full fix
  (held-snapshot via `pg_export_snapshot()` + materialized export
  jobs) roadmap'd for MPF v0.3.

### Changed — Postgres upgrade hardening (#138)

Codex-reviewed across rounds 17-49, closing 33 codex findings; the
v5.3.4 cut targets the common single-cluster local install + remote-
DSN reject paths.

- **`_resolve_runtime_backend` now mirrors runtime priority exactly**
  per pydantic-settings 2.10.1 semantics. `backend` field uses
  `validation_alias`, so env `PG_BACKEND` overrides init kwargs
  (TOML); empirically verified.
  `host`/`port`/`database`/`user` use `env_prefix='PG_'` with no
  validation_alias, so init kwargs (TOML) win over env. The
  installer's `_resolve_runtime_backend` and `_resolve_db_field_strict`
  reflect the per-field split.
- **`MNEMOS_CONFIG_PATH` honored** across resolver, loader, and
  post-migration patch+verify. Hardcoded `repo_path/config.toml` was
  replaced with `_resolve_runtime_config_path` that mirrors runtime
  `_config_paths` order. Operator-cwd `config.toml` is NOT a
  candidate (the installer runs in operator's shell, not the
  service's `WorkingDirectory=repo_path`), so a stray cwd config
  cannot shadow the actual installed service config.
- **`_psql_superuser` / `_psql_superuser_file` use `-v ON_ERROR_STOP=1`**
  so psql exits non-zero on the first SQL error in a `-c` or `-f`
  invocation. Previously psql could continue past non-fatal errors
  and exit 0, letting `run_migrations()` report 'OK' on partial
  schema drift.
- **`run_migrations` fails fast on the first failed migration file.**
  Migration order is documented as load-bearing; previous behavior
  set `success=False` and kept applying later files, leaving
  ambiguous schema drift. Combined with `ON_ERROR_STOP=1`, the
  first SQL error in any migration aborts the whole run.
- **Backend-aware `--upgrade` dispatch.** Profile-only dispatch
  (which round-19 introduced) was wrong because profile is a
  deployment-shape signal (where the workload runs), not a DB-
  backend signal (what the DB actually is). The new
  `_resolve_runtime_backend` is consulted before dispatch:
  `sqlite` → `setup_sqlite_database`, `postgres` → `run_migrations`.
  DSN/url-based configs (DATABASE_URL/MNEMOS_DATABASE_URL/PG_URL +
  DSN variants + [database].url/[database].dsn in TOML) are
  explicitly refused with 400; the runner is not DSN-aware.
- **`_is_local_postgres_host` narrowed.** Accepts only
  `None`/`""`/`"localhost"`. Explicit IPs (`127.0.0.1`, `::1`)
  rejected as TCP-intent that diverges from the socket-based
  migration runner. Whitespace-only and padded values
  (`" localhost "`, `"127.0.0.1\n"`) fail closed at both the
  loader (with a clear "trim the whitespace or fix the typo" error)
  and the locality guard.
- **Empty/malformed `PG_PORT`/`embedding_dim`/`backend` fail closed**
  with a `ValueError`, matching runtime Pydantic
  `ValidationError`. Distinguishes absent (None / empty string,
  fall through to default) from present-but-malformed (raise).
- **`run_migrations` validates `db_name` as a bare identifier**
  (Round-50 finding 2). Without `_validate_identifier`,
  `psql -d "host=10.0.0.5 port=5432 dbname=staging"` would have
  bypassed every host/port/DSN/url guard and connected to whatever
  the conninfo encoded.
- **Per-user `MNEMOS_PROFILE_OVERRIDE` env supported.** Mirrors
  runtime `_profile_from_sources` priority:
  `MNEMOS_PROFILE_OVERRIDE` > `[server].profile` >
  `[deployment].profile` > `MNEMOS_PROFILE` > backend/conn-field
  inference > legacy `personal` → `edge`. Installer CLI flag
  `--profile <p>` now sets `MNEMOS_PROFILE_OVERRIDE` so it wins
  over stale TOML.
- **Two residual round-50 limitations documented.** `localhost`
  with asyncpg uses TCP (resolves via DNS), while psql with no
  `-h` uses the socket — multi-cluster hosts can have these
  point to different DBs. Closing this needs a TCP-aware migration
  runner (out of scope for v5.3.4). And the existing residuals
  in KNOWN_LIMITATIONS.md (MCP write quota, GDPR write-fence)
  remain.

### Fixed — Migration list drift across installer/sqlite/docker-compose (#143, #146)

- 4 v5.1+ postgres migrations and 2 sqlite migrations had drifted
  away from EXPECTED_MIGRATIONS, EXPECTED_SQLITE_MIGRATIONS,
  docker-compose.yml, and docker-compose.staging.yml. All 4
  surfaces now sync; the regression test passes 7/7.
- `migrations_v5_3_4_mcp_audit_log.sql` (#146) appended to all
  surfaces in the same shape.

### Fixed — Connector gallery references (#144)

- Commit 5b92ab6 deliberately removed `docs/connectors/continue-dev.md`
  as orphan after v5.0.11 introduced `continue.md` as canonical.
  Four sites still referenced the old slug, breaking 2 connector
  smoke tests:
  - `tests/test_connector_smoke.py:43` (STDIO_SURFACES tuple)
  - `tests/test_connector_doc_configs.py:194` (EXPECTED_DOCS)
  - `tests/test_mcp_namespace_isolation.py:4` (module docstring)
  - `mnemos/mcp/tools/memory.py:32` (_ns_with_default helper docstring)
  All four updated to canonical `continue.md`.

### Added — Configurable SQLite embedding dim

- `_DatabaseSettings.embedding_dim` (default 768) reads `MNEMOS_EMBEDDING_DIM`
  or `PG_EMBEDDING_DIM`. Lets the same source build target multiple embedding
  models — 768 for nomic-embed-text (PYTHIA fleet default), 512 for
  bge-small-zh-v1.5 (Cix Sky1 NPU substrate), 1536 for OpenAI
  text-embedding-3-small, 3072 for text-embedding-3-large. Range: [1, 8192]
  per sqlite-vec `SQLITE_VEC_VEC0_MAX_DIMENSIONS`. Out-of-range values warn
  and fall back to 768.
- **Dim-mismatch is now fatal at startup, not silent degradation.** Both the
  vec0 virtual table (`memory_embedding_vec`) and the fallback shadow table
  (`memory_embeddings`) are checked on `SqliteBackend.open()`. If the existing
  dim differs from the resolved dim, a `RuntimeError` is raised with the
  exact `sqlite3` commands the operator must run to migrate. Refusing to
  start beats running new-dim queries against stale-dim rows and returning
  meaningless cosine scores.
- **Installer threads `embedding_dim` end-to-end.** `MNEMOS_EMBEDDING_DIM`
  read at install time is persisted to the generated `config.toml`
  (`[database] embedding_dim = N`) and to the systemd `EnvironmentFile`
  (`/etc/mnemos/mnemos.env` → `MNEMOS_EMBEDDING_DIM=N`), so subsequent
  service restarts run with the same dim the install was sized for. Without
  this, an install at 512 followed by a default-env service start at 768
  would trip the dim-mismatch guard. All three installer modes — `--unattended`,
  `--wizard`, and `--agent` — apply `MNEMOS_EMBEDDING_DIM` via the centralized
  `_apply_embedding_dim_from_env()` helper.
- **Runtime embedding-dim invariant.** `SqliteMemoryRepository.semantic_search()`
  and `upsert_memory_embedding()` now validate `len(embedding)` against the
  configured dim on every call and raise `ValueError` with actionable
  troubleshooting on mismatch. Without this, a misconfigured embedding
  endpoint after startup (e.g. swapped to a different model mid-flight)
  could poison the table or silently degrade search to "rank by recency"
  (cosine returns 0.0 on length mismatch).
- **Exhaustive fallback-dim scan at startup.** Replaced the single-row
  sample with `_scan_fallback_embedding_dims()` that builds a `{dim: count}`
  histogram across all `memory_embeddings` rows via
  `json_array_length()`. A DB poisoned before the runtime invariant landed
  could have mixed-dim rows; sampling could pick a matching row and hide
  the corruption. The startup guard now reports the exact stale-dim shape
  (e.g. `dim=768 x42`) in its error message and refuses to start until
  every row matches the configured dim.

### Added — Postgres embed-dim ALTER on fresh install

- `_alter_postgres_embedding_dim()` runs after migrations apply. When
  `cfg.embedding_dim != 768` it inspects the existing column type via
  `format_type(atttypid, atttypmod)` and:
  - Returns idempotent OK if the column already matches `vector(<dim>)`,
    even on populated tables. Re-running the installer with the same
    dim is a no-op.
  - On a fresh install with type mismatch, ALTERs the column to the
    target dim. Cheap (no rows to re-embed).
  - On a populated install with type mismatch, refuses with a
    Postgres-correct transactional recovery command (`BEGIN; UPDATE
    memories SET embedding=NULL; ALTER … TYPE vector(<dim>) USING NULL;
    COMMIT;`). The previous draft mistakenly referenced the SQLite-only
    `memory_embeddings` table — fixed.
- Capped at 2000 dimensions for pgvector ivfflat compatibility (the
  baseline `idx_memories_embedding` is ivfflat). Above 2000-D the
  installer fails closed — the previous version returned True and
  silently let the installer persist e.g. 3072 into config while
  leaving the DB at 768, breaking runtime cosine. Halfvec / no-ANN
  strategies for >2000-D dims are a separate scope.
- COUNT(*) safety gate fails closed on any error (psql rc != 0,
  unparseable output). The ALTER uses `USING NULL` which clears all
  embedding rows it rewrites — running it under uncertainty about the
  row count would silently destroy data on a populated DB.
- COUNT and ALTER run under a single ACCESS EXCLUSIVE table lock via a
  plpgsql DO block (`LOCK TABLE memories IN ACCESS EXCLUSIVE MODE; …;
  ALTER …`). Splitting them into separate sessions would race against
  any concurrent writer that inserts a row between our COUNT-returns-0
  and our ALTER-takes-its-lock — that row would be silently nulled.
  Refusal on populated DB happens via `RAISE EXCEPTION` inside the
  block; the wrapper detects the `MNEMOS_EMBED_DIM_REFUSE` marker and
  renders operator-facing recovery instructions.
- `--upgrade` now round-trips `embedding_dim` from `config.toml`. Before,
  `_load_existing_config()` skipped the field; an existing 512-D install
  would lose its dim on `--upgrade`, default back to 768, skip the
  ALTER entirely, and leave config + DB schema mismatched.
- `--upgrade` also re-persists `embedding_dim` to config.toml and the
  managed service env file after a successful migration. Without this,
  a one-shot `MNEMOS_EMBEDDING_DIM=512 ... --upgrade` against a 768-D
  config would ALTER the DB to 512 but leave config.toml + service env
  at 768; the next normal service start would then send 768-D queries
  against a 512-D pgvector column and fail at runtime.
- The post-migration persistence step is fatal-on-fail. The DB may
  already be at `vector(<new dim>)` when we try to update config.toml
  and `/etc/mnemos/mnemos.env`; if either write fails OR the config.toml
  read-back doesn't reflect the expected dim, `--upgrade` returns 1 with
  explicit manual-fix instructions. The previous best-effort behavior
  swallowed exceptions and ignored `_write_env_file()`'s False return,
  letting automation report a successful upgrade with mismatched config.
- `_load_existing_config()` now infers `profile=server` from a config.toml
  that has `[database].backend = "postgres"` or explicit non-default
  postgres connection fields, even when no `[server].profile` is set.
  The previous default-to-edge behavior would silently rewrite a postgres
  install as sqlite on next `_write_config_toml`, since edge/dev profiles
  imply sqlite. Explicit `[server].profile` still wins.
- New `_patch_config_toml_embedding_dim()` does a surgical regex update of
  only `[database].embedding_dim`. The `--upgrade` flow uses this instead
  of `_write_config_toml()` so it doesn't accidentally rewrite
  profile-derived defaults like `[database].backend`, `[rate_limit]
  storage_uri`, `[graeae] mode_default`, `[logging] level`, or
  `[compression] workers`. A model-swap upgrade preserves every other
  production setting verbatim.
- New `_patch_service_env_embedding_dim()` does the same surgical update
  for `MNEMOS_EMBEDDING_DIM` in the systemd `EnvironmentFile`. The full
  `_write_env_file()` would rebuild the file from `cfg.graeae_providers`
  (which `_load_existing_config()` doesn't repopulate on `--upgrade`),
  silently erasing `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
  `TOGETHER_API_KEY`, and any operator-managed lines. Surgical patch
  preserves all of them. The temp file is staged in the system temp dir
  (not in `dirname(env_path)`) so `tempfile.mkstemp` doesn't raise
  PermissionError on the root-owned `/etc/mnemos` shape; the patcher
  then tries `os.replace` and falls back to `sudo install` preserving
  uid/gid/mode of the existing file.
- `--upgrade` skips the env patch (instead of failing) when the systemd
  EnvironmentFile doesn't exist. Config-based upgrades can't tell the
  difference between a systemd install, a no-service install, and a
  non-Linux deployment — config.toml doesn't persist `cfg.create_service`.
  `MNEMOS_NO_SERVICE_ENV=1` provides an explicit opt-out for no-service
  shapes. Operators on launchd / no-service receive a clear note in
  stderr explaining how to set the env var manually if needed.
- The env-file replace path always uses `install -m -o -g` (preserving
  uid/gid/mode from the existing file), with sudo as fallback when
  permission denied. The previous direct `os.replace` from system temp
  would install the new file as `root:root` when run as root, locking the
  `mnemos` service group out, and would also raise `EXDEV` on hosts
  where /tmp is a separate filesystem.
- `--upgrade` handles a missing config.toml without unconditionally
  failing. For env-only / container-shaped deployments where there's no
  config.toml on disk, the upgrade verifies that `MNEMOS_EMBEDDING_DIM`
  in the environment matches the upgrade target dim and accepts. If they
  diverge, the installer refuses with explicit instructions to persist
  the env var in the launcher before retrying.
- `_patch_config_toml_embedding_dim()` mirrors the env-file patcher's
  ownership-preserving pattern. The previous `os.replace` from a
  0600-mode tempfile would install the patched config.toml as the
  running uid:gid with 0600 — breaking the `mnemos` service group's
  read access on a root:mnemos 0640 production shape. The fix reads
  uid/gid/mode from the existing file and goes through `install -m -o
  -g` (with sudo fallback) so the metadata is preserved verbatim.
- `_config_from_env()` now accepts both `MNEMOS_DB_*` (the installer's
  unattended-install convention) AND `PG_*` (what `service._write_env_file()`
  emits to the systemd EnvironmentFile, and what runtime config parses).
  Without the PG_* fallbacks, a container shape that mirrored the
  service env file would hit `--upgrade` with cfg defaults
  (localhost/mnemos/mnemos_user) and target the wrong DB.
  `_load_existing_config()` similarly falls back to env mode when either
  `MNEMOS_DB_PASSWORD` OR `PG_PASSWORD` is set, so an env-only deploy
  using just PG_* vars (no config.toml) is supported. MNEMOS_* aliases
  win when both shapes are set.
- `_patch_config_toml_embedding_dim()` now uses `tomllib` to validate the
  file is parseable TOML before patching, and re-validates the result
  after patching. The header-line regex tolerates leading whitespace and
  trailing inline comments (both valid TOML). If `tomllib` parses a
  `[database]` table that the regex can't safely span (e.g.,
  `[[database]]` array-of-tables), the patcher refuses with a clear
  error rather than appending a duplicate `[database]` section that
  would corrupt the file post-ALTER.
- Installer aborts (`return 1`) if `run_migrations()` returns False,
  preventing config.toml + service env from getting written at the
  configured dim while the DB schema stays at the old type.
- The base `db/migrations.sql` still defines `vector(768)` as the
  cold-path baseline; `_alter_postgres_embedding_dim()` is invoked
  unconditionally (idempotent — short-circuits when type already
  matches). The previous `embedding_dim != 768` short-circuit silently
  let an existing 512-D install downgrade to a "successful" 768 install
  while the column stayed at 512.

## [5.0.0] — 2026-05-02

Major release closing the v3.6 / v4.x charters and absorbing the
v4.2.0a14 alpha line. v5.0 is the first release that ships the
full divergent dream-state pipeline (REPLAY → CLUSTER →
CONSOLIDATE → SYNTHESISE → EXTRACT), the right-to-be-forgotten
worker, the archival subsystem, the unified LLM facade, and the
recall-pattern observability surface.

A PROTEUS barrage validated the release at 2500 concurrent
writes / 2000 reads / 200 search round-trips against a fresh
Postgres 17 deployment: 98.5% write success, 99.7% read success,
100% search success.

### Added — Subsystem modularization

- Optional subsystem extras: `morpheus`, `persephone`, `pantheon`,
  `kronos`, `knossos`, `apollo`, `artemis`, `nats`, and `hot`.
- Named bundles: `edge`, `server`, `ml`, `interop`, and `full`, so
  operators install deployment shapes instead of selecting every
  individual subsystem.
- Missing optional subsystems now fail closed with HTTP 503 install
  hints, MCP tools for missing subsystems are filtered from
  `tools/list`, and optional workers no-op cleanly when their extra
  is unavailable.
- Migration note: `pip install mnemos-os==5.0.0` is now core-only.
  Use `pip install 'mnemos-os[full]==5.0.0'` for the prior
  all-bundled behavior or `pip install 'mnemos-os[server]==5.0.0'`
  for production server deployments.

### Added — GDPR right-to-be-forgotten

- **Deletion-request lifecycle.** New ``deletion_requests`` table
  with status state machine (requested → confirmed → soft_deleted
  → restored | hard_deleted | cancelled). Admin endpoints under
  ``/admin/deletion_requests`` for create / cancel / confirm /
  restore / force-purge. Per-target advisory lock + active-row
  partial unique index prevent overlapping requests.
- **Soft-delete worker (Phase B).** ``deletion_request_worker``
  picks ``status='confirmed'`` rows, two-phase sweep
  (``confirmed`` → ``sweep_verifying`` → ``soft_deleted``) with
  ``SELECT FOR UPDATE SKIP LOCKED`` and 30-day restore window via
  ``restore_by``.
- **Hard-delete worker (Phase C).** Purges rows whose
  ``soft_deleted`` window has elapsed. Trigger-suppressed
  hard-delete via ``SET LOCAL mnemos.suppress_version_snapshot``
  so the audit chain isn't polluted with surrogate version rows.
- **Operational caveats** documented in ``KNOWN_LIMITATIONS.md``
  with recovery SQL for the rare race windows (final-verify
  race, sweep-verifying exhaustion under pathological concurrent
  writes).

### Added — MORPHEUS divergent dream-state, slices 3 + 4

- **CONSOLIDATE phase.** Merges near-duplicate clusters into a
  canonical with read-only pointers (``permission_mode=0o400``)
  on the originals. New ``consolidated_into`` column on
  ``memories``. Soft-only: never hard-deletes user data.
  Federation-aware: peers receive a consolidation tombstone via
  the federation feed (see consolidation event type). Opt-in via
  ``MNEMOS_MORPHEUS_CONSOLIDATE``.
- **EXTRACT phase.** LLM mining of latent KG triples from
  ``verbatim_content`` of prose memories not yet triplified.
  Two-model split: fast/quantized for raw extraction, optional
  strong reasoner for verification (gated on ``extract_verify``).
  Results land in ``kg_triples`` with ``extracted_by_run_id``
  provenance. New ``triples_extracted_at`` idempotency column on
  memories. Opt-in via ``MNEMOS_MORPHEUS_EXTRACT``.

### Added — PERSEPHONE archival subsystem

- **Cold-set rotation.** ``memory_archive`` table holds
  zstd-compressed full payloads; live ``memories`` row keeps a
  stub-pointer (content cleared, ``archived_at`` set) so the
  primary table stays small while preserving identity.
- **Sweep + restore.** ``persephone_archival_worker`` and
  ``/admin/persephone/{sweep,archive/{id},restore/{id},status}``
  endpoints. Eligibility: not consolidated, not deleted, last
  recall ≥ M days ago. Archived rows hidden from search and list
  by default; ``?include_archived=true`` opts in (root-only).
- **Federation-aware.** Peers see the archival event via the
  existing version trigger.
- Operator-gated by ``MNEMOS_PERSEPHONE_ENABLED``.

### Added — PANTHEON + IRIS

- **PANTHEON v0.1 scaffold.** Unified LLM facade in front of the
  GRAEAE muses registry. ``/pantheon/v1/{models,
  chat/completions, embeddings, route/explain}`` OpenAI-compat
  routes. Auto-populated catalog from existing muses; alias
  prefix resolver (``auto:reasoning``, ``auto:cheap``,
  ``auto:fast``, ``consensus:<task_type>``); per-model
  ``usage_tier`` metadata.
- **PANTHEON v0.2.** Per-(user, session) hard caps on
  ``consultation_only`` tier (configurable via
  ``MNEMOS_PANTHEON_CONSULTATION_CAP``); MNEMOS routing-log writes
  for every routing decision (``category=pantheon_routing``,
  ``namespace=pantheon``); rolling-window adaptive routing policy
  that scores backends by latency / error / cost from the recent
  routing-log. ``/pantheon/v1/route/explain`` returns the full
  resolution chain with per-candidate scores.
- **IRIS MCP tools.** ``pantheon_list_models`` and
  ``pantheon_route_explain`` tools registered on the canonical
  MCP surface, gated by the same V4 §6.4 security checks.
- Disabled by default (``MNEMOS_PANTHEON_ENABLED=false``); 503
  with profile-aware message when off.

### Added — KRONOS v0.1

- **Recall-pattern anomaly detection.** Z-score over
  ``memories.recall_count`` history per memory. Flags spikes
  (``trending``) and drops (``eligible_for_persephone``).
- **Namespace drift.** Compares last 7-day recall volume against
  prior-30-day baseline.
- **Recall-load forecasting.** EWMA over hourly recall buckets
  predicts next-window load with 95% CI.
- **PERSEPHONE eligibility forecast.** Predicts how many
  memories become archive-eligible in the next N days.
- New ``/admin/kronos/{anomalies,drift,forecast}`` routes;
  ``tool_kronos_anomalies`` and ``tool_kronos_forecast`` MCP
  tools. Operator-gated by ``MNEMOS_KRONOS_ENABLED``.
- v0.1 is CPU-only via numpy. GPU integration via Tesseract
  deferred to v5.1.

### Added — DAG wiring for compression derivations

- Each successful compression contest now persists a child row
  in ``memory_versions`` parented to the source memory's
  ``branch='main'`` HEAD, on ``branch='distilled'`` (raw
  compression artifact) or ``branch='narrated'`` (prose).
  ``change_type='compress'`` extends the existing CHECK
  constraint; commit hash is content-derived (sha256 over
  parent + variant + branch). Compressed artifacts are now
  walkable from the original memory's version history.

### Added — Rust hot-path completion (mnemos_hot v0.2)

- **Deterministic judge scoring.** Compression fidelity can use the
  Rust bigram-overlap / Levenshtein / length-ratio scorer, with the
  Python scorer preserved as the fallback and source of truth.
- **Embedding L2-normalize batch.** MORPHEUS and compression
  embedding helpers dispatch batch normalization to Rust when
  ``MNEMOS_HOT_RS_ENABLED=1``.
- **Composite search re-rank.** Postgres semantic search can opt into
  vector + decayed-recency reranking via ``boost_recency`` while the
  default pgvector ordering remains unchanged.
- **SHA-256 batch hashing.** Compression DAG commit-hash payloads use
  the Rust batch helper when enabled, falling back to ``hashlib``.
- Parity invariant: every Rust accelerator is opt-in and covered by
  Python-fallback parity tests with float tolerance at ``1e-9``.

### Added — NATS substrate v0.2

- Bounded second slice. PANTHEON routing-log → NATS publish to
  ``mnemos.pantheon.routing`` (opt-in via
  ``MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING``). New
  ``pantheon_routing_audit`` table fed by an optional consumer
  worker. Webhook outbox migration and federation rewire remain
  deferred to substrate v0.3.

### Added — MCP §6.4 security gates

- Cross-cutting audit + hardening across all 22 MCP tools:
  parameter-shape audit log (no raw values), per-tool rate
  buckets honouring ``mnemos.core.rate_limit``, role + namespace
  validation in the dispatcher, root-bypass logged as a warning,
  and uniform error normalization (``Resource not found`` /
  ``Invalid tool input`` / ``Rate limit exceeded`` /
  ``Tool execution failed``) so error shapes don't leak ownership
  data.
- ``_safe_path_segment`` and ``_safe_path_value`` raise generic
  errors that don't echo the offending value or the regex
  pattern.
- ``_rest_delete`` now ``raise_for_status`` (was silent on 4xx).
- Tool surface count includes the four v5 additions:
  ``pantheon_list_models``, ``pantheon_route_explain``,
  ``tool_kronos_anomalies``, and ``tool_kronos_forecast``.

### Added — Document import retry-safety

- ``import_chunk_key`` content-derived idempotency key prevents
  duplicate chunk insertion on retry; ON CONFLICT (key) DO
  UPDATE returns the canonical row id. Per-chunk transaction
  isolation ensures partial failures don't poison the import.

### Added — Connector documentation + smoke

- End-to-end smoke per surface (``tests/test_connector_smoke.py``):
  spawns the configured stdio MCP transport, sends ``tools/list``,
  validates the canonical tool registry returns, exercises
  ``search_memories``. ChatGPT HTTP/SSE smoke uses a mock REST
  backend.
- Documentation gallery covers all 8 connector surfaces with
  mechanically-validated JSON snippets.

### Added — Documentation

- ``docs/RFC-002-REENGAGEMENT.md`` — re-engagement memo for the
  MemPalace working group framing MNEMOS as a contributor to
  MIF rather than a competing format.
- ``docs/papers/mnemos-dag-distillation.md`` — design paper
  draft. Git-like DAG + LLM-synthesized distillation/narration
  + judge-verified fidelity.

### Operational

- The PROTEUS barrage exposed long-tail latency under sustained
  50-concurrent writes (p99 ~33s) — a v5.1 optimization target.
  Search and read paths held up well (search p99 ~300ms; reads
  p50 ~120ms with the same long tail under contention).
- All migrations apply cleanly on a fresh Postgres 17 + pgvector
  install. ``mnemos_proteus_test`` was provisioned with all v4.2
  migrations through ``migrations_v4_2_pantheon_routing_audit.sql``
  in a single ``run_migrations`` call.

## [4.2.0a14] — 2026-05-01

Pre-release alpha consolidating five overnight waves of work
between 2026-04-30 and 2026-05-01: NATS multi-replica federation
+ webhook receivers, partial-outage test infrastructure, latent
bug fixes (RLS SQL, pool acquire timeouts), Accept-header content
negotiation, MCP get_memory format parameter, /metrics auth gate,
path-traversal hardening across MCP tools, TimeoutPool wrap, and
an OpenAPI-export CLI for Custom GPT / Actions consumers.

A single ``v4.2.0a14`` tag bundles work from a2 through a14;
intermediate alphas were tagged but not released. Release notes
group by area rather than by intermediate version.

### Added

- **NATS multi-replica receivers (`a8`)**: queue-group sharding
  on JetStream consumers so federation + webhook receivers scale
  horizontally without duplicate processing.
- **Live-broker integration test harness (`a9`,
  `tests/integration_nats/`)**: ``ManagedBroker`` fixture spawns
  a real ``nats-server`` per test, with ``--server_name=<uuid>``
  identity check that rejects port-race squatters; partial-
  outage tests drive the production ``consumer_loop`` reconnect
  + backoff path against true broker restarts. Skips cleanly
  when no ``nats-server`` binary is present.
- **Federation feed compressed-variant payloads (`a14`)**: new
  ``GET /v1/federation/feed?prefer_compressed=true`` query
  param emits the winning ``memory_compressed_variants`` row in
  place of raw content when the variant is strictly smaller on
  the JSON wire (gated via Postgres ``to_json(text)::text``
  exact-byte measurement). Bytes-on-wire reduction up to 4–6×
  for variant-bearing memories. v3.6 charter §2.5 surface 1.
- **Accept-header content negotiation on
  ``GET /v1/memories/{id}`` (`a14`)**:
  - ``Accept: text/plain`` → prose narration body (same as
    ``/v1/memories/{id}/narrate?format=prose``).
  - ``Accept: application/x-apollo-dense`` → raw winning-
    variant content (same as ``?format=dense``).
  - Default (``application/json``, ``*/*``, missing) → existing
    JSON ``MemoryItem``.
  ``Vary: Accept`` set on every representation so caches do not
  conflate types. Both branches honour the same
  ``VisibilityFilter.for_read`` contract — federated / world /
  group-readable memories are returned identically across
  Accept values. v3.6 charter §2.5 surface 2.
- **MCP ``get_memory`` ``format=prose|dense`` (`a14`)**: stdio
  + HTTP-SSE MCP clients can request the compressed variant
  representations through the same content-negotiation paths
  the HTTP API exposes. v3.6 charter §2.5 surface 3.
- **``MNEMOS_METRICS_REQUIRE_AUTH`` (`a14`)**: optional Bearer-
  token gate on ``/metrics``. Default off (network-scope
  convention preserved). When enabled, the request must carry
  a valid Bearer token from an ``api_keys`` row whose owning
  user has ``role='root'``; non-root keys return 403,
  unknown / revoked keys return 401, and a settings-read
  failure fails closed (503).
- **``mnemos dump-openapi`` CLI (`a14`)**: emits the FastAPI
  OpenAPI spec without booting the server. ``--output PATH``,
  ``--indent N`` (0..8), ``--title T`` overrides, and
  ``--target {full|gpt-actions}``. The ``gpt-actions`` target
  truncates endpoint summary/description fields to 300 chars
  and parameter description fields to 700 chars per OpenAI's
  Custom GPT Actions production limits, so the artifact
  imports cleanly into a Custom GPT or OpenAI Actions bridge.
  v4.1 connector deliverable.
- **``mnemos doctor`` CLI (`a13`)**: pure-stdlib accelerator
  detection (NVIDIA CUDA / Tegra, Intel iGPU, Apple Silicon)
  that names the recommended ``mnemos-os[ml|gpu|phi]`` extra.
- **Connector documentation gallery (`a13–a14`)**: per-surface
  Markdown guides (``docs/connectors/{claude-code,cursor,
  codex-cli,continue-dev,cline}.md``) with mechanically-
  verified config snippets. Canonical MCP tool surface table
  in the README enumerates 22 tools with R/W classification,
  including ``pantheon_list_models``, ``pantheon_route_explain``,
  ``tool_kronos_anomalies``, and ``tool_kronos_forecast``,
  and the ``kg_``-prefix asymmetry called out (``kg_create_triple``
  has the prefix; ``update_triple`` / ``delete_triple`` do
  not). The ``mnemos_`` UI prefix some agents add (Cursor's
  tool drawer) is documented as display-only.
- **Memory architecture design paper
  (``docs/MEMORY_ARCHITECTURE.md``, `a13`)**: 3000-word
  description of identity, provenance, version DAG,
  compression/synthesis, federation, persistence, and
  observability.
- **Operator observability guide
  (``docs/OBSERVABILITY.md``, `a13`)**: Prometheus scrape
  config, the live metric set, optional metrics auth, and the
  shipped Grafana dashboard
  (``docs/observability/grafana/mnemos-overview.json``).
- **TimeoutPool proxy wrap (`a14`)**: wraps the asyncpg pool
  at lifecycle creation so the 86+ legacy ``_lc._pool.acquire()``
  call sites inherit ``DEFAULT_ACQUIRE_TIMEOUT`` uniformly,
  without per-site migration. The distillation worker's pool
  also routes through ``wrap_pool_with_timeout`` at start +
  reconnect.
- **``mnemos.core.pool.is_infrastructure_error`` predicate
  (`a14`)**: distinguishes pool / connection-loss class
  errors (asyncio.TimeoutError + asyncpg connection family)
  from content / processing failures, used by the compression
  worker's broad-except handlers to avoid converting pool
  pressure into terminal MARK_FAILED rows.
- **Compression queue infra-retry semantics (`a14`)**: when
  the worker hits a pool / connection error, the affected
  rows are reset to ``status='pending'`` with ``attempts``
  decremented (``GREATEST(attempts - 1, 0)``) and an
  ``error='infra_retry: ...'`` breadcrumb. The stale-running
  sweep refuses to terminalize rows whose ``error`` is
  ``NULL`` or starts with ``infra_retry:`` regardless of
  ``attempts``, so sustained pool pressure cannot terminalize
  a content-OK row. ``counts['infra_errors']`` is the new
  telemetry bucket.
- **MNEMOS_DEFAULT_NAMESPACE write-stamp (`a13`)**: MCP
  create/search/list/bulk tools stamp the configured
  namespace on writes. Documented as a write-stamp / search-
  filter ergonomic, NOT enforced isolation; root keys cross.
- **Profile-aware ``require_postgres_pool_or_503`` helper +
  67-site migration sweep (`a14`, rounds 53-60)**: every
  Postgres-only route now emits a profile-aware 503 detail
  that names the route AND tells operators to set
  ``MNEMOS_PROFILE=server`` (or ``MNEMOS_PERSISTENCE_BACKEND
  =postgres + a working PG_*``) to enable the route. The old
  bare ``Database pool not available`` detail conflated
  "this route is Postgres-only-by-design" with "the pool is
  transiently down" — operators on edge profiles chased
  phantom outages. 67 call sites across 14 modules
  (journal, ingest, sessions, portability, providers, oauth,
  dag, document_import, kg, webhooks, versions,
  consultations, memories, admin, federation) migrated onto
  the canonical helper. ``oauth_me`` keeps a non-raising
  ``if cookie_session and _lc._pool:`` fallback because it
  short-circuits to a personal/api-key response rather than
  raising; documented in
  ``tests/test_postgres_only_503_invariant.py``.
- **AST invariant pins the bare-503-shape (`a14`,
  round-61)**: ``tests/test_postgres_only_503_invariant.py``
  bans the bare ``if not _lc._pool: raise HTTPException(503,
  "Database pool not available")`` shape across
  ``mnemos/api/routes/`` and asserts ``require_postgres_pool
  _or_503`` is called from ≥20 sites — a future regression
  to the bare shape trips a unit test before code review.
- **Document-import transactional outbox + HTTP status
  surfacing (`a14`, rounds 47-50)**: per-chunk ``async with
  conn.transaction():`` wraps the memory INSERT and
  ``_dispatch_webhook(conn=conn)`` so the delivery row joins
  the same transaction (corpus-review-2026-04-29 #2 closure).
  Single-file ``POST /v1/documents/import`` returns 207 Multi-
  Status when ``errors`` is non-empty + 502 Bad Gateway when
  every chunk failed on a retryable infra fault; multi-file
  ``POST /v1/documents/batch-import`` aggregates per-file
  ``status_code`` into a top-level 207 (mixed) or 502
  (every per-file ``status_code == 502``) so HTTP-status-
  only clients see partial / full failure even when the
  body is JSON.
- **AST invariant: every dispatch call passes ``conn=``
  (`a14`, round-52)**:
  ``tests/test_dispatch_outbox_invariants.py`` AST-walks
  every call to ``mnemos.webhooks.dispatcher.dispatch``
  (and aliased imports) and fails if any call is missing
  the ``conn=`` keyword — the transactional-outbox guarantee
  that the delivery row joins the caller's transaction.
- **``mnemos dump-openapi --server-url`` (`a14`,
  round-51)**: Custom GPT / OpenAI Actions consumers can
  now bake the deployment hostname into the spec at export
  time (``servers: [{url: ...}]``) rather than patching by
  hand post-export. ``--target gpt-actions`` deep-copies the
  cached FastAPI ``app.openapi()`` dict before mutation so
  the per-CLI-invocation server override doesn't bleed into
  pytest fixtures.
- **OpenAI Custom GPT connector doc (`a14`, round-42)**:
  ``docs/connectors/openai-custom-gpt.md`` covers Custom GPT
  Actions setup against ``mnemos dump-openapi --target
  gpt-actions``: spec generation, Bearer-auth wiring,
  endpoint description / parameter description limits.
- **Claude Desktop connector doc (`a14`, round-44)**:
  ``docs/connectors/claude-desktop.md`` fills the previously
  broken README link with stdio + HTTP/SSE recipes.
- **Connector-doc config validation tests (`a14`,
  round-46)**: ``tests/test_connector_doc_configs.py``
  mechanically parses every fenced JSON block in
  ``docs/connectors/*.md`` and fails the suite on a config
  that won't parse. Surfaced two pre-existing broken-JSON
  bugs in ``continue-dev.md`` and ``claude-desktop.md``
  (placeholder ``...existing...`` / ``... ...`` syntax that
  never parsed); both fixed in the same commit.
- **MNEMOS_NODE_NAME hostname-fallback warning (`a14`,
  round-39)**: when ``MNEMOS_NODE_NAME`` is unset and the
  NATS connect helper falls back to ``socket.gethostname()``,
  one WARNING line is logged the first time so operators
  see the fallback explicitly. Subsequent connects stay
  silent (one-shot ``_NODE_NAME_FALLBACK_LOGGED`` flag).
  NATS-corpus-review-V4.2 finding #9 closure.
- **NATS payload sensitivity + ACL guidance
  (``docs/NATS_OPERATIONS.md``, `a14`, rounds 40-41,
  corrected round-45)**: per-subject sensitivity table
  documents that JetStream nudges carry only memory IDs
  (not bodies); bodies are fetched via authorized HTTP feed.
  Sample ACL configs for ``mnemos-server`` (pub/sub) vs
  ``mnemos-observer`` (subscribe-only) vs federation peer
  scope-tight pattern. NATS-corpus-review-V4.2 findings
  #10, #11.

### Fixed

- **document_import retry-safety arc (`a14`, rounds 62..68)**:
  the round-54..60 503-helper sweep dropped a stub route_label
  on ``import_memories_from_document``; codex caught it in
  round-61 review, then surfaced six progressively-deeper
  problems over rounds 62..67 before round-68 closed the loop
  with a real schema-level idempotency primitive. The full arc:
  - **round-62**: per-caller ``route_label`` (single-file vs
    batch); batch endpoint pre-loop pool check so SQLite/edge-
    profile 503s escape uncaught with the correct top-level
    status. Pool check precedes Docling-availability check.
  - **round-63**: aggregator surfaces top-level 503 if ANY
    per-file is 503 (so a mid-batch pool drop doesn't hide
    behind a 207 body). Helper wraps acquire to convert
    asyncpg/asyncio.TimeoutError to HTTPException(503,
    route_label).
  - **round-64**: helper returns ``(payload, 503)`` preserving
    committed-chunks ``memory_ids`` on infra failure instead of
    raising bare HTTPException(503).
  - **round-65**: ``unconfirmed_memory_ids`` field surfaces
    in-flight chunk IDs whose INSERT was accepted but commit-
    ack was lost. Retry-aware clients query
    ``GET /v1/memories/{id}`` to reconcile.
  - **round-66**: documentation revised — a single 404 on the
    reconciliation read is NOT a safe rollback oracle under
    Postgres MVCC; three operator-honest retry options
    documented in DOCUMENT_IMPORT_GUIDE.md.
  - **round-67**: deferred-primitive section corrected —
    ``ON CONFLICT (...) DO NOTHING RETURNING id`` returns 0
    rows on conflict, NOT the existing row's id. Two viable
    shapes (DO UPDATE no-op SET vs two-step INSERT-then-SELECT)
    documented with their trade-offs.
  - **round-68**: ships the full primitive — migration
    ``migrations_v4_2_document_import_chunk_idempotency.sql``
    adds ``import_chunk_key`` (sha256 of owner_id+namespace+
    source_file+chunk_num with NUL separators) and a partial
    UNIQUE index. Helper switches to ``ON CONFLICT
    (import_chunk_key) DO UPDATE SET import_chunk_key =
    EXCLUDED.import_chunk_key RETURNING id`` and trusts the
    RETURNING value as the canonical id. Postgres serializes
    the conflict path against the prior in-flight transaction,
    so commit-ambiguous retries are now safe — ``new_memory_id
    ()`` remains the surrogate but the canonical id is whatever
    came back from RETURNING. The no-op SET fires the AFTER
    UPDATE trigger, but ``mnemos_version_snapshot()`` only
    writes a new ``memory_versions`` row when audited fields
    are IS DISTINCT — ``import_chunk_key`` is not in that
    audited set, so retry-conflicts produce zero version-row
    churn.
- **LATENT BUG: ``SET LOCAL <name> = $1`` SQL on RLS-enabled
  Postgres (`a14`)**: PostgreSQL ``SET`` syntax does NOT
  accept bind parameters (per the official docs). The
  ``maybe_set_pg_rls`` helper and the parallel ``_rls_context``
  in ``mnemos/api/routes/memories.py`` had been using this
  shape since at least v3.0. The bug was latent because the
  live deployment runs ``MNEMOS_RLS_ENABLED=false``; the day
  someone flipped RLS on, every authenticated read would have
  500'd with a Postgres syntax error before the protected
  query ran. Both call sites now use ``SELECT
  set_config('<name>', $1, true)``.
- **Path-traversal across MCP / Knossos / KG tool surfaces
  (`a14`)**: caller-controlled ``memory_id`` / ``commit_hash``
  / ``triple_id`` / ``drawer_id`` / ``subject`` values
  spliced into REST paths could escape the
  ``/v1/memories/`` (and similar) prefix via httpx dot-
  segment normalization. With the new ``_rest_get_text``
  helper returning raw text, this widened to an
  exfiltration vector for any text endpoint
  (e.g. ``/metrics``). Validation + URL-encoding helpers
  ``_safe_path_segment`` (strict alphanum + ``_:-`` whitelist
  for IDs; admits documented federated id grammar
  ``fed:<peer>:<remote>``) and ``_safe_path_value`` (looser
  whitelist for free-form fields like KG entity names; rejects
  ``..`` traversal + URL-rewrite chars) applied at every
  splice site across ``mnemos/mcp/tools/{memory,dag,kg}.py``
  and ``mnemos/tools/knossos_mcp.py``.
- **Compression worker turning pool pressure into terminal
  failed rows (`a14`)**: pre-fix, every post-dequeue
  ``Exception`` ran ``MARK_FAILED``. After the round-28
  TimeoutPool wrap, asyncio.TimeoutError reached that
  handler and converted transient pool pressure into
  permanent failed compression rows. Eight-round
  iterative fix split infrastructure errors from content
  errors, reset un-processed batch tails before re-raising,
  unified all post-dequeue infra exit paths through one
  tail-reset site, and rewrote the stale-running sweep to
  refuse terminalization without a recorded content-error
  breadcrumb.
- **MORPHEUS / DAG endpoints (`a13`)**: cross-namespace
  telemetry leak on MORPHEUS read endpoints and DAG read /
  visibility skew with memory CRUD already-fixed in
  ``v4.1.3``; v4.2.0a14 verifies the pre-existing fixes.
- **Federation peer URL validation aligned with webhook SSRF
  policy (`v4.1.3`)**: peer registration runs through the
  same private-IP / metadata-host validator as webhooks.
- **Connector documentation honesty (`a12–a13`)**: 5 connector
  doc files refactored after 12+ rounds of codex review
  caught doc-overstating-code patterns: enforced isolation
  (which is not enforced; docs now say "write stamp"); per-
  key ``default_namespace`` (no such column; namespace lives
  on ``users``); CLI shape (real CLI is
  ``mnemos serve mcp-stdio``, not ``mnemos mcp serve --stdio``;
  endpoint is ``/sse``, not ``/v1/mcp/sse``); SSH inline-env
  caveat (``env VAR=val cmd`` is not shell-safe for tokens
  with metacharacters); MCP tool-name registry asymmetry
  (kg_create_triple has the prefix; update_triple /
  delete_triple do not — autoApprove takes the bare name).
- **fastembed semantic-similarity scoring (`a12`)**: the
  ``QualityAnalyzer`` previously called ``model.encode()``
  on fastembed which silently returned 85.0 for every pair.
  Switched to ``model.embed([text1, text2])`` returning an
  iterator of ndarrays. Added a ``-1.0`` sentinel for failed
  embeddings + a ``HEURISTIC_ONLY_CAP=70`` so high-trust
  task types cannot auto-approve from heuristic-only signal.
- **Heuristic compression auto-approve floor (`a12`)**:
  approve threshold dropped from 100 to 70 when no semantic
  signal is available, so a memory cannot reach
  ``approved`` purely on heuristics.

### Changed

- **psycopg dropped from default install (`a12`)**: psycopg's
  LGPL-licensed transitives don't fit MNEMOS's
  Apache/MIT/BSD/MPL closure. asyncpg-only by default;
  installer's ``create_api_key`` falls back through asyncpg
  → psycopg → psycopg2 → ``psql`` CLI when the optional
  shim is installed.
- **psutil + spacy removed from default deps (`a12`)**: zero
  imports across ``mnemos/``; both unused since the v4.0
  refactor.
- **torch removed from required deps (`a12`)**: heavyweight
  ML deps moved behind opt-in extras
  (``mnemos-os[ml|gpu|phi]``); fastembed (Apache-2.0,
  ~20MB) replaces sentence-transformers (which depended on
  torch). ``mnemos doctor`` recommends the right extra
  per host accelerator.
- **/narrate endpoint visibility (`a14`)**: lifted to
  ``VisibilityFilter.for_read`` so federated / world /
  group-readable memories render identically to the JSON
  ``GET /v1/memories/{id}`` path. RLS context (``SET LOCAL
  ...``) applied inside the transaction to match the JSON
  path's defense-in-depth.

### Operational

- **No-op for v4.1.3 deployments**: every change in this
  alpha is additive or replaces internal mechanism without
  changing the existing on-the-wire contract. The federation
  feed ``prefer_compressed`` query, MCP ``format`` parameter,
  Accept-header dispatch, and dump-openapi CLI are all
  opt-in.
- **Variant write-time wire-byte measurement
  (``federation_feed``)**: the byte gate uses
  ``2 * octet_length(to_json(v.compressed_content)::text)``
  vs ``octet_length(to_json(m.content)::text) +
  COALESCE(octet_length(to_json(m.verbatim_content)::text), 0)``.
  ``to_json(text)::text`` returns the exact JSON-escaped
  serialization Postgres emits on the wire, so the gate is
  conservative without false positives on control-character-
  heavy content.

## [4.2.0a1] — 2026-04-30

NATS JetStream substrate alpha — first slice of the v4.2 MQ work
chartered in `project_mnemos_graeae_mq_design.md`. Additive only:
existing webhook outbox remains the durable delivery path.

### Added

- `mnemos/nats/` package: `connect_nats`, `ensure_streams`,
  `publish_event`, `get_jetstream`. Fail-open — if NATS is
  unreachable or `MNEMOS_NATS_URL` is unset, publishing is a silent
  no-op.
- `MNEMOS_MEMORY` JetStream stream declared on startup. Subjects
  `mnemos.memory.created.<namespace>`, `…updated.…`, `…deleted.…`.
  File-backed, 30-day retention, 10 GB cap, 2-min duplicate window.
- `MNEMOS_NATS_URL` + `MNEMOS_NATS_TOKEN` settings (typed
  `_NatsSettings`).
- `memory.created` events now publish to NATS in addition to the
  transactional webhook outbox. `Nats-Msg-Id` header set to
  `<memory_id>.created` for idempotent re-publishes.
- Hatchet workflow-engine integration deferred to v4.2.0a2.

## [4.1.3] — 2026-04-29

Corpus-review hardening release.

### Fixed

- Pinned webhook delivery DNS resolution from validation through HTTP connect to close DNS-rebinding SSRF.
- Moved consultation completion and DAG live-merge webhooks into the transactional outbox path, with delivery scheduled only after commit.
- Released GRAEAE provider concurrency slots in `finally` during cancelled fan-out.
- Marked sessions, entities, state, and MORPHEUS HTTP routes as Postgres-only on edge profiles with explicit 503 responses.
- Restricted MORPHEUS run telemetry reads to root/operator callers.
- Migrated route-level asyncpg acquires to `PoolManager.acquire()`.
- Aligned DAG read preflight with memory read visibility while keeping branch/merge writes strict-owner scoped.
- Applied webhook SSRF URL validation to federation peers; private peer URLs require `FEDERATION_ALLOW_PRIVATE=true`.
- Added typed `AuthSettings` and server-profile fail-closed auth defaults via `MNEMOS_AUTH_ENABLED`.
- Made SQLite duplicate explicit memory IDs raise `DuplicateMemoryError` instead of silently succeeding.

## [4.1.2] — 2026-04-29

GRAEAE provider-routing fix + container-env operations standard.

### Fixed

- `mnemos.domain.graeae.engine._ranked_candidates` tiebreak ordering
  added an explicit non-reasoning preference between `last_synced` and
  `len(model_id)`. Before this fix, the `len()` fallback accidentally
  promoted `-reasoning` SKUs (shorter names) over `-non-reasoning`
  siblings of equal weight/version, so xAI Grok consultations came
  back tagged with `\confidence{N}` blocks instead of clean text.
  Provider helper `_is_reasoning_variant(model_id)` formalizes the
  classification.
- New regression suite at `tests/test_graeae_ranked_candidates.py`
  covers the helper + the tiebreak ordering.

### Operational

- v4.x container env standard documented: every `mnemos serve`
  container MUST mount `~/.api_keys_master.json` →
  `/etc/mnemos/api_keys.json` (read-only) AND set
  `MNEMOS_KEYS_PATH=/etc/mnemos/api_keys.json`. The v4.1.1 cutover
  surfaced that without these, GRAEAE quietly falls back to
  empty-key/no-provider state and every consultation 401s.
- Pre-existing reasoning-variant rows in `model_registry` should be
  marked `deprecated=true` for Grok-family providers via:
  `UPDATE model_registry SET deprecated = true WHERE provider = 'xai'
   AND model_id ~ '-reasoning$' AND model_id NOT LIKE '%non-reasoning'`.
  v4.1.2 fleet rollout includes this UPDATE on PYTHIA + CERBERUS
  before container restart.

## [4.0.0] — 2026-04-29

Major refactor + multi-backend persistence + multi-worker support release.

### Added

- Persistence abstraction (`PersistenceBackend` ABC) plus SQLite implementation
  using sqlite-vec / FTS5 / JSON1 / WAL.
- Deployment profiles: server (Postgres + Redis + multi-worker), edge
  (SQLite single-worker), dev (SQLite + DEBUG).
- Multi-worker support via Redis-backed circuit breaker / rate limiter /
  concurrency limiter; in-process fallback preserved.
- Single-binary distribution via pyinstaller for linux-x86_64,
  linux-aarch64, macos-aarch64 with sqlite-vec bundled.
- Unified `mnemos` CLI: serve / install / worker / export / import /
  consult / health / version.
- 7 import-linter contracts enforce package boundaries in CI.
- Pydantic Settings singleton replaces 105 ad-hoc `os.environ.get` calls;
  CI bans `os.environ` outside `core.config` + `installer`.
- 3 new GRAEAE reasoning modes: single, debate, majority.

### Changed

- Codebase restructured into `mnemos/` package (`api/routes` / `core` / `db` /
  `domain` / `mcp` / `webhooks` / `workers` / `hooks` / `installer` / `tools` /
  `cli`).
- `portability.py` (2679 LOC) split into 10 focused files + repository
  layer; route file is now 82 LOC.
- `openai_compat.py` (1366 LOC) -> 7 focused files; route file 270 LOC.
- `mcp_tools.py` (1278 LOC) -> 6 per-domain modules.
- `webhook_dispatcher.py` (1748 LOC) -> 11 modules per concern.
- `workers=1` pin removed; multi-worker safe with Redis.

### Fixed

- GRAEAE empty-body bug (HTTP 200 + 0 bytes on short prompts under
  `arch_design` with no mode field).
- Unknown mode values now 422-rejected (was silent fallthrough).

## [3.5.1] — 2026-04-28 (doc-triage patch)

Documentation and version-state reconciliation only. No product behavior
changes from v3.5.0.

### Changed

- Bump package/runtime version metadata from 3.4.1 to 3.5.1.
- Reframe README, deployment, specification, API, roadmap, evolution, and
  release-charter docs around the shipped v3.5.x state.
- Preserve historical LETHE / ANAMNESIS / ALETHEIA references, but remove or
  reframe current-state docs that still described retired compression engines,
  `CompressionManager`, the `DistillationEngine` compatibility wrapper, or
  vestigial session compression columns as active.
- Surface shipped v3.2-v3.5 features in user-facing docs: two-dimensional
  owner+namespace tenancy, MORPHEUS, recall tracking, MPF portability,
  CHARON schema preflight, webhook retry leases/outbox hardening, MCP registry
  parity, faithful OpenAI-compatible gateway handling, PostgreSQL streaming
  replication doctrine, and namespace-uniform audit closure.

## [3.5.0] — 2026-04-28

v3.5.0 is the audit-driven hardening and uniform-tenancy release. It shipped
the branch sequence that began after v3.4.1: session-history ordering,
memory-read tenancy and DAG integrity, webhook retry hardening, RLS
group-select parity, the federation compound-cursor tie-breaker, consultation
audit endpoint scoping, MCP transport parity, faithful OpenAI-compatible
gateway controls, namespace-uniform tenancy across remaining product surfaces,
bulk webhook parity, and the single-site PostgreSQL streaming-replication
doctrine.

### Added

- **Shared read-visibility helper** — `api/visibility.py` now owns
  `read_visibility_predicate` (`api/visibility.py:40-96`),
  `version_visibility_predicate` (`api/visibility.py:99-137`),
  `_assert_target_head_visible` (`api/visibility.py:140-168`),
  and `handle_trigger_pgerror` (`api/visibility.py:24-37`).
- **Trigger replacement migration** —
  `db/migrations_v3_5_trigger_same_memory_parent.sql` replaces
  `mnemos_version_snapshot()` so UPDATE/DELETE resolve branch HEADs
  under lock, fail closed on missing/NULL/foreign heads with SQLSTATE
  `MN001`, and keep the DELETE tombstone path live.
- **RLS group-select policy migration** —
  `db/migrations_v3_5_rls_group_select_unix_bits.sql` replaces
  `mnemos_group_select` so RLS uses Unix group-read bit math
  (`((permission_mode / 10) % 10) >= 4`), matching
  `read_visibility_predicate` and closing #25.
- **Docker existing-volume upgrade path** — `docker-compose.yml` and
  `docker-compose.staging.yml` now include a one-shot
  `postgres-upgrade` service that applies v3.5 database patch
  migrations after Postgres is healthy. This is required because
  `/docker-entrypoint-initdb.d` only runs when a volume is first
  initialized.
- **Regression coverage** — new slice-2 tests cover branch visibility,
  cross-memory DAG guards, visibility gaps in logs, trigger concurrency
  locking, `MN001` update/delete conflict mapping, version tenancy, and
  migration-list sync. The merged branch reports 768 passing tests.

### Changed

- **Slice 13 Phase-1 audit closure.** Internal categorization managers now
  require caller context and scope state, journal, and entity CRUD by
  `owner_id + namespace`; memory-created webhook delivery rows are enqueued in
  the same transaction as the memory insert; unknown chat-completion models now
  return OpenAI-style `404 model_not_found`; stale session compression columns
  are dropped by a new v3.5 migration; deployment docs and templates now state
  the v3.5 single-worker runtime contract.
- **Memory read surfaces use one predicate.** `list_memories`,
  `get_memory`, search, rehydrate, and gateway context now share the
  owner/federated/world/group-readable predicate. The Redis search
  cache key serializes raw inputs with `json.dumps(..., separators=...)`
  and includes group IDs so `None`, empty string, and group variation
  cannot collide.
- **History reads are per-snapshot.** `list_versions`, `get_version`,
  `diff_versions`, HTTP DAG log/get-commit/merge paths, and MCP
  log/checkout/diff paths gate each `memory_versions` row by the
  snapshot's own `owner_id`, `namespace`, and `permission_mode`.
  `memory_versions` lacks `group_id` and `federation_source`, so the
  version predicate intentionally fails closed for those historical
  cases.
- **DAG writers serialize on branch identity.** `merge_branch` and
  feature-branch `revert_memory` share `_branch_advisory_lock_key`
  (`api/handlers/dag.py:21-40`) and use advisory-lock-before-row-lock
  discipline. Main-branch revert still updates the live memory row
  through the trigger under the main GUC; feature-branch revert is a
  pure DAG insert.
- **Branch creation is race-safe.** HTTP and MCP branch creation lock
  the parent memory row with `FOR SHARE`, resolve the starting snapshot
  after the lock, and insert with `ON CONFLICT DO NOTHING RETURNING`.
  MCP implicit-HEAD retries are idempotent; explicit `from_commit`
  retries must match the existing head.
- **Merge writes target tenancy.** Merge commits copy content and
  provenance from the source snapshot but owner/namespace/permission
  from the target branch head; drift guards compare all versioned
  fields including tenancy before mutating live main.
- **Branch logs do not bridge hidden history.** Recursive log walks are
  same-memory only, and `parent_hash` is emitted only when the actual
  immediate parent is also visible.
- **Session history order fixed.** Slice 1 returns the most recent
  history messages instead of the oldest, with deterministic system-row
  pinning.
- **Project URLs moved.** `pyproject.toml` metadata points at
  `mnemos-os/mnemos`.
- **Legacy compatibility shims removed.** Federation cursors are compound-only,
  webhook recovery assumes current writer rows (`writer_revision=1`), session
  creation no longer accepts `compression_tier`, and the ARTEMIS compression
  path no longer exposes the `DistillationEngine` compatibility wrapper.
  Search helpers also use the full read-visibility predicate whenever
  `owner_id` is supplied instead of preserving the owner/federation-only
  fallback for omitted `group_ids`.
- **Slice 9 HA replication doctrine** — single-site deployments now document
  PostgreSQL streaming replication as the canonical HA path: one writable
  primary, read-only standbys, WAL shipping, and a stable writer endpoint for
  MNEMOS. Federation stays first-class, but is reserved for genuinely remote
  scenarios such as multi-site deployments, multi-org curated feeds, developer
  laptop replicas with intermittent connectivity, and planned v4 SQLite-based
  local-replica profiles.
- **Slice 12 — compression semantics** — drop session-layer always-NULL
  `compression_ratio` fiction columns from `session_messages` +
  `session_memory_injections`; document operator-batched compression doctrine in
  `docs/COMPRESSION.md`. Real compression layer
  (`memory_compression_queue`, `memory_compression_candidates`,
  `memory_compressed_variants`, `StatsResponse`, `RehydrationResponse`, admin
  batch endpoints) unchanged.
- **Namespace-uniform product surfaces.** State, journal, entities, sessions,
  and GRAEAE consultations now carry the same owner+namespace discipline as
  memory rows. Entity uniqueness is widened to
  `(owner_id, namespace, entity_type, name)`; state keys are scoped by
  `(owner_id, namespace, key)`.
- **Bulk memory create webhook parity.** `POST /v1/memories/bulk` now emits
  `memory.created` through the same transactional outbox path as single
  memory creation for every successful item and rolls back the batch if outbox
  enqueue fails.

### Fixed

- **Slice 8 OpenAI-compatible gateway honesty (#5/#6/#7)** —
  `/v1/chat/completions` now propagates `temperature`, `max_tokens`, and
  `top_p` through GRAEAE into provider payloads; supports OpenAI-format
  SSE when `stream=true`; accepts string or content-block message payloads;
  and passes tools/tool_choice, response_format, stop/n, and penalties only
  where the selected provider can honor them. Unsupported provider/field
  combinations now return explicit HTTP 400s instead of being silently
  dropped. `/v1/models/{model_id}` now returns 404 for unregistered models,
  and model discovery no longer synthesizes `owned_by="Unknown"` entries.
- **Slice 7 MCP split-brain (#24)** — `api/mcp_tools.py` is now the
  canonical MCP tool registry for stdio and HTTP/SSE transports. The live MCP
  surface includes CRUD, bulk create, stats, KG tools, DAG log/branch/diff/
  checkout, and `recommend_model`, with registry parity tests pinning both
  transports. HTTP/SSE now supports `MNEMOS_MCP_TOKENS=user:api_key` per-user
  bearer issuance and logs a WARNING when legacy shared `MNEMOS_MCP_TOKEN`
  mode would collapse clients onto one backend identity.
- **Slice 6 consultation audit endpoint scoping (#22)** —
  `/v1/consultations/audit` now returns only the caller's consultation audit
  rows for non-root users, while root retains the global operational view.
  `/v1/consultations/audit/verify` now scopes non-root verification to the
  caller's own consultation audit rows and keeps full-chain verification for
  root. Existing consultation detail and artifact routes are pinned by
  regression tests to return 404 for another user's consultation IDs.
- **Slice 5 round-2 search compression probe cleanup** — large
  `/v1/memories/search` result sets no longer call the retired
  distillation backend health check or log misleading "compression
  disabled" telemetry. The live compression path remains the
  queue-driven APOLLO/ARTEMIS contest and its persisted variants.
- **Federation feed cursor tie-breaker** — `/v1/federation/feed` now
  paginates with an opaque cursor carrying both `updated` and `id`, filters
  with `(m.updated > cursor_updated OR (m.updated = cursor_updated AND
  m.id > cursor_id))`, and orders by `m.updated ASC, m.id ASC`. The puller
  decodes the cursor for the next page while persisting the existing
  timestamp cursor column, so no schema migration is required. Feed servers
  are compound-cursor-only; malformed or missing cursors start an initial
  fetch from the beginning.
- **Webhook retry replay state machine** — `api/webhook_dispatcher.py:121-146`
  now recovers due `pending` rows plus `retrying` rows only when no
  successor attempt exists. Superseded attempts use
  `status='abandoned'` plus `superseded=TRUE`, while final failures keep
  `superseded=FALSE`; `db/migrations_v3_5_webhook_superseded_marker.sql`
  adds the audit marker and converts rows from the pre-round-8 branch-only
  terminal state. `db/migrations_v3_5_webhook_attempt_unique.sql` adds a
  live partial unique index on `(subscription_id, event_type, payload_hash,
  attempt_num)`, and successor inserts now use `ON CONFLICT DO NOTHING
  RETURNING` after an in-transaction successor recheck.
  `db/migrations_v3_5_webhook_retry_terminal_state.sql` repairs existing
  superseded `retrying` rows with `abandoned`.
  Round 3 replaces the long-held `FOR UPDATE SKIP LOCKED` send lock with
  `lease_token` / `lease_expires_at` persisted claims in
  `db/migrations_v3_5_webhook_attempt_lease.sql`, so DNS validation and
  outbound HTTP no longer hold shared DB connections. It also caps active
  sends per process, gates recovery claims and successor inserts
  with a per-chain advisory lock, and runs a startup repair burst before
  backing off to periodic repair sweeps. Operators must drain webhook
  writers before applying the v3.5 webhook retry migrations during rolling
  upgrades. Round 4 derives one wall-clock send deadline from
  `WEBHOOK_LEASE_SECONDS`, reserves a finalize buffer, wraps DNS validation,
  the HTTP POST, and the response-body read in that deadline, and streams
  response bodies into a fixed audit cap so a slow receiver cannot outlive
  the lease or hold a semaphore slot indefinitely. Round 5 anchors each send
  timeout to the DB-returned claim timestamps instead of a fresh static
  budget, and sends `Accept-Encoding: identity` on webhook POSTs as the first
  response-compression defense. Round 6 switches
  webhook lease/expiry SQL from transaction-snapshot `NOW()` to
  `clock_timestamp()`, reads audited response bodies through `aiter_raw()` and
  rejects non-identity response encodings before decompression, and adds
  `db/migrations_v3_5_webhook_writer_revision.sql` so current-writer rows are
  explicitly stamped with `writer_revision=1`. Round 7 adds
  `db/migrations_v3_5_webhook_status_updated_at.sql`, a trigger-maintained
  status-transition timestamp for audit and repair observability. Round 9 relaxes the
  idempotent repair sweep so out-of-order `pending`/`retrying` overwrites of an
  already superseded attempt are terminalized again whenever a newer successor
  exists. Round 10 splits retry repair and delivery recovery into independent
  lifespan tasks so slow webhook POSTs cannot starve the repair cadence, and
  makes the repair predicate skip rows with an unexpired lease so active
  new-worker sends do not lose ownership. Round 11 moves the app-side send
  deadline anchor inside `_claim_delivery` immediately before the lease UPDATE,
  makes lease-valid success finalization cancel free live successors under the
  chain advisory lock, and drains in-flight webhook delivery attempts during
  graceful shutdown before any last-resort cancellation. Round 12 schedules
  recovered rows into the lifecycle-tracked delivery-attempt registry instead
  of awaiting sends inside the recovery worker, adds succeeded-predecessor
  guards to claim, failure-finalize, and repair paths so active successors
  converge after canonical success, and treats response headers as the delivery
  acknowledgement while response-body capture becomes best-effort audit data.
  Round 13 extends the succeeded-predecessor guard into success finalization,
  so an active successor that also receives 2xx is abandoned/superseded with
  its response audit metadata instead of creating a second succeeded row.
  Round 14 broadens the convergence guard from earlier predecessors to any
  succeeded chain peer across claim, success-finalize, and failure-finalize
  paths, and adds `db/migrations_v3_5_webhook_succeeded_unique.sql` with a
  partial unique index that structurally enforces one terminal succeeded row
  per retry chain. Round 15
  excludes the current delivery id from succeeded-chain peer checks, requires
  active peer-abandon updates to still target live non-superseded attempts, and
  isolates ordinary stream/client cleanup exceptions after response headers so
  captured acknowledgements still finalize while `CancelledError` propagates.
  Round 16 makes revocation, final-failure, and retry-failure terminal UPDATEs
  require the leased row to still be live (`pending`/`retrying` and not
  superseded), so failure finalization cannot overwrite same-row terminal
  writes that already won. Round 17
  applies the same live-row guard to the success finalize UPDATE, clearing only
  stale lease columns when a same-row terminal write has already won, and
  moves recovery to claim due rows with a lease in the dequeue CTE before
  scheduling send tasks so repeated recovery polls do not enqueue duplicates
  behind the send semaphore. Round 18 sizes each recovery claim batch to the
  send semaphore's current free slots, treats `lease-expired-before-send` as a
  non-consumptive lease release instead of a failed attempt, and makes
  recovery-preclaimed sends take the retry-chain advisory lock for a final
  live-lease and succeeded-peer recheck before any outbound POST. Round 19
  makes external 2xx ACKs trump later lease expiry during success finalization:
  matching token ownership plus a still-live row is enough to persist
  `status='succeeded'`, while failure paths still require lease validity.
  Post-header stream/client cleanup is also bounded so a stuck `__aexit__`
  cannot delay finalization indefinitely. Round 20 moves status-code
  finalization ahead of response-body capture and stream/client cleanup:
  headers first persist `response_status` with `response_body=NULL`, then a
  post-finalize audit update fills the body only if capture finishes within its
  own timeout. Cleanup is also post-finalize best-effort. Round 21 splits the
  successful 2xx terminal UPDATE into its own short committed transaction, then
  reacquires the chain advisory lock for best-effort free-successor cleanup so
  cleanup lock contention, exceptions, or shutdown cancellation cannot roll back
  an already ACKed `status='succeeded'`. It also makes recovery-preclaimed sends
  re-check for live successors, including active-leased successors, under the
  pre-POST chain lock and abandon/supersede the older attempt before any
  duplicate outbound delivery.
  Round 22 keeps the ACK-protecting behavior for ordinary per-successor
  cleanup failures while closing the mixed-version replay window in the common
  case: the success UPDATE now finds and abandons free live successors in the
  same chain-locked transaction, with each abandon isolated by an explicit
  savepoint. A post-commit cleanup pass remains only as a fallback for
  successors inserted after the in-transaction successor query but before the
  success commit.
  Round 23 makes that convergence fully atomic for rolling-upgrade safety:
  per-successor savepoints and the post-commit fallback are removed, so a
  2xx success row and all free successor `status='abandoned'` updates commit
  or roll back together. Cleanup exceptions and `CancelledError` before commit
  now roll back the ACK record and partial cleanup, leaving the lease-owned
  attempt retryable. The rare tradeoff is a bounded duplicate POST after
  lease expiry, logged for observability, instead of a committed succeeded
  predecessor with live successors that old pre-GA workers could replay.
  Round 24 adds
  `db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql`, making
  `status='succeeded'` terminal at the database layer. Old id-only writers
  that attempt to move an ACKed row back to `pending` or `retrying` now fail
  with a trigger-raised `check_violation`, while response-body audit updates
  and lease clearing remain permitted.

### Conflicts and operator handling

- Trigger-raised `MN001` maps to HTTP 409 with reconciliation guidance:
  the branch row is missing, has `NULL head_version_id`, or points to a
  version from another memory. Operators should reconcile
  `memory_branches` against `memory_versions` for that memory before
  retrying the write.

### Deferred after v3.5.0

- Dedicated per-memory deletion-log table and GDPR wipe workflow remain v4
  scope. v3.5.0 keeps the DELETE tombstone snapshot path live in the version
  DAG, but it does not add a separate deletion-log subsystem.

## [3.4.1] — 2026-04-26

Federation schema-compat preflight + dev↔prod MPF restore drill.
Cross-version federation safety is the headline: peers now exchange
schema fingerprints before opening sync, refusing to pair when their
migration sets diverge unless an operator explicitly opts in via
`compat_mode=permissive`. Eight rounds of Codex adversarial review
on the federation handshake (verdict: SHIP). Restore-drill runbook
validated end-to-end on 10k records (~13s, 770 rec/sec) PYTHIA →
PROTEUS.

### Added

- **`GET /v1/federation/schema`** — preflight endpoint returning
  `mnemos_version`, `schema_signature` (`major.minor`), and
  `migrations_fingerprint` (sha256 over filename + content of
  `db/migrations*.sql`). Peers call this before opening sync and
  refuse to pair on mismatch.
- **`federation_peers` columns** — `compat_mode`
  (`strict|permissive`, default `strict`), `peer_mnemos_version`,
  `last_schema_check_at`. `strict` blocks sync on schema mismatch
  with HTTP 409 + operator-action message; `permissive` allows it
  through with a logged warning.
- **Typed exceptions** — `FederationSchemaIncompatible` /
  `Unverifiable` / `Transient` map to HTTP 409 / 409 / 503 so
  peers can distinguish "your schema is wrong" from "I can't
  reach you right now."
- **Native vs federated memory counts in `/stats`** — top-level
  totals plus a per-peer breakdown so operators can see at a
  glance which peer contributed which slice of the catalog.
- **`docs/RESTORE-DRILL.md`** — step-by-step dev↔prod MPF
  round-trip runbook: 5MB body cap on `/v1/import` means the CLI
  tool is the production path; `--preserve-metadata` is the
  dev↔prod lever; three-step DELETE + orphan sweep cleanup
  pattern documented.

### Changed

- **Worker queue ordering changed to next-due-time.** Previous FIFO
  starved peers with shorter `sync_interval_secs` when a longer-
  interval peer queued a large batch. New ORDER BY balances
  fairness across heterogeneous intervals.
- **`FEDERATION_ALLOW_INSECURE` plumbed through staging compose env.**
  Required for cross-version smoke tests on PROTEUS without
  full TLS termination.
- **MORPHEUS / APOLLO S-IVB naming locked** — no rename in v3.4.x.
  Both names appear in code, docs, and ops procedures by design;
  see `docs/PANTHEON.md`.

### Verified

- PROTEUS staging upgraded to v3.4.0, cross-version tested against
  PYTHIA v3.3.0 — `strict` returns 409 with operator-action message;
  `permissive` flip succeeds with 200. FK rollback applied during
  the v3.4 migration audit (issue #1 mnemos-os/mnemos rescoped to
  v3.5).

## [3.4.0] — 2026-04-26

CHARON v0.2 release: full MPF v0.1 sidecar round-trip, plus
staging-deploy infrastructure for PROTEUS as the cross-version
proving ground. Forty-four rounds of Codex adversarial review on the
sidecar attachment paths (cross-tenant attack surface, DAG
poisoning, version-DAG divergence, timestamp-shift, commit-hash
collision, conflict-row equality semantics, snapshot-consistent
export under REPEATABLE READ READ ONLY).

### Added

- **CHARON v0.2 sidecar round-trip** — full MPF v0.1 import + export
  with `--preserve-metadata` flag as the dev↔prod lever. Sidecar
  surfaces: `kg_triples`, `documents`, `facts`, `events`,
  `compression_manifest`, `memory_versions`. Tenant-scoped record
  IDs prevent cross-tenant sidecar attachment. Memory-versions
  sidecar requires root + `preserve_owner=true` (architectural
  restriction). Per-surface hard cap on sidecar export to bound
  memory consumption on large catalogs.
- **`docker-compose.staging.yml`** — PROTEUS staging compose,
  Postgres bound to :5433 (host-Postgres collision avoidance),
  pre-init `mnemos` role for fresh DB initialization.
- **v3.4 planning charters + ops doc** — `docs/history/V3_5_CHARTER.md`,
  `docs/history/V3_6_CHARTER.md`, `docs/history/V4_PLAN.md`, `docs/OPERATIONS.md`,
  `docs/PANTHEON.md` (extended with charter-bound sidecar
  ownership rules), `ROADMAP.md` cut.

### Changed

- **`OLLAMA_EMBED_*` env vars renamed to `INFERENCE_EMBED_*`.**
  Ollama is one of several inference backends; the variable name
  was misleading. Old names not honored — operators must update
  env files on upgrade.
- **GUC scope tightened on branch context.** Branch-scoped GUC now
  set within transaction only, parameterized to prevent injection.
  Same fix cherry-picked to v3.3.0 release as `8058666` (pre-v3.4
  audit).

### Audit

- Forty-four-round Codex adversarial review on sidecar paths.
  Closed: cross-tenant DAG-edge attack, shadow-parent attack,
  memory-ID oracle attack (records-loop), commit_hash collision,
  timestamp-shift, DAG-divergence integrity check, existing-memory
  DAG poisoning, no-main-branch import bypass, stale-version DAG
  poisoning, stale memory_branches not cleared before restore,
  version verification ON CONFLICT exact-match,
  `kg_triples` / `compression_manifest` ON CONFLICT exact-match,
  IS-NOT-DISTINCT-FROM semantics in conflict checks, in-envelope
  parents required for newly-inserted records, freshly-inserted vs
  conflict-skipped UUID tracking, JSON sidecars warn without
  `--preserve-metadata`, root-path conflict-row equality extension,
  conflict-row check covers all envelope-bound columns, pre-insert
  validation rejections block sidecar attachment, gated sidecar
  timestamp tolerance on freshness, COALESCE-tolerance for sidecar
  timestamp retries, snapshot-consistent export via REPEATABLE
  READ READ ONLY.

## [3.3.0] — 2026-04-26

Compression-stack settlement, CI policy flip to GitLab, MORPHEUS
slice 2 (real cluster + synthesise), and the docs/history/EVOLUTION.md origin
narrative. Closes the v3.2 compression-stack open question by
retiring ALETHEIA from the default contest.

### Added

- **MORPHEUS slice 2 — real cluster + synthesise phases.** Phase 1
  foundation shipped in v3.3.0-alpha.1; slice 2 adds the cluster
  pass (semantic grouping over the working set) and the synthesise
  pass (LLM-mediated synthesis of cross-memory patterns into
  derived facts). Three audit-log items closed: namespace scope on
  cluster output, cluster introspection endpoint, FastAPI
  deprecation cleanup. 31 tests in `tests/test_knossos_phase1.py` cover
  the phase-1 tool surface (0.46s).
- **`recall_count` + `last_recalled_at` on memory search hits.**
  Every search result increments the recall counter and updates
  the timestamp. Useful for downstream "warmest" / "coldest"
  prioritization queries.
- **`docs/history/EVOLUTION.md`** — five-month development timeline from
  v0.1 design review through v3.2 compression-stack settlement.
  Restructured to put origin story in v1.0 section + ADR block
  for release-gate decisions.
- **GitLab CI** (`.gitlab-ci.yml`) — three stages (`lint`, `test`,
  `build`) running against real Postgres + pgvector service. See
  `~/.claude/rules/gitlab-ci-policy.md` for the full rationale.
- **GitHub `pr-check.yml`** — slim PR-only lint + unit test
  workflow so external contributors get green/red signal on PR
  without maintainer-side GitLab pre-flight.

### Changed

- **`/kg` and `/sessions` routers moved to `/v1/` prefix.** The
  v2 endpoints stayed in place during the v3.0–3.2 transition;
  v3.3 finishes the migration. Old paths return 410 with the new
  path in the response body.
- **ALETHEIA retired from the default compression stack.** The
  going-forward stack is LETHE + ANAMNESIS + APOLLO (APOLLO in
  v3.3+ per ROADMAP.md Apollo Program). ALETHEIA won 0 contests in
  the 2026-04-23 CERBERUS benchmark — its index-list scoring prompt
  doesn't survive instruction-tuned generalist LLMs, and the
  fallback-to-first-N path is strictly inferior to LETHE at lower
  cost. Niche audit found every case where ALETHEIA might
  theoretically win is owned by LETHE (cheaper), ANAMNESIS (better
  fact shape), or APOLLO (schema-typed). `ALETHEIAEngine` now emits
  a DeprecationWarning on construction and is skipped in the
  default contest (`distillation_worker.py` still honors
  `MNEMOS_ALETHEIA_ENABLED=true` for operators who had it opted in,
  but logs a deprecation warning when that gate flips on). The
  engine class stays importable; v4.0 removes it entirely. See
  `docs/benchmarks/compression-2026-04-23.md` for measured rationale
  and the niche audit captured in-session.

### Removed

- **LETHE / ANAMNESIS / ALETHEIA modules and `CompressionManager`
  removed from the active code path.** Engine classes stay
  importable for backward compatibility; the manager is gone. The
  contest harness instantiates engines directly.

### Fixed

- **`install.py` / `installer/db.py` migration loaders include
  v3.2.2 + v3.3 migrations.** Two newer migration files were
  silently absent from the canonical loader.
- **CI pre-creates `mnemos_user` + `mnemos` roles** before applying
  migrations, eliminating a flaky CI failure mode where migrations
  ran before role provisioning completed.

## [3.2.0] — 2026-04-23

Tenancy + observability + ideation-infrastructure release. Rolls in
the v3.1.1 ops-hardening and v3.1.2 Tier-3 tenancy candidates, adds
the full request-correlation/metrics/tracing/logs observability
stack, wires compression artifacts into the hot retrieval paths,
makes the OpenAI-compatible gateway registry-first, lands per-user
namespace tenancy end-to-end (DB column, auth resolution, admin
provisioning API, Tier 3 enforcement on DAG / entities / webhooks),
ships MPF v0.1 export / import, brings the reasoning layer in line
with the public contract (consensus_response / consensus_score /
winning_muse / cost / latency_ms populated from the engine's own
_compute_consensus output), and opens `/v1/consultations` to
operator-driven Custom Query selection across the refreshed
frontier model registry. Queue workers are now self-healing (stale-
running sweep with forward-progress guarantee) and the GPUGuard
circuit breaker handles auto-replacement safely via a probe-identity
handshake. Closes every HIGH finding from the v3.2 memory-OS audit
and the full follow-up Codex re-audit.

### Ops hardening — v3.1.1 candidate

- **Stranded-running queue recovery sweep**
  (`compression/worker_contest.py`, `distillation_worker.py`). The
  v3.1 contest path had a belt-and-suspenders gap: if the
  fresh-connection mark-failed fallback ALSO failed (pool
  exhausted, SIGKILL mid-txn, host reboot), a queue row sat in
  `running` forever because the dequeue only matched `pending`.
  New `_sweep_stale_running()` runs at the top of every batch,
  reclaiming rows stale longer than
  `MNEMOS_CONTEST_STALE_THRESHOLD_SECS` (default 600). Rows below
  retry ceiling go back to `pending`; rows at ceiling go terminal
  `failed` with `stranded_running: ...` marker.

- **Sweep-vs-late-finisher race defense.** `_process_one` opens its
  persist transaction with `SELECT ... FOR UPDATE` against
  `memory_compression_queue` and checks both `status='running'` AND
  `attempts == <dequeue-time value>`. If the sweep reclaimed or
  another worker re-dequeued after reset, the fingerprint mismatches
  and the worker bails cleanly — no duplicate audit rows, no
  overwrite. New `race_abandoned` metric counter.

- **GPUGuard single-probe in HALF_OPEN** (`compression/gpu_guard.py`).
  The circuit breaker's HALF_OPEN state admitted every concurrent
  caller as the probe — thundering herd against a possibly-still-
  broken endpoint. Added `_probe_in_flight` coordination so exactly
  one probe is admitted at a time. Subsequent callers fast-fail
  until the probe resolves via `record_success` / `record_failure`.
  Auto-replacement of a wedged probe is intentionally NOT included
  in v3.1.x — avoiding an identity-tracking handshake that would
  have been needed to prevent late-completion races. Operators
  recover a wedged HALF_OPEN via `reset()`.

- **Richer error metadata in candidate manifests**
  (`compression/contest_store.py`). `persist_contest()` now runs
  every non-winner candidate through `_enriched_manifest()`, which
  preserves engine-authored manifest keys and ADDS a namespaced
  `_audit` block: `reject_reason`, `engine_version`, `error`
  (full exception text, previously lost from the DB), `quality_score`
  on floor rejections, `compression_ratio`, `elapsed_ms`, `gpu_used`.
  Winners are not enriched — their typed columns are authoritative.
  Non-dict engine-authored `_audit` values are preserved under
  `_audit_original` rather than crashing persist.

- **Log-space `speed_factor` to stop multiplicative speed dominance**
  (`compression/contest.py`). Raw linear `fastest_ms/elapsed_ms`
  crushed slow-but-accurate engines — a 10x-slower candidate
  scored 0.1 and multiplied through the composite made
  quality_first weighting unable to recover. Now:
  `factor = clamp(1 + log10(ratio)/2, [SPEED_FACTOR_FLOOR=0.1, 1.0])`.
  10x-slower maps to 0.5, 100x-slower bottoms at the floor. This
  is a scoring-breaking change for the `speed_factor` column;
  existing v3.1.0 rows are on a different scale.

- **Scoring-profile validation** (`compression/contest.py`).
  Custom TOML profiles previously accepted any `float()`-able
  value. Negative weights, 1000x weights, `quality_floor >= 1.0`,
  non-numeric strings, and NaN/Inf all produced surprising
  behavior. New `_validated_profile()` clamps weights to
  `[0.0, 10.0]` and `quality_floor` to `[0.0, 0.99]` with loud
  WARNING logs on every clamp. Explicit NaN/Inf rejection
  (they compare False to any numeric bound and would silently
  poison composite scores for every candidate).

- **`docs/SYSTEM_REQUIREMENTS.md`** — per-tier (Server / Workstation
  / Edge) resource floor reference. CPU / RAM / disk / GPU per
  tier, baseline (Python / Postgres / pgvector), environment
  knobs (`MNEMOS_CONTEST_ENABLED`, `MNEMOS_ALETHEIA_ENABLED`,
  `MNEMOS_CONTEST_STALE_THRESHOLD_SECS`), observed resource
  usage from live deployments as sanity check.

### Tier 3 tenancy rollup — v3.1.2 candidate

- **KG triples carry `owner_id` + `namespace`**
  (`db/migrations_v3_1_2_kg_tenancy.sql`, `api/handlers/kg.py`).
  Previously `kg_triples` had no tenancy columns and the `/kg`
  read/mutate paths had NO owner filter at all — every
  authenticated caller saw every row. Added columns idempotently
  (ADD COLUMN without DEFAULT, backfill from linked memory
  rows via `memories.memory_id` join, residual NULLs → 'default',
  THEN SET DEFAULT + NOT NULL — sequencing matters because
  ADD COLUMN DEFAULT would have made the backfill a no-op).
  Handlers now filter on BOTH owner AND namespace for non-root
  callers; cross-tenant `memory_id` references on create are
  rejected 404 (not 403 — existence is invisible to non-owners).

- **App-layer namespace enforcement on `list_memories` and
  `get_memory`** (`api/handlers/memories.py`). RLS policies from
  v1_multiuser scope `owner_id` + `group_id` but never filter by
  `namespace`. Personal-mode installs with RLS disabled had no
  tenancy filter at all. Non-root callers now get
  `AND namespace = user.namespace` appended to every WHERE
  branch (combined with category/subcategory filters). Root
  bypasses.

- **Owner + namespace pinning on `/memories/search` and
  `/memories/rehydrate`** (`api/handlers/memories.py`,
  `api/lifecycle.py`). `_fts_fetch` and `_vector_search` gained
  an `owner_id` kwarg. Non-root searches force `owner_id =
  user.user_id` and `namespace = user.namespace` regardless of
  the request body. Cross-namespace request from non-root →
  HTTP 403 (explicit rejection rather than silent narrowing).
  Cache key now hashes the EFFECTIVE pinned values, not the
  raw request.

- **Namespace enforcement on mutation precheck paths**
  (`api/handlers/memories.py`). `update_memory`,
  `delete_memory`, and `get_compression_manifests` now check
  BOTH owner AND namespace for non-root callers.

- **Registry-backed `/v1/models`** (`api/handlers/openai_compat.py`).
  Replaces the hardcoded six-entry list with
  `SELECT … FROM model_registry WHERE available AND NOT deprecated
  ORDER BY graeae_weight DESC`. This originally retained a built-in
  fallback list and synthetic `owned_by="Unknown"` detail responses for
  unregistered IDs; v3.5 Slice 8 supersedes that behavior with
  registry-only discovery and 404 on unknown model detail lookup.

### Provider routing + audit fixes — handoff work

- **Provider-unavailability errors now explain the cause**
  (`graeae/engine.py`, `api/handlers/openai_compat.py`).
  `_unavailable()` gained an `error: str` field; `route()` populates
  it for each failure class (provider not registered, missing
  api_key, upstream exception). `/v1/chat/completions` surfaces
  the cause in the 503 detail so operators don't have to tail
  debug logs:
  ```
  HTTP 503 {"detail": "Provider anthropic unavailable: HTTP 401 Unauthorized"}
  ```
  Missing-key case is caught pre-dispatch with a targeted hint
  at the standard env var to set.

- **MNEMOS-native Provider Registry File + env-var fallback**
  (`graeae/api_keys.py`). The key-file loader was too rigid and
  too permissive in the wrong ways: it only accepted the canonical
  `{"llm_providers": {...}}` shape AND logged only a generic
  warning on missing files. Replaced with:
  - Canonical shape only (MNEMOS-owned format, self-contained,
    no symlinks to third-party service key files).
  - Per-provider environment variable fallback using standard
    names every vendor SDK uses — `OPENAI_API_KEY`,
    `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`,
    `GROQ_API_KEY`, `PERPLEXITY_API_KEY`, `TOGETHER_API_KEY`,
    `NVIDIA_API_KEY`. Env vars win when both are set.
  - Search-path order swapped so `~/.config/mnemos/api_keys.json`
    is preferred over the legacy `~/.api_keys_master.json`.
  - `load_api_keys()` → `load_provider_registry()` with a
    backward-compat alias.

- **Refreshed frontier model defaults** (`graeae/engine.py`,
  `api/handlers/openai_compat.py`). v3.1.0 shipped with 2024-era
  model IDs. Updated to current:
  - `openai: gpt-4o → gpt-5.2-chat-latest`
  - `claude: claude-3-5-sonnet-20241022 → claude-opus-4-6`
  - `gemini: gemini-1.5-pro → gemini-3-pro-preview` (URL too)
  - `xai: grok-2-latest → grok-4-1-fast`
  - `perplexity: sonar-pro` (unchanged)
  - `groq: llama-3.3-70b-versatile` (unchanged)

  GPT-5 series requires `temperature=1` (returns 400 on any other
  value). `_query_openai_compatible` now omits the temperature
  field for `gpt-5*` models, matching the existing
  `max_completion_tokens` branching.

- **`graeae_audit_log` schema backfill**
  (`db/migrations_v3_1_2_audit_log_columns.sql`). Databases that
  applied `migrations_v2_versioning.sql` first got the v2 table
  shape; `migrations_v3_graeae_unified.sql` used `CREATE TABLE IF
  NOT EXISTS` so the six new columns (`prompt`, `response_text`,
  `prev_chain_hash`, `model`, `latency_ms`, `cost_usd`) never
  landed. The consultations handler INSERT referenced these by
  name, so `/v1/consultations` returned 503 with "audit trail
  is required" and an `UndefinedColumnError` in the log.
  Added all six via `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.
  All nullable so existing hash-only audit rows aren't
  retroactively invalidated.

- **UUID → str coercion on consultation response**
  (`api/handlers/consultations.py`). asyncpg returns UUID columns
  as `uuid.UUID` objects; `ConsultationResponse.consultation_id`
  is typed `Optional[str]` and pydantic strict mode rejected the
  UUID. One-line coercion at the construction site.

### Tests

Suite: 282 → 295 → 303 → 309 → 317 → 318 → 318 across the series.
All targeted tests green, full suite 0 regressions.

### Deferred to later releases (v3.3+)

- Horizontal scaling past workers=1 — GRAEAE reliability state
  (circuit breaker, rate limiter, semaphores) is process-local, so
  the server is still pinned to single-worker uvicorn. External
  state store (Redis) or session-affinity at the load balancer is
  the path forward.
- Webhook SSRF DNS-rebinding defense — the current allowlist is
  checked once at subscribe time; a malicious DNS TTL could still
  flip to an internal IP between check and delivery. Needs
  per-delivery re-resolution against a pinned IP.
- Federation peer tokens stored plaintext — `federation.py:113`
  still writes tokens in the clear; needs symmetric-encrypt-at-rest
  with operator-supplied key or KMS plugin.
- APOLLO engine (v3.2–v3.4 per ROADMAP.md) — schema-aware dense
  encoding for LLM-to-LLM wire use. S-IC scheduled for v3.3 kickoff.
- Dream state (v3.3 preview / v3.4 real) — divergent-mode ideation
  riding on APOLLO's dense-form substrate. Design scoped in
  `docs/DREAM_STATE_DESIGN.md`.

## [3.1.0] — 2026-04-23

Compression platform release. Adds a plugin `CompressionEngine` ABC open
to operator-registered engines, a competitive per-memory contest across
three built-in engines, and a persisted audit log recording every
winner AND loser per contest with its score and disqualification
reason — not just the chosen output. Extends the v3.0 schema with three
new tables (`memory_compression_queue`, `memory_compression_candidates`,
`memory_compressed_variants`) wired through a GPU circuit breaker that
fast-fails when the inference endpoint is unreachable.

Ships the Tier 1 small-fix unblocks already on master since 2026-04-22
under the v3.1 umbrella; Tier 3 tenancy fixes are explicitly deferred
to v3.1.1; APOLLO (the fourth engine, schema-aware dense encoding for
LLM-to-LLM wire use) is staged across v3.2–v3.4 per `ROADMAP.md`.

### Added

- **Plugin `CompressionEngine` ABC** (`compression/base.py`). Open
  interface for first-party and operator-registered engines. Declares
  `id`, `label`, `version`, `gpu_intent` at class level. One async
  method, `compress(CompressionRequest) -> CompressionResult`. Adapted
  from OpenClaw's `CompactionProvider` pattern (Apache-2.0, credited in
  module docstring).

- **Three engines under the ABC**: LETHEEngine (extractive, CPU),
  ALETHEIAEngine (LLM-assisted token importance, GPU), ANAMNESISEngine
  (LLM fact extraction, GPU). All three compose the existing v3.0
  engines; existing sync callers (manager.py, distillation_engine.py)
  continue to work unchanged.

- **Competitive-selection contest** (`compression/contest.py`). The
  distillation worker runs every eligible engine per memory via
  `asyncio.gather`, scores each candidate via a composite function
  (`quality * ratio_term * speed_factor`, with a quality floor that
  disqualifies damaged output), and picks the highest-scoring survivor.
  Scoring profile configurable via `~/.mnemos/compression_scoring.toml`:
  `balanced` | `quality_first` | `speed_first` | `custom`.

- **Persisted contest audit log** (`compression/contest_store.py`).
  `persist_contest()` writes every candidate (winner AND losers)
  into `memory_compression_candidates` and upserts the winner into
  `memory_compressed_variants` in a single transaction. Operators
  get a full record of what was tried, what scored how, and why each
  engine was or wasn't picked.

- **GPU circuit breaker** (`compression/gpu_guard.py`). Per-endpoint
  three-state breaker (CLOSED → OPEN → HALF_OPEN → CLOSED) tracks
  health of each configured `GPU_PROVIDER_HOST`. `gpu_required`
  engines (ALETHEIA, ANAMNESIS) fast-fail with
  `reject_reason='disabled'` when the circuit is open instead of
  piling doomed requests onto a dead endpoint. Process-local
  registry (v3.2 horizontal-scaling work makes it shared-state).

- **Distillation-worker queue drain** (`compression/worker_contest.py`
  + `distillation_worker.py`). `process_contest_queue()` atomically
  dequeues pending rows via `FOR UPDATE SKIP LOCKED`, runs the
  contest, persists the outcome, transitions the queue row
  `pending → running → done/failed` with an honest rejection-reason
  summary on failure. Runs alongside the existing v3.0 direct-memory
  polling loop; failure-isolated so a contest error doesn't stall
  the legacy path.

- **`GET /v1/memories/{id}/compression-manifests`** endpoint
  (`api/handlers/memories.py`). Returns the current winning variant
  and every historical contest grouped by `contest_id`, with
  scoring fields and reject_reason per engine attempt.
  `?include_content=true` returns full compressed content; default
  is a 200-char preview. RLS-gated via the underlying memories
  table.

- **v3.1 schema** (`db/migrations_v3_1_compression.sql`). Three new
  tables wired idempotently: `memory_compression_queue` (write-time
  task queue), `memory_compression_candidates` (full contest log),
  `memory_compressed_variants` (current winner per memory). Dry-run
  validated against real Postgres.

- **Environment flags**:
  - `MNEMOS_CONTEST_ENABLED` (default `true`) — gates the whole v3.1
    path. Operators who want to run v3.0 behavior exclusively can
    flip to `false`.
  - `MNEMOS_ALETHEIA_ENABLED` (default `false`) — see "Changed"
    below.
  - `MNEMOS_CONTEST_MIN_CONTENT_LENGTH` (default `0` = off) —
    optional threshold below which the worker marks queue rows
    `failed` with `error='too_short'` before running any engine.
    Surfaced by the 2026-04-23 benchmark: ~8% of real production
    memories are short templated blurbs (git commit headers,
    consultation stubs) that cannot be meaningfully compressed under
    any engine at the balanced profile's floor — LETHE returns
    ratio~1.0, ANAMNESIS's rendering inflates past 1.0, contest
    fails "no winner" after burning ANAMNESIS's multi-second GPU
    round-trip. Recommended value `500` for GPU-constrained installs;
    default `0` preserves the full-contest behavior.

- **Admin compression-queue endpoints** (`api/handlers/admin.py`):
  - `POST /admin/compression/enqueue` — enqueue specific memory IDs
    into `memory_compression_queue`. Skips unknown IDs silently
    (reports count in response).
  - `POST /admin/compression/enqueue-all` — bulk enqueue up to
    `limit` (default 500, max 10,000) memories. Default filters to
    memories without an existing variant; `only_uncompressed=false`
    forces re-contest.
  Without these, the v3.1 contest pipeline has no application-layer
  entry point — operators would need manual SQL to exercise it.

- **First real benchmark**:
  `docs/benchmarks/compression-2026-04-23.md`. 464 stratified memories
  from PYTHIA MNEMOS (uncompressed only, small/medium/large buckets)
  drained through the contest on a CERBERUS test deployment with
  gemma-4-E4B-it-Q6_K as the judge model. Winner distribution,
  per-category breakdown, ratio histogram, timing histogram per
  engine, outlier cases, and the one real bug the drain surfaced
  and fixed.

- **`ROADMAP.md`**. Committed scope for v3.1 and the v3.2–v3.4
  "Apollo Program" staged rollout. Explicit deferrals with
  rationale.

### Changed

- **ALETHEIA is disabled by default** (`MNEMOS_ALETHEIA_ENABLED=false`).
  The v3.0 engine's index-list scoring prompt ("output comma-separated
  token indices to keep") doesn't survive instruction-tuned chat
  models — tested against Qwen2.5-Coder-7B and gemma-4-E4B-it, both
  return off-spec text the parser can't interpret. Parser falls
  through to first-N truncation with honest `quality_score=0.60`,
  which the balanced profile's 0.70 quality_floor correctly rejects.
  Engine never wins and burns GPU time. Default engine roster is now
  LETHE + ANAMNESIS. Operators with a tuned prompt/model combination
  opt in via the env var. The prompt redesign is v3.x scope.

- **README.md + ROADMAP.md reality-alignment audit**. Stripped APOLLO
  from v3.1 descriptions (moved to v3.2–v3.4). Switched "four engines"
  → "three engines under a plugin ABC". Normalized stale v3.0.0
  language to v3.0 (release line). Removed "on the roadmap" claims
  for integration adapters not actually in the roadmap. Generalized
  specific production-count numbers that would age.

### Fixed

- **Tier 1 unblocks** (already on master as 2026-04-22 commits, now
  under the v3.1 umbrella):
  - MCP stdio server path prefix (`#M31-01`). The published stdio
    MCP server called `/memories*` but the REST router registers
    `/v1/memories*` — nine of fourteen memory tools returned 404
    against a default install.
  - Installer `api_keys` schema alignment (`#M31-04`). Fresh
    auth-enabled installs failed at seed because `installer/db.py`
    wrote columns the current schema no longer has.
  - Admin `create_user` accepts `role='federation'` (`#M31-03`).
    Federation peer onboarding previously required direct SQL writes
    because the admin validator and the v1_multiuser CHECK
    constraint both rejected the role at creation time.

- **`mnemos_version_snapshot()` trigger bytea crash on backslash
  content** (`db/migrations_v3_1_versioning_fix.sql`). The v2
  versioning trigger computed `commit_hash` via direct `text::bytea`
  cast on concatenated memory content. Postgres interprets
  backslash-escape sequences (`\x47`, `\d+`, `\0`, `\n`, `\x1b[...`)
  as bytea escape syntax and rejects the INSERT outright with
  "invalid input syntax for type bytea". Affected any production
  install ingesting memories that contain code, paths, or regex
  patterns — which is most real content. Latent since v2 shipped;
  surfaced by the v3.1 CERBERUS test deployment running real PYTHIA
  memories. Fix replaces `(text)::bytea` with `convert_to(text,
  'UTF8')` which returns raw UTF-8 bytes without trying to parse
  escape sequences. Idempotent migration; `CREATE OR REPLACE
  FUNCTION` replaces the existing definition in place.

- **Composite-zero winner CHECK-constraint violation**
  (`compression/contest.py`). Short memories where every engine
  scored `composite_score=0` (ratio at or below MIN_CHUNK_RATIO
  or >= 1.0) previously "won" the contest with
  `persist_contest`'s NULL coercion violating
  `mcc_winner_has_output`. Surfaced during the 49-memory CERBERUS
  drain. `run_contest` now requires `composite_score > 0` for
  winner eligibility; zero-composite survivors fall through to
  `reject_reason='inferior'`, and the queue row is marked `failed`
  with an honest "no winner" message rather than silently storing a
  degenerate "winner" variant.

- **ALETHEIA parser returns first-N fallback on unparseable model
  responses** (`compression/aletheia.py`). Pre-existing v3.0 bug
  where the importance-score parser returned empty content when
  zero valid indices survived filtering (as opposed to an actual
  exception). Now explicitly raises on empty-indices → existing
  first-N fallback fires. Compress result reports honest
  `quality_score=0.60` and `method='aletheia_parse_fallback'` when
  fallback is used. Surfaced during live-GPU testing against Qwen
  and gemma; the contest correctly filters the degenerate output
  via the ratio_term floor, but the audit log now accurately shows
  WHAT happened rather than reporting "aletheia" with empty content.

- **`ratio_term` floor below MIN_CHUNK_RATIO** (`compression/contest.py`).
  Scoring function returned `1.0 - ratio` for any ratio, which
  rewarded degenerate empty-output engines (ratio=0) with maximum
  score. Now returns 0 for ratios below `MIN_CHUNK_RATIO` (0.15) or
  at/above 1.0 — empty output and non-compression both score zero.
  Surfaced by live-GPU testing of ALETHEIA.

### Deferred

- **Tier 3 tenancy fixes** — v3.1.1 patch series with migration
  guides and per-fix regression coverage. Covers KG `owner_id`
  column + handler enforcement, namespace enforcement on memory
  paths, application-layer owner filter (defense-in-depth beside
  RLS), and registry-backed `/v1/models` (instead of hardcoded list).
- **APOLLO engine + schema-aware dense encoding** — v3.2–v3.4
  Saturn V-staged rollout per `ROADMAP.md`. Design informed by
  InvestorClaw's consultative-LLM pipeline pattern, not by raw
  Apollo-era telemetry specs.
- **Narration endpoint** (`GET /v1/memories/{id}/narrate`) — v3.2,
  APOLLO's companion read path.
- **Hot-path compression-variant reads** (rehydrate / gateway inject
  / session context serving winner variants instead of raw
  `memories.content`) — v3.2 alongside APOLLO.
- **Judge-LLM quality scoring** replacing engine self-reports —
  v3.2 alongside APOLLO. Today's scoring depends on engines'
  self-reported quality; a real judge would likely shift some
  wins between engines.

## [3.0.1] — 2026-04-22

Patch release fixing three credibility-sensitive defects in the initial
public cut of v3.0.0. No feature changes, no schema changes, safe in-place
upgrade.

### Fixed

- **OpenAI gateway: full conversation history reaches the provider**
  (`api/handlers/openai_compat.py`). The `_route_to_provider` helper used
  by `/v1/chat/completions` and `/sessions/*/messages` previously
  collapsed the request to `messages[-1]["content"]`, silently dropping
  the system prompt, injected memory context, and every prior assistant
  turn before the provider call. A new `_flatten_messages_for_prompt`
  helper serializes the full `messages` array with role boundaries so
  multi-turn chat and session history reach the provider intact. Silent
  regression — no error, just degraded responses — fixed.

- **Docker Compose applies all 11 migrations, not 4**
  (`docker-compose.yml`). The v3.0 Compose file mounted only the first
  four migration files into `docker-entrypoint-initdb.d/`. Fresh Compose
  installs booted without sessions, DAG, consultations audit, webhooks,
  OAuth, federation, or ownership tables — every v3 route 500'd on first
  use. All eleven migration files are now mounted in the canonical
  order (matches `installer/db.py::run_migrations()`).

- **Session compression metrics tightened** (`api/handlers/sessions.py`).
  The session-injection path currently ships raw-slice truncation, not
  real compression; the `compression_ratio` columns on
  `session_messages` and `session_memory_injections` now write `NULL`
  rather than placeholder constants. Real ratios are populated in v3.1
  once compression is wired into the session path.

### Also

- Internal renaming: compression mode aliases in `compression/lethe.py`
  and `compression/distillation_engine.py` updated to accurate
  descriptors. No behavior change; source-tree honesty pass.

## [3.0.0] — 2026-04-22

First public release.

MNEMOS has been in daily production use since December 2025, backing multiple
active agentic systems. This is the first cut shipped as open source — a
single unified FastAPI service covering memory, multi-LLM consensus
reasoning, DAG versioning, provider routing, and an OpenAI-compatible
gateway.

### What's in

**Unified API under `/v1/*`**

- **Consultations** (`/v1/consultations`) — GRAEAE multi-LLM consensus
  reasoning with cited memory artifacts and a tamper-evident SHA-256
  hash-chained audit log. Memory-injection tracking per consultation via
  `consultation_memory_refs`. Atomic persistence: consultation row, audit
  entry, and memory refs commit in a single transaction; audit-write
  failure aborts the consultation.
- **Memories** (`/v1/memories`) — CRUD, semantic + FTS search, DAG
  versioning (git-like: `log`, `branch`, `merge`, `revert`), three-tier
  compression pipeline (LETHE CPU / ALETHEIA GPU / ANAMNESIS archival)
  with a written quality manifest on every transformation.
- **Providers** (`/v1/providers`) — unified catalog, health tracking,
  task-aware model recommendation. Falls back to static config when the
  model-registry table is empty (fresh-install friendly).
- **OpenAI-compatible gateway** (`POST /v1/chat/completions`,
  `GET /v1/models`) — drop-in for OpenAI SDK consumers with automatic
  provider routing and optional memory injection.
- **Sessions** (`/sessions`) — stateful multi-turn chat with memory
  injection at turn boundaries.
- **Webhooks** (`/v1/webhooks`) — HMAC-SHA256-signed outbound event
  delivery. SSRF-hardened URL validation at both subscription and
  dispatch time (loopback, private, link-local, cloud-metadata endpoints
  all rejected). Durable retry log replayed on restart (1m / 5m / 30m /
  2h backoff; `abandoned` after four attempts).
- **OAuth / OIDC** (`/auth/oauth/*`) — browser login via Google, GitHub,
  Azure AD, or any generic OIDC provider (Keycloak, Authentik, Auth0,
  Okta). DB-backed sessions, hourly GC, `email_verified` required for
  cross-provider account linking. Coexists with API-key Bearer auth.
- **Federation** (`/v1/federation/*`) — pull-based cross-instance memory
  sync. Per-memory opt-in via `permission_mode` (others-read bit).
  Admin-only peer management, `federation`-role `/feed` endpoint,
  loop-prevention via `federation_source`.
- **Knowledge graph** (`/kg/triples`, `/kg/timeline/{subject}`) —
  temporal triple store with `valid_from` / `valid_until` windows.
- **Per-owner multi-tenant isolation** on memories, consultations,
  state, journal, entities. Root-only override for cross-owner
  operations.

**Infrastructure and tooling**

- Python 3.11+, PostgreSQL + pgvector, asyncpg.
- Body size limit enforced as streaming ASGI middleware (chunked-upload
  safe, default 5 MB, `MAX_BODY_BYTES` configurable).
- Rate limiter keyed on socket peer by default; honours `X-Forwarded-For`
  when `RATE_LIMIT_TRUST_PROXY=true`.
- Distillation worker supervised with exponential-backoff restart
  (cap 5 min).
- TLS enforced on federation peer URLs (opt-out via
  `FEDERATION_ALLOW_INSECURE`).
- CI runs under `uv` with a reproducible `.venv`. Ruff-clean tree.
- Installer CLI (`mnemos-install`) shipped as a `[project.scripts]`
  entry point so `pip install mnemos-os` gives you a working install
  binary without needing the source tree.
- All eleven SQL migrations ride inside the wheel as `db/*.sql`
  package data — accessible at runtime via
  `importlib.resources.files("db")`.

**Integrations**

- Drop-in hooks, skills, and MCP configs for Claude Code, OpenClaw,
  ZeroClaw, and Hermes. Each framework gets SKILL.md + MCP config +
  enforcement snippet; Claude Code also includes idempotent install /
  uninstall scripts.
- IBM Docling integration for PDF / DOCX / HTML / MD / PPTX / TXT
  import (`tools/docling_import.py`).
- Generic bulk-import helper (`tools/memory_import.py`).
- MCP tools for DAG versioning and the model optimizer (stdio MCP server).

### Security posture

- Tamper-evident SHA-256 hash chain on every consultation.
  `audit/verify` walks the chain from genesis; rate-limited 5/min,
  `audit` list 30/min.
- Consultation row + audit entry + memory refs commit atomically.
  Audit-write failure aborts the consultation with 503.
- Webhook URL validation blocks loopback, RFC1918 private, link-local,
  multicast, reserved, cloud-metadata endpoints (Google / AWS / Azure /
  Alibaba / Tencent / IPv6 variants). Async DNS resolution so a slow
  resolver can't freeze the ASGI worker.
- Webhook payloads HMAC-SHA256 signed per subscription. Delivery log
  retained after soft-delete for audit.
- OAuth cookie `Secure` flag honours `X-Forwarded-Proto` behind a
  trusted proxy (`OAUTH_TRUST_PROXY=true`). Sessions DB-backed,
  revocable.
- OAuth account-linking requires `email_verified=true` from the
  provider (strict — the string `"false"` does not count as verified).
- DAG merge wrapped in a single transaction held under
  `pg_advisory_xact_lock` keyed on `(memory_id, target_branch)` so
  concurrent merges cannot produce orphan commits or duplicate version
  numbers.
- Memory `owner_id` / `namespace` override on create requires
  `role='root'`.
- Explicit `owner_id = $2` filter on memory PATCH / DELETE as
  defense-in-depth beyond RLS.

### License

Apache License 2.0 — see [`LICENSE`](./LICENSE). Contributions accepted
under the Developer Certificate of Origin (DCO), see
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

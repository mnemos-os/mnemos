# MNEMOS Specification

**Version**: v4.0.0 current; shipped 2026-04-29
**Status**: Authoritative for the checked-out v4.0.0 tree. Behavior not
described here is either undefined (report as a bug) or scoped to a
future release via `ROADMAP.md`.
**Purpose**: supply enough structural detail that a scoping tool
(human or LLM) can estimate effort to build MNEMOS from scratch, or
to re-implement any named subsystem. Not a marketing doc. Not a
roadmap. Not an API reference; see README, ROADMAP.md, and
API_DOCUMENTATION.md respectively.

---

## 1. Abstract

MNEMOS is a memory operating system for agentic software. It is a Python
3.11+ package (`mnemos/`) exposing a FastAPI service on port 5002, MCP stdio
and HTTP/SSE transports, background workers, installer helpers, and one
operator CLI (`mnemos`). It runs in three deployment profiles:

- `server`: Postgres + pgvector + Redis, suitable for multi-worker production.
- `edge`: SQLite + sqlite-vec, single-worker, suitable for laptops, Pi-class
  hosts, local appliances, and Termux-style installs.
- `dev`: SQLite + sqlite-vec with debug logging and development defaults.

The system is operating-system-shaped rather than library-shaped:
hash-chained reasoning audit logs, content-addressed DAG versioning on every
memory, a plugin compression contest with a persisted per-decision audit trail,
SSRF-hardened outbound webhooks, cross-instance federation, owner+namespace
tenancy, shared read-visibility predicates across live and historical memory
surfaces, a model registry with scheduled sync from provider APIs and Arena.ai
Elo rankings, request-scoped observability, and an OpenAI-compatible gateway
that injects memory context on the fly.

v4.0 adds the package restructure, `PersistenceBackend` abstraction,
Postgres/SQLite backends, deployment profiles, single-binary distribution,
unified CLI, and Redis-coordinated multi-worker support. Apache-2.0.

## 2. System Scope

### 2.1 In scope at v4.0.0

- **Memory**: CRUD, search, DAG versioning with branch/merge, knowledge-graph
  triples, categories + namespaces, background compression with persisted audit.
- **Persistence**: `mnemos.persistence.base.PersistenceBackend` with
  `PostgresBackend` (asyncpg + pgvector + RLS + LISTEN/NOTIFY) and
  `SqliteBackend` (aiosqlite + sqlite-vec + FTS5 + JSON1 + WAL).
- **Reasoning (GRAEAE)**: multi-LLM consultation across registered providers
  with cryptographic hash-chain audit, Custom Query lineup selection,
  routing-strategy modes (`auto`, `local`, `external`, `all`), reasoning-shape
  modes (`single`, `debate`, `majority`), and Redis-backed reliability
  primitives for multi-worker operation.
- **Gateway**: OpenAI-compatible `/v1/chat/completions` + `/v1/models` with
  registry-backed provider resolution, memory context injection, propagated
  generation controls, SSE streaming, and explicit pass-or-400 handling for
  provider-specific tool/format/multimodal support.
- **Sessions**: multi-turn conversation state with memory injection at turn
  boundaries. Legacy session compression columns were removed in v3.5.
- **Tenancy**: per-user `owner_id` + `namespace` two-axis gate; root bypasses
  both. Live memory reads use owner/federation/world/group visibility; version
  and DAG history use per-snapshot visibility.
- **Auth**: Bearer API keys (`/admin/users/{id}/apikeys`), OAuth/OIDC browser
  login (authlib), and RLS-capable schema on the Postgres profile.
- **Federation**: pull-based cross-instance sync with Bearer-auth peers,
  schema-compat preflight, compound feed cursor, per-memory opt-in, and
  loop-prevention via `federation_source`.
- **Webhooks**: SSRF-hardened outbound delivery with HMAC signing, persisted
  leases, retry-chain convergence, and terminal-success database guard.
- **Portability**: MPF v0.1.x export/import with sidecars, Docling-based
  document ingest (optional extra).
- **Observability**: request-ID ContextVar, Prometheus `/metrics`,
  OpenTelemetry spans (opt-in), structured JSON logs (opt-in).
- **MORPHEUS**: operator-triggered dream-state runs with REPLAY / CLUSTER /
  SYNTHESISE / COMMIT phases, cluster introspection, namespace scoping, and
  rollback by `morpheus_run_id`.
- **Two-protocol surface**: REST over HTTP plus MCP over stdio and HTTP/SSE
  from one tool registry.

### 2.2 Explicitly out of scope at v4.0.0

- Full per-memory deletion-log / GDPR wipe subsystem. v3.5 keeps DELETE
  tombstone snapshots live in the version DAG, but it does not add a separate
  deletion-log table.
- Federation per-peer ACL beyond the current peer credentials, namespace
  filters, category filters, and feed role gate.
- PERSEPHONE archival and MORPHEUS mutation paths (consolidate / archive /
  extract). These move to v4.1+ planning.
- Web UX in `mnemos-web`, mobile native clients, and Rust rewrites. These are
  post-v4.0 tracks.

### 2.3 Non-goals (permanent)

- General-purpose key-value store. Use Redis.
- Blob storage. Memory content is text; binary handling is upstream of MNEMOS.
- Inference engine. MNEMOS routes to providers (OpenAI, Together, Groq, local
  vLLM/Ollama); it does not serve model weights.
- Application framework. MNEMOS is the memory kernel that agents call; agent
  logic lives in callers.

## 3. Architecture And Subsystem Inventory

The v4.0 source tree is organized by boundary:

```text
mnemos/
  api/routes/      FastAPI route adapters
  core/            config, lifecycle, visibility, pool, resilience primitives
  db/              repository functions and SQL access helpers
  domain/          business logic: compression, GRAEAE, MORPHEUS, portability
  persistence/     backend ABC plus Postgres and SQLite implementations
  mcp/             stdio + HTTP/SSE transports and tool registry
  webhooks/        validation, dispatcher, delivery/recovery/repair workers
  workers/         out-of-process background workers
  hooks/           integration hooks
  installer/       install wizard, db bootstrap, service generation
  tools/           CHaron/MPF utilities and adapters
  cli/             unified Typer command surface
```

Seven import-linter contracts enforce the architecture in CI: layered
api -> domain -> persistence/db -> core; API routes do not call `mnemos.db`
directly; domain modules remain sibling-independent; MCP does not call route
handlers as functions; webhooks are independent from domain; core has no upward
dependencies; persistence has no upward dependencies.

### 3.1 Storage layer

| ID | REST router | DB tables | Role |
|----|-------------|-----------|------|
| memories | `mnemos/api/routes/memories.py` | `memories` | Core CRUD + search; hot-path compression-variant reads via three-tier COALESCE |
| versions | `mnemos/api/routes/versions.py` | `memory_versions`, `memory_branches` | Read-only view over the DAG with per-snapshot visibility |
| dag | `mnemos/api/routes/dag.py` | `memory_versions`, `memory_branches` | Git-like operations: log, branch, merge, revert; same-memory parent guards |
| kg | `mnemos/api/routes/kg.py` | `kg_triples` | Knowledge-graph triples with subject/predicate/object |
| entities | `mnemos/api/routes/entities.py` | `entities` | Named-entity registry with tenancy gates |
| state | `mnemos/api/routes/state.py` | `state` | Arbitrary key/value state attached to memories |
| journal | `mnemos/api/routes/journal.py` | `journal` | Append-only operational log |

### 3.2 Reasoning layer

| ID | REST router / module | DB tables | Role |
|----|----------------------|-----------|------|
| consultations | `mnemos/api/routes/consultations.py` + `mnemos/domain/graeae/engine.py` | `graeae_consultations`, `graeae_audit_log`, `consultation_memory_refs` | Multi-LLM reasoning with hash-chain audit |
| providers | `mnemos/api/routes/providers.py` + `mnemos/domain/graeae/provider_sync.py` | `model_registry`, `model_registry_sync_log` | Provider inventory + model registry + scheduled sync |

GRAEAE reliability modules live under `mnemos/domain/graeae/`. Redis-backed
rate limiter, circuit breaker, and concurrency limiter coordinate multi-worker
server deployments; the in-process fallback is retained for edge/dev and logs a
warning when used with multiple workers.

### 3.3 Access, auth, and cross-instance layers

| ID | REST router / module | DB tables | Role |
|----|----------------------|-----------|------|
| openai_compat | `mnemos/api/routes/openai_compat.py`, `mnemos/domain/openai_compat/` | `model_registry`, `session_messages` | OpenAI-compatible gateway with memory injection and provider capability validation |
| sessions | `mnemos/api/routes/sessions.py` | `sessions`, `session_messages`, `session_memory_injections` | Multi-turn conversation state |
| health | `mnemos/api/routes/health.py` | `memory_stats` | Liveness, readiness, profile, and statistics |
| admin | `mnemos/api/routes/admin.py` | `users`, `api_keys`, `oauth_providers`, `groups`, `user_groups` | User/key/group/provider provisioning |
| oauth | `mnemos/api/routes/oauth.py` | `oauth_identities`, `oauth_sessions` | OAuth/OIDC browser login |
| visibility | `mnemos/core/visibility.py` | - | Live and historical read predicates; `MN001` conflict mapping |
| federation | `mnemos/api/routes/federation.py` | `federation_peers`, `federation_sync_log` | Pull-based peer sync and schema preflight |
| webhooks | `mnemos/api/routes/webhooks.py`, `mnemos/webhooks/` | `webhook_subscriptions`, `webhook_deliveries` | Outbound delivery, leases, repair/recovery |
| portability | `mnemos/api/routes/portability.py`, `mnemos/domain/portability/` | - | `/v1/export` + `/v1/import` (MPF v0.1) |
| document_import | `mnemos/api/routes/document_import.py` | - | Docling-based PDF/DOCX/HTML extraction |

### 3.4 Persistence feature matrix

| Capability | PostgresBackend | SqliteBackend |
|---|---|---|
| Driver | asyncpg | aiosqlite |
| Vector search | pgvector | sqlite-vec when available, cosine fallback otherwise |
| Full-text search | PostgreSQL FTS / tsvector | FTS5 |
| JSON | jsonb | JSON text + JSON1 |
| Transactions | ACID, row locks, advisory locks | WAL, serialized writer mutex |
| Tenancy enforcement | application predicates + optional RLS | application predicates only |
| Notifications | LISTEN/NOTIFY | polling |
| Multi-worker profile | supported with Redis | not recommended; edge/dev are single-worker |

### 3.5 Workers, compression, and client protocols

| Surface | Entry point | Role |
|---|---|---|
| API | `mnemos.api.main:app` via `mnemos serve` | REST service on port 5002 |
| CLI | `mnemos.cli.main:app` | `serve`, `install`, `worker`, `export`, `import`, `consult`, `health`, `version` |
| MCP | `mnemos.mcp.stdio`, `mnemos.mcp.http` | 18 tools from `mnemos/mcp/tools/` |
| Distillation worker | `mnemos/workers/distillation.py` | Drains `memory_compression_queue`; runs APOLLO + ARTEMIS contests |
| Registry sync | `scripts/sync_provider_models.py` | Scheduled provider + Arena/LMArena sync |

Compression engines live under `mnemos/domain/compression/`: ARTEMIS is
CPU-only extractive compression; APOLLO is schema-aware dense encoding with
optional LLM fallback. Contest orchestration, scoring profiles, and persistence
live in that package and the backend repositories.

## 4. Data Model

### 4.1 Tables (32)

| # | Table | Tenancy | Purpose |
|---|-------|---------|---------|
| 1 | `memories`                       | owner_id, namespace | Core memory content + FTS + embedding |
| 2 | `memory_versions`                | (inherits)          | DAG version history; `commit_hash`, `parent_version_id`, `merge_parents UUID[]`, `branch` |
| 3 | `memory_branches`                | (inherits)          | Branch HEAD pointers |
| 4 | `memory_compression_queue`       | owner_id            | Work queue for `mnemos/workers/distillation.py` |
| 5 | `memory_compression_candidates`  | —                   | Full contest audit (winner + losers + reject_reason + scores) |
| 6 | `memory_compressed_variants`    | —                   | Current winning variant per memory (hot-path read target) |
| 7 | `memory_stats`                   | —                   | Cached aggregates for `/stats` |
| 8 | `kg_triples`                     | owner_id, namespace | Subject/predicate/object with embeddings on each leg |
| 9 | `entities`                       | owner_id, namespace | Named entity registry |
| 10 | `state`                         | owner_id, namespace | Arbitrary k/v |
| 11 | `journal`                       | owner_id, namespace | Append-only operational log |
| 12 | `users`                         | —                   | Accounts (role: user / root / federation), `namespace` column |
| 13 | `api_keys`                      | (FK user_id)        | Hashed Bearer tokens |
| 14 | `groups`                        | —                   | Group memberships |
| 15 | `user_groups`                   | (FK user+group)     | Join table |
| 16 | `oauth_providers`               | —                   | OIDC provider configuration |
| 17 | `oauth_identities`              | (FK user_id)        | Linked external identities |
| 18 | `oauth_sessions`                | (FK user_id)        | Active OIDC sessions |
| 19 | `sessions`                      | user_id, namespace  | Chat session metadata |
| 20 | `session_messages`              | (FK session)        | Per-turn messages |
| 21 | `session_memory_injections`     | (FK session)        | Which memories were injected into which turn |
| 22 | `graeae_consultations`          | owner_id, namespace | GRAEAE consultation rows (prompt + consensus + cost + latency) |
| 23 | `graeae_audit_log`              | (FK consultation)   | Hash-chained audit (prev_hash → current_hash) |
| 24 | `consultation_memory_refs`      | (FK consultation)   | Memories referenced by a consultation |
| 25 | `model_registry`                | —                   | Provider × model catalog with Elo, cost, deprecated flags |
| 26 | `model_registry_sync_log`       | —                   | Scheduled-sync operational history |
| 27 | `federation_peers`              | —                   | Bearer-authenticated peer instances |
| 28 | `federation_sync_log`           | (FK peer)           | Pull history + error log |
| 29 | `webhook_subscriptions`         | owner_id            | URL + events + HMAC secret (per-subscription) |
| 30 | `webhook_deliveries`            | (FK subscription)   | Delivery attempts + status + retries |
| 31 | `compression_quality_log`       | —                   | Per-decision compression-quality records |
| 32 | `morpheus_runs`                 | owner_id, namespace | MORPHEUS/APOLLO S-IVB run state and rollback contract |

**Primary key types**: string (memory IDs follow `mem_...` +
`fed:peer:...` federated prefix conventions), UUID (`gen_random_uuid`)
for version IDs and per-row surrogates.

**Vector column**: `vector(768)` on `memories.embedding` (pgvector
extension). Default embedding dimension is 768; configurable via
`EMBED_MODEL` + `EMBED_DIM` when swapping models.

**Content-addressed column**: `memory_versions.commit_hash` (SHA-256
of memory_id + version_num + content + snapshot_at). Unique index.

### 4.2 Migrations

SQL migrations in `db/` and `db/migrations_sqlite/` are idempotent. Canonical
order is defined in `mnemos/installer/db.py`, mirrored by `docker-compose.yml`
and `docker-compose.staging.yml` initdb mounts:

1. `migrations.sql` (v1 baseline)
2. `migrations_v1_multiuser.sql` (users, api_keys, groups, RLS policies)
3. `migrations_v2_versioning.sql` (memory_versions + trigger)
4. `migrations_v2_sessions.sql` (sessions stack)
5. `migrations_model_registry.sql` (provider/model catalog)
6. `migrations_v3_dag.sql` (DAG columns, branches, octopus-merge support)
7. `migrations_v3_graeae_unified.sql` (consultations + hash-chain audit)
8. `migrations_v3_webhooks.sql` (subscriptions + deliveries)
9. `migrations_v3_oauth.sql` (OIDC provider/session tables)
10. `migrations_v3_federation.sql` (peers + federation role)
11. `migrations_v3_ownership.sql` (ownership / permission comments)
12. `migrations_v3_1_compression.sql` (queue + candidates + variants)
13. `migrations_v3_1_versioning_fix.sql` (convert_to UTF8 bytea cast)
14. `migrations_v3_1_2_kg_tenancy.sql` (KG tenancy)
15. `migrations_v3_1_2_audit_log_columns.sql` (audit column backfill)
16. `migrations_v3_2_user_namespace.sql` (`users.namespace`)
17. `migrations_v3_2_entities_namespace.sql` (`entities.namespace`)
18. `migrations_v3_2_2_version_snapshot_new_values.sql` (UPDATE snapshots record NEW)
19. `migrations_v3_3_morpheus.sql` (MORPHEUS runs)
20. `migrations_v3_3_morpheus_namespace.sql` (MORPHEUS namespace)
21. `migrations_v3_3_recall_tracking.sql` (recall counters)
22. `migrations_charon_trigger_guard.sql` (trigger suppression guard)
23. `migrations_v3_4_federation_compat.sql` (schema preflight fields)
24. `migrations_v3_5_trigger_same_memory_parent.sql` (same-memory branch HEAD guard; `MN001`)
25. `migrations_v3_5_rls_group_select_unix_bits.sql` (RLS group-read parity)
26. `migrations_v3_5_webhook_retry_terminal_state.sql` (retry terminal-state repair)
27. `migrations_v3_5_webhook_attempt_lease.sql` (persisted webhook leases)
28. `migrations_v3_5_webhook_writer_revision.sql` (current-writer marker)
29. `migrations_v3_5_webhook_status_updated_at.sql` (status transition timestamp)
30. `migrations_v3_5_webhook_superseded_marker.sql` (superseded audit marker)
31. `migrations_v3_5_webhook_attempt_unique.sql` (live successor uniqueness)
32. `migrations_v3_5_webhook_succeeded_unique.sql` (single succeeded chain peer)
33. `migrations_v3_5_webhook_succeeded_terminal_trigger.sql` (terminal success guard)
34. `migrations_v3_5_entities_namespace_unique.sql` (entity uniqueness includes namespace)
35. `migrations_v3_5_state_journal_namespace.sql` (state/journal namespace columns)
36. `migrations_v3_5_session_compression_ratio_drop.sql` (drop fiction ratio columns)
37. `migrations_v3_5_session_compression_legacy_drop.sql` (drop legacy session compression fields)
38. `migrations_v3_5_sessions_consultations_namespace.sql` (sessions/consultations namespace)

All migrations pattern: `BEGIN; <add-column>/<backfill>/<set-default>
/<set-not-null>/<add-constraint>; COMMIT;`. Idempotent via
`IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`.

### 4.3 Referential integrity

22+ foreign-key edges across the schema. Each edge declares an
explicit `ON DELETE` semantic (`CASCADE`, `SET NULL`, or `RESTRICT`) —
no loose string joins. The FK from `memory_branches.head_version_id` can
prove the target version exists; the v3.5 trigger replacement proves it
belongs to the same memory before ordinary UPDATE/DELETE writes.

### 4.4 Data-model invariants (must always hold)

| # | Invariant |
|---|-----------|
| I1 | Every `memory_versions` row has a `commit_hash`; the hash is deterministic (same content → same hash). |
| I2 | `memory_versions` has FK-less memory_id (versions survive memory deletion). |
| I3 | `memory_branches` HEAD pointer on `main` is strictly increasing in `version_num` per memory. |
| I4 | `memory_compressed_variants` has at most one current winning row per memory_id (primary key on `memory_id`). |
| I5 | `memory_compression_candidates` records every attempt per contest; a completed contest selects one winner into `memory_compressed_variants`, while historical winning candidates remain in the candidate log. |
| I6 | `graeae_audit_log` is a hash-chain: each row's `prev_hash` equals the previous row's `commit_hash` within the same consultation. |
| I7 | Non-root live-memory reads use the shared owner/federation/world/group predicate from `mnemos/core/visibility.py`; writes remain owner+namespace scoped. |
| I8 | Non-root historical reads use the snapshot's own `owner_id`, `namespace`, and `permission_mode`; live-memory visibility does not authorize hidden old snapshots. |
| I9 | DAG walks and branch-head joins are same-memory scoped; `parent_hash` is only emitted for an immediate parent that is also visible. |
| I10 | Ordinary trigger-driven UPDATE/DELETE must resolve an existing non-NULL same-memory branch HEAD or raise SQLSTATE `MN001`. |
| I11 | `federation_source IS NOT NULL ⇒ owner_id='federation'`. |
| I12 | Non-root writes on tenant-scoped tables filter by `owner_id = current_user.owner_id AND namespace = current_user.namespace`. Root role bypasses both. |
| I13 | Webhook URLs are validated at subscribe time and delivery time, including DNS resolution and metadata-host denial. |
| I14 | Webhook retry chains have at most one live successor attempt per attempt number and one terminal succeeded row per `(subscription_id, event_type, payload_hash)` chain. |
| I15 | API keys persisted as `sha256(token)`; tokens never stored plaintext. |

## 5. Interface Contracts

### 5.1 REST (101 route declarations, 21 routers)

Surface breakdown:

| Area | Endpoints | Representative path |
|------|-----------|---------------------|
| Memories CRUD + search | 10 | `POST /v1/memories/search`, `GET /v1/memories/{id}`, `POST /v1/memories/rehydrate` |
| Versioning + DAG | 9 | `GET /v1/memories/{id}/versions`, `POST /v1/memories/{id}/branch`, `POST /v1/memories/{id}/merge` |
| Knowledge graph | 5 | `POST /v1/kg/triples`, `GET /v1/kg/triples`, `GET /v1/kg/timeline/{subject}` |
| Entities | 7 | `POST /entities`, `GET /entities/{id}`, `POST /entities/{id}/link` |
| State | 4 | `GET /state/{key}`, `PUT /state/{key}` |
| Journal | 3 | `GET /journal`, `POST /journal` |
| Sessions | 5 | `POST /v1/sessions`, `POST /v1/sessions/{id}/messages` |
| Consultations (GRAEAE) | 7 | `POST /v1/consultations`, `GET /v1/consultations/audit/verify` |
| Providers + registry | 5 | `GET /v1/providers`, `GET /v1/providers/health`, `GET /v1/providers/recommend`, `GET /v1/models` |
| Gateway (OpenAI-compat) | 1 | `POST /v1/chat/completions` |
| Federation | 10 | `POST /v1/federation/peers`, `GET /v1/federation/feed`, `GET /v1/federation/schema` |
| Webhooks | 5 | `POST /v1/webhooks`, `GET /v1/webhooks/{id}/deliveries` |
| Ingest + documents | 3 | `POST /ingest/session`, `POST /v1/documents/import`, `POST /v1/documents/batch-import` |
| Portability (MPF) | 2 | `GET /v1/export`, `POST /v1/import` |
| Admin | 15 | `POST /admin/users`, `POST /admin/users/{id}/apikeys`, `POST /admin/compression/enqueue-all` |
| OAuth | 5 | `GET /auth/oauth/{provider}/login`, `GET /auth/oauth/{provider}/callback` |
| Health + metrics | 3 | `GET /health`, `GET /stats`, `GET /metrics` |
| MORPHEUS | 3 | `GET /v1/morpheus/runs`, `GET /v1/morpheus/runs/{id}`, `GET /v1/morpheus/runs/{id}/clusters` |

All REST endpoints use Pydantic request/response models (defined in
`mnemos/domain/models.py`). All non-public endpoints require Bearer auth or
session cookie. Non-root writes are owner+namespace gated; memory reads
use the live or per-snapshot visibility predicates described in §10.2.

Rate limiting: SlowAPI, opt-in via `RATE_LIMIT_ENABLED=true`.

Body size: default 5 MB, `MAX_BODY_BYTES` override. Chunked-transfer
aware streaming limiter (not just Content-Length check).

### 5.2 MCP (stdio and HTTP/SSE, 18 tools)

Entry points: `mnemos.mcp.stdio` and `mnemos.mcp.http`. Both use
the shared tool registry under `mnemos/mcp/tools/`. Tool manifest:

| Tool | Maps to REST |
|------|--------------|
| `create_memory`       | `POST /v1/memories` |
| `bulk_create_memories`| `POST /v1/memories/bulk` |
| `get_memory`          | `GET /v1/memories/{id}` |
| `list_memories`       | `GET /v1/memories` |
| `search_memories`     | `POST /v1/memories/search` |
| `update_memory`       | `PATCH /v1/memories/{id}` |
| `delete_memory`       | `DELETE /v1/memories/{id}` |
| `get_stats`           | `GET /stats` |
| `kg_create_triple`    | `POST /v1/kg/triples` |
| `kg_search`           | `GET /v1/kg/triples` |
| `kg_timeline`         | `GET /v1/kg/timeline` |
| `update_triple`       | `PATCH /v1/kg/triples/{id}` |
| `delete_triple`       | `DELETE /v1/kg/triples/{id}` |
| `log_memory`          | `GET /v1/memories/{id}/log` |
| `branch_memory`       | `POST /v1/memories/{id}/branch` |
| `diff_memory_commits` | DAG commit diff helper |
| `checkout_memory`     | `GET /v1/memories/{id}/commits/{commit}` |
| `recommend_model`     | `GET /v1/providers/recommend` |

MCP contract-wire regression test: `tests/test_mcp_stdio_wire.py`.

### 5.3 Inter-subsystem

- **Consultation ↔ Provider**: reliability stack guards every
  per-provider HTTP call (circuit breaker + rate limiter + semaphore).
- **Gateway ↔ Registry**: `_resolve_provider_for_model` queries
  `model_registry` first for chat routing, falls back to substring
  heuristic, and 400s on complete miss. `/v1/models` discovery is
  stricter: list/detail responses are registry-only and
  `GET /v1/models/{model_id}` returns 404 for unregistered IDs.
- **Gateway ↔ Provider controls**: `temperature`, `max_tokens`, and
  `top_p` flow through `graeae.route(..., generation_params=...)`.
  OpenAI-style adapters pass `stop`, `n`, penalties, `response_format`,
  and supported tool calls; Anthropic/Gemini map equivalent native
  generation fields. Unsupported tools, response formats, penalties, or
  multimodal content blocks fail at the gateway with HTTP 400.

OpenAI-compatible field support matrix:

| Field | OpenAI-style | Anthropic | Gemini | Unsupported providers |
|-------|--------------|-----------|--------|-----------------------|
| `temperature`, `max_tokens`, `top_p` | Native (`max_completion_tokens` for GPT-5) | Native Messages fields | `generationConfig` | Provider default only if no explicit field |
| `stream` | Native SSE | Single-shot SSE fallback | Single-shot SSE fallback | Single-shot SSE fallback |
| `tools`, `tool_choice` | OpenAI provider passthrough | Claude tool schema mapping | 400 | 400 |
| `response_format` | Passthrough | 400 | JSON MIME mapping for `json_object` | 400 |
| `stop`, `n`, penalties | Passthrough | `stop` only; penalties/n rejected | Native `generationConfig` | 400 |
| content blocks / images | OpenAI vision-capable models | Claude vision | Gemini vision | 400 |
- **Gateway ↔ Memories**: `_search_mnemos_context` left-joins
  `memory_compressed_variants` and COALESCEs winner → raw content.
- **Worker ↔ Queue**: `process_contest_queue` dequeues with
  `FOR UPDATE SKIP LOCKED`, runs engines in parallel, persists via
  `persist_contest` in a single transaction. Stranded-running sweep
  at batch head (`_sweep_stale_running`, default 600s threshold).
- **Worker ↔ Engines**: plugin `CompressionEngine` ABC with
  `supports()` pre-filter and `compress()` async method.
- **Federation ↔ Memories**: pulled memories stamped
  `owner_id='federation'` with `federation_source={peer_url}`; non-root
  reads include them via `(owner_id=$1 OR federation_source IS NOT NULL)`.
- **Webhooks ↔ Events**: internal event bus emits deltas; delivery
  worker drains `webhook_deliveries` with exponential backoff +
  HMAC-SHA256 signing.

## 6. State Machines

### 6.1 GPU circuit breaker (`mnemos/domain/compression/gpu_guard.py`)

Per-endpoint, Redis-backed when configured and in-process otherwise. States:
`CLOSED → OPEN → HALF_OPEN → CLOSED` with:

- Failure threshold (N consecutive) → open.
- Probe identity handshake on `HALF_OPEN` (v3.2): each probe carries
  a token; only the token holder's success/failure transitions state,
  preventing stale probes from corrupting recovery.
- Cooldown timer drives `OPEN → HALF_OPEN`.
- `is_available()` returns `(admitted, probe_token)`; callers use
  `record_success` / `record_failure`.

### 6.2 Compression queue

| State | Transition | Trigger |
|-------|------------|---------|
| `pending`  | `→ running` | `FOR UPDATE SKIP LOCKED` dequeue |
| `running`  | `→ done`    | Engine contest finishes, winner persisted |
| `running`  | `→ failed`  | All engines error OR attempts exceeded |
| `running`  | `→ pending` | Stale sweep (v3.1.1): `started_at < NOW() - threshold AND attempts < max` |
| `running`  | `→ failed`  | Stale sweep terminal: `attempts >= max`, stamped `error='stranded_running: ...'` |

Forward-progress invariant: sweep failure is caught + logged; never
blocks the dequeue. Negative-threshold footgun guarded by
`_parse_stale_threshold_secs()` (v3.2 tail).

### 6.3 Memory DAG

Git-like: `memory_versions` with `parent_version_id` (single parent)
or `merge_parents UUID[]` (octopus merge). `memory_branches` tracks
HEAD per branch per memory_id. `commit_hash = sha256(memory_id |
version_num | content | snapshot_at)`; unique index.

Linear invariant on `branch='main'`: `version_num` strictly
increasing per memory. Non-main branches may share version numbers
with main.

Read contract:

- A caller must first be allowed to read the live memory through
  `read_visibility_predicate` unless root.
- Each historical row is then filtered by `version_visibility_predicate`
  against the snapshot's own `owner_id`, `namespace`, and
  `permission_mode`. `memory_versions` does not carry `group_id` or
  `federation_source`, so group/federation historical visibility is
  intentionally not inferred.
- Recursive log CTEs only follow parents whose `memory_id` matches the
  original memory. `parent_hash` is omitted when the immediate parent is
  invisible rather than bridging to the next visible ancestor.

Write contract:

- `merge_branch` and feature-branch `revert_memory` take the shared
  `(memory_id, branch)` advisory lock from `mnemos/api/routes/dag.py`
  before row locks.
- Main-branch revert mutates `memories` under the trigger after checking
  live row drift against the current main HEAD.
- Feature-branch revert is a pure `memory_versions` insert and advances
  only that branch's `memory_branches` row.
- Merge commits copy content/provenance from source and
  owner/namespace/permission from target.
- Trigger-driven UPDATE/DELETE resolves branch HEADs under
  `FOR UPDATE OF mb`; broken branch state raises `MN001` and maps to
  HTTP 409.

### 6.4 OAuth state

Two cookies:
- `mnemos_oauth_state`: Starlette SessionMiddleware cookie; carries
  PKCE verifier + CSRF nonce across the authorize→callback roundtrip;
  `max_age=600`.
- Application session cookie: set after successful login, longer
  lifetime.

### 6.5 Consultation audit hash chain

Each consultation has N audit entries; each entry's `prev_hash`
equals the previous entry's `commit_hash`. `commit_hash = sha256(
prev_hash | prompt | response | provider | quality_score |
timestamp)`. Chain-verify endpoint: `GET /v1/consultations/audit/verify`
(rate-limited 5/min because chain walks are O(N) on a large log).

## 7. Failure Modes

| Subsystem | Failure | Handling |
|-----------|---------|----------|
| Provider outage | Circuit breaker opens | `fast-fail` on subsequent requests; consult() falls back to other providers |
| All providers fail on a consultation | `_compute_consensus` returns `""/0.0/None/0.0/0` | Consultation row still persists with empty consensus; audit chain unbroken |
| GPU endpoint down | `gpu_guard` opens circuit | gpu_required engines return `error='gpu_guard circuit open ...'`; contest records reject_reason='error'; gpu_optional falls back to CPU path |
| Worker dequeues and crashes mid-run | Row stuck in `running` | Next batch's stranded-running sweep reclaims: reset-to-pending if `attempts < max`, terminal-fail otherwise |
| Postgres unreachable at startup | Fail-fast with clear log | No silent degraded mode; service does not start |
| Rate-limit exceeded | `429 Too Many Requests` | With `X-Request-ID` header correlating to server logs (middleware outermost as of v3.2 tail) |
| Body too large | `413 Payload Too Large` | Pure-ASGI streaming limiter handles chunked uploads (no in-memory buffering) |
| OAuth state cookie absent | `400 invalid_request` | Typically caused by `MNEMOS_SESSION_SECRET` rotation mid-flight |
| Federation peer lies | Size caps (1 MB/memory, 64 KB metadata) | Bounded blast radius; cap tripped logs peer identity |
| Webhook URL resolves to private IP | Delivery blocked | URL is validated at subscribe and delivery time, including DNS resolution |
| Memory branch state corrupt | `409 Conflict` | Trigger raises `MN001`; API returns branch reconciliation guidance |
| MCP tool call errors out | Structured error response | MCP contract preserved; client sees `is_error=True` + message |

## 8. External Dependencies

### 8.1 Required runtime

- **Python 3.11+**. The `tomllib` stdlib dependency bounds us.
- **PostgreSQL 15+** with `pgvector` extension. Latency target: <5 ms
  for the worker's dequeue path.
- **Filesystem**: ~1 KB/row for memory text; ~1.5× row count at
  ~2 KB/row for compression candidates; rolling backups 2× live
  corpus.
- **Network (outbound)**: whatever the caller's LLM providers need
  (OpenAI, Together, Groq, etc.); optional GPU inference endpoint for
  APOLLO LLM fallback (`GPU_PROVIDER_HOST`).

### 8.2 Python dependencies (required, 18 packages)

```
fastapi>=0.115.0           # HTTP surface
uvicorn[standard]>=0.30.0  # ASGI server
starlette>=0.40.0          # Middleware + session cookie
pydantic>=2.8.0            # Models / validation
python-multipart>=0.0.9    # File upload handling
asyncpg>=0.29.0            # Postgres async driver (primary)
psycopg[binary]>=3.1.0     # Postgres sync driver (installer)
httpx>=0.27.0              # Outbound HTTP (providers, federation, webhooks)
slowapi>=0.1.9             # Rate limiting
limits>=3.6.0              # SlowAPI backend
redis>=5.0.0               # Optional SlowAPI/cache backend
python-dotenv>=1.0.0       # .env loading
mcp>=1.0.0                 # MCP stdio server
numpy>=1.26.0              # Vector math
psutil>=5.9.0              # Process metrics
authlib>=1.3.0             # OAuth/OIDC
itsdangerous>=2.2.0        # Starlette session signing
prometheus_client>=0.20.0  # /metrics exposition
```

### 8.3 Python dependencies (optional extras)

```
[project.optional-dependencies]
tracing   = [opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http >=1.27.0]
structlog = [structlog >=25.0.0]
docling   = [docling >=2.5.0, docling-core >=2.0.0, pillow >=10.0.0]
ml        = [fastembed >=0.3.0]                                    # CPU embeddings, ONNX, no torch
gpu       = [fastembed-gpu >=0.3.0]                                # NVIDIA CUDA EP via fastembed-gpu
phi       = [openvino-genai >=2024.4.0, fastembed >=0.3.0]         # Intel iGPU via OpenVINO
full      = [spacy >=3.7.0, networkx >=3.3]                        # NLP/graph extras (no embeddings)
sqlite    = [aiosqlite >=0.20.0, sqlite-vec >=0.1.6]
dev       = [pytest >=8.0.0, pytest-asyncio >=0.23.0, pytest-cov >=5.0.0, ruff >=0.5.0]
```

The ML extras are deliberately **torch-free**. ``fastembed`` uses
ONNX runtime for the same MiniLM/Nomic embedding model family that
``sentence-transformers`` exposes via torch, but ships ~10–20 MB
instead of ~700 MB–1 GB of torch + nvidia binary weight. This
matches the production blueprint at PYTHIA :5002 (``phi_server.py``
uses ``fastembed`` + ``openvino_genai`` with no ``import torch``).

GPU acceleration is gated behind the ``[gpu]`` extra (NVIDIA CUDA EP)
or ``[phi]`` extra (Intel iGPU OpenVINO). Apple Silicon hosts use
``[ml]`` (CPU fastembed); MLX / CoreML EP integration is a v4.3+
candidate. Tegra hosts use ``[ml]`` as well; TensorRT / TRT-LLM
integration is out of scope.

Use ``python -m mnemos.runtime.hardware`` (or ``mnemos doctor``)
on the target host to print the suggested extra before running
the pip install.

### 8.4 External service dependencies

- **LLM providers** (any subset): OpenAI, Anthropic-compatible
  proxies, Google Gemini, Groq, Together, Perplexity, local Ollama,
  local vLLM. At least one required for GRAEAE; zero required for
  memory-only use.
- **OpenTelemetry collector** (OTLP/HTTP): optional.
- **Prometheus scraper**: optional.
- **OIDC provider** (Google, GitHub, Okta, etc.): optional, for the
  OAuth login flow.
- **Peer MNEMOS instances**: for federation; zero required.

## 9. Configuration

### 9.1 Environment-variable surface (~35 `MNEMOS_` vars)

Grouped by concern:

**Bind + DB**
- `MNEMOS_BIND` (127.0.0.1), `MNEMOS_PORT` (5002)
- `MNEMOS_DB_HOST`, `MNEMOS_DB_PORT`, `MNEMOS_DB_NAME`,
  `MNEMOS_DB_USER`, `MNEMOS_DB_PASSWORD`

**Auth**
- `MNEMOS_API_KEY` (default root), `MNEMOS_KEY`, `MNEMOS_KEYS_PATH`
- `MNEMOS_SESSION_SECRET`, `MNEMOS_SESSION_HTTPS_ONLY`

**Compression / queue / workers**
- `MNEMOS_CONTEST_ENABLED` (true)
- `MNEMOS_CONTEST_MIN_CONTENT_LENGTH` (0)
- `MNEMOS_CONTEST_STALE_THRESHOLD_SECS` (600)
- `MNEMOS_APOLLO_ENABLED` (true)
- `MNEMOS_APOLLO_LLM_FALLBACK_ENABLED` (true)

**Observability**
- `MNEMOS_STRUCTURED_LOGS` (false)
- `OTEL_EXPORTER_OTLP_ENDPOINT` (via env, standard OTel)

**GRAEAE / consultations**
- `MNEMOS_GRAEAE_URL`
- Per-provider env vars for API keys (consumed by registry)

**Misc**
- `MNEMOS_PROFILE` (core|standard|full — install profile)
- `MNEMOS_CONFIG`, `MNEMOS_INSTALL_DOCLING`
- `MNEMOS_CREATE_DB`, `MNEMOS_CREATE_SERVICE`, `MNEMOS_SERVICE_USER`
- `MNEMOS_REDIS_URL` (optional SlowAPI backend)
- `MNEMOS_ELO_PATH` (Arena.ai Elo import path)
- `MNEMOS_LISTEN_PORT` (registry-sync sub-process)
- `MNEMOS_INSTALLER_CLAUDE_MODEL`
- `MNEMOS_BASE`, `MNEMOS_CLIENT_*` (client-side config for MCP)

Plus non-`MNEMOS_`-prefixed standards: `GPU_PROVIDER_HOST`,
`GPU_PROVIDER_PORT`, `GPU_PROVIDER_TIMEOUT`, `MAX_BODY_BYTES`,
`CORS_ORIGINS`, `RATE_LIMIT_ENABLED`, `OTEL_*`.

### 9.2 On-disk config files

- `~/.config/mnemos/api_keys.json` (Provider Registry File; MNEMOS-
  native format; per-vendor env-var fallback)
- `~/.mnemos/compression_scoring.toml` (scoring profile overrides)

## 10. Security

### 10.1 Authentication

- **Bearer API keys** for programmatic clients; hashed-at-rest
  (`sha256(token)`); rotatable per-user.
- **OAuth / OIDC** for browser login (authlib); pluggable providers.
- **Session cookie** post-login; `httponly`, `same_site=lax`,
  `secure` via `MNEMOS_SESSION_HTTPS_ONLY`.

### 10.2 Authorization

- **Live memory visibility**: non-root reads pass when the caller owns
  the memory, the row is federated, the world-read bit is set, or the
  group-read bit is set for one of the caller's groups; namespace still
  pins non-root reads. Root bypasses.
- **Snapshot visibility**: `memory_versions` reads pass only when the
  snapshot itself is owned by the caller or world-readable in the
  caller's namespace. This is narrower than live memory visibility
  because historical rows lack `group_id` and `federation_source`.
- **Role allowlist**: `user`, `root`, `federation`. Federation role
  bounded to cross-instance-pull calls.
- **Per-memory federation opt-in**: pulls land under
  `owner_id='federation'` and are read-accessible via the loop-guard
  clause `(owner_id=$1 OR federation_source IS NOT NULL)`.

### 10.3 Defense-in-depth

- Pgvector query sanitization (`float()` cast on every component).
- FTS via `plainto_tsquery` (not `to_tsquery`) — operator metacharacters
  treated as literals.
- Federation size caps (1 MB/memory, 64 KB/metadata, 256 chars/name).
- Body streaming limiter (chunked-transfer aware).
- Audit endpoint rate limits (5/min chain-verify, 30/min list).
- SSRF validation on webhook URLs at subscription and delivery time,
  including asynchronous DNS resolution and cloud-metadata deny lists.
- HMAC-SHA256 signing on webhook delivery.
- Hash-chained audit log on every GRAEAE consultation.
- DAG trigger conflict mapping: SQLSTATE `MN001` becomes HTTP 409 with
  reconciliation guidance instead of a silent cross-memory parent write.
- RLS group-select policy uses the same Unix group-read bit math as
  application visibility after
  `db/migrations_v3_5_rls_group_select_unix_bits.sql`.

### 10.4 Known gaps (as of v4.0.0)

- Full deletion-log/GDPR wipe workflow is not present. DELETE tombstone
  snapshots exist in the version DAG when the delete trigger is attached, but
  no separate deletion-log table ships in v4.0.
- Federation per-peer ACLs beyond bearer identity, role gate, namespace
  filters, and category filters remain future work.
- No in-process secrets encryption layer (values read from env + config
  files directly).

## 11. Operational Requirements

### 11.1 Runtime footprint

| Tier | CPU | RAM | Disk | GPU |
|------|-----|-----|------|-----|
| Server      | 8+ cores  | 16+ GB | 50+ GB SSD | CUDA 12+, 8+ GB VRAM recommended |
| Workstation | 4+ cores  | 8 GB   | 20 GB SSD  | Optional (4+ GB VRAM) |
| Edge        | 2 cores   | 4 GB   | 10 GB      | None (contest disabled) |

### 11.2 Throughput baseline

- API request latency (cached path): 5–30 ms.
- API request latency (DB path): 20–100 ms.
- Vector search: <50 ms for corpus <=100k rows with HNSW on Postgres; SQLite
  profile performance depends on sqlite-vec availability and local storage.
- Compression contest throughput depends on APOLLO fallback/judge use;
  ARTEMIS and APOLLO schema matches are CPU-cheap.

### 11.3 Horizontal scaling

Single-worker remains the default for `edge` and `dev`. Multi-worker is
supported for `server` with Redis via
`RATE_LIMIT_STORAGE_URI=redis://host:6379/1`; in-process `memory://` fallback
logs a startup warning when `MNEMOS_WORKERS > 1` because rate-limit,
circuit-breaker, and concurrency state can drift between processes.

### 11.4 Backup / restore

- `pg_dump` + rolling storage for Postgres (see `mnemos/tools/backup/`).
- SQLite profile backups must include the `.sqlite3`, WAL, and shm files or
  checkpoint first.
- MPF v0.1 `/v1/export` endpoint for portable snapshots (not a
  backup substitute — no audit-log preservation in v0.1).

## 12. Complexity Indicators

Raw metrics at v4.0.0, measured from the checked-out tree unless noted.

| Metric | Value | Notes |
|--------|-------|-------|
| Total Python LOC | ~67,000 | Excludes virtualenvs; simple `wc -l` over Python files |
| Production LOC | ~40,800 | `mnemos/` package + scripts/tools |
| Test LOC | ~24,900 | tests/ only |
| Python files | 185+ | Primary modules + tests |
| Test files | 86 | Unit + integration + live-gated E2E |
| Test count | 1055+ passing cases in the doc-sweep tier | `pytest` collection includes parametrized cases; DB-gated tests are selectively ignored in CI/doc sweeps |
| REST endpoints | 102 mounted application routes | Across 21 routers; excludes generated FastAPI docs/openapi routes |
| MCP tools | 18 | Memory CRUD + KG + stats + DAG + model recommendation |
| DB tables | 32 | See §4.1 |
| Migrations | 39 Postgres SQL files + SQLite mirror chain | Idempotent, ordered |
| Named concepts | ~40 | See Appendix H |
| External service protocols | 4 | Postgres wire, HTTP (providers + peers + webhooks + GPU), OAuth/OIDC, MCP stdio |
| Required Python deps | 18+ | See §8.2 |
| Optional dep groups | 6 | build, sqlite, tracing, structlog, docling, full/phi |
| Env vars (MNEMOS_ prefix) | ~30 | See §9.1 |
| FK edges | 22+ | Explicit ON DELETE on every edge |
| Invariants | 10 | See §4.4 |
| State machines | 5 | Circuit breaker, queue, DAG, OAuth state, audit hash-chain |

### 12.1 Effort-estimation dimensions (for a scoping tool)

Dimensions a scoping tool should weigh separately:

- **Stateful surface**: Postgres schema (32 tables, 22+ FKs, 17
  migrations) + pgvector integration + advisory locks + hash-chained
  audit = the single largest complexity driver.
- **Integration breadth**: 4 external service protocols × N provider
  adapters (currently 8+) × 3 MCP-addressable clients = wide
  integration surface.
- **Correctness-critical paths**: compression contest, DAG merge,
  audit hash-chain — each has a persistent audit and cannot silently
  diverge.
- **Reliability primitives**: per-endpoint circuit breakers, rate
  limiters, concurrency semaphores, streaming body limiter, SSRF
  allowlist, HMAC signing — individually small, collectively many.
- **Tenancy enforcement**: owner/namespace write gates plus
  live-memory and per-snapshot read predicates. Uniform, but present at
  every memory and history surface.
- **Observability plumbing**: request-ID ContextVar threaded through
  middleware + logs + metrics + traces (4 instruments, one
  ContextVar source).

### 12.2 Re-implementation effort sketch

A scoping tool estimating cost from scratch should bucket:

| Subsystem bucket | Relative effort | Rationale |
|------------------|-----------------|-----------|
| Core memory CRUD + FTS + embedding | 1.0× (baseline) | Straightforward; vectordb + Postgres |
| GRAEAE consensus + hash-chain audit | 2.0× baseline | Multi-provider adapters + reliability stack + cryptographic audit |
| DAG versioning + branch/merge | 2.0× baseline | Content-addressed + octopus merges + merge conflict resolution |
| Compression platform + contest | 2.5× baseline | Plugin ABC + scoring profiles + persistent audit + GPU guard + stranded-running sweep + engine implementations |
| OAuth/OIDC + Bearer + tenancy | 1.5× baseline | authlib handles bulk of OAuth; uniform two-axis tenancy adds ~5% to every data-plane handler |
| Federation (pull + loop-prevention) | 1.5× baseline | Well-scoped but requires per-peer trust model |
| Webhooks (SSRF + HMAC + retries) | 1.0× baseline | Narrow-surface, well-understood pattern |
| Observability (4-instrument unified) | 1.0× baseline | Mostly wiring; complexity in middleware ordering (LIFO trap) |
| OpenAI-compat gateway + registry | 1.5× baseline | Registry-backed routing + compression injection is non-obvious |
| MCP stdio server | 0.5× baseline | Thin wrapper over REST; contract-wire regression test is the complexity |
| Install / config / ops | 1.0× baseline | Not trivial (profiles, migrations, service unit, Docker) |

Roughly **11-14 full-bucket subsystems** at baseline equivalence. The
current branch is about 41k LOC of production Python plus 25k LOC of
tests; v3.5.0 added hardening and tenancy closure rather than a new
standalone user-facing subsystem.

## 13. Version history (summary)

See `CHANGELOG.md` for the authoritative list. Selected milestones:

- **v3.0.0** — unified API surface, DAG versioning, federation scaffolding.
- **v3.1.0** — compression platform (LETHE + ANAMNESIS + ALETHEIA),
  plugin `CompressionEngine` ABC, contest with persisted audit.
- **v3.1.1** — ops hardening: stranded-running sweep, GPUGuard
  single-probe handshake, precondition fingerprint.
- **v3.1.2** — Tier 3 tenancy (KG, namespace, registry-backed models).
- **v3.2.0** — per-user namespace end-to-end, observability stack
  (request-ID / Prometheus / OpenTelemetry / structlog), compression
  in hot retrieval paths, registry-backed gateway, MPF v0.1 export /
  import, Custom Query mode on consultations, engine-consistent
  consultation persistence, middleware LIFO fix, probe-identity
  handshake on GPUGuard.
- **v3.2 tail** — ALETHEIA retired from default contests; APOLLO S-IC + S-II landed.
- **v3.2.1** — registry-driven GRAEAE muse manifest with ELO-override
  + newer-version override + live-probe + n-1 fallback; non-blocking
  startup reload; gateway provider/model namespacing; gateway prefix-
  strip is provider-aware; new endpoints `/v1/consultations/muses`,
  `/v1/consultations/modes`, `POST /admin/graeae/reload-providers`.
- **v3.2.2** — three regression fixes from Codex deep-review:
  `mnemos_version_snapshot()` UPDATE branch now records NEW state
  (was duplicating OLD into every version row); federation
  `next_cursor` emitted with explicit `Z` so non-UTC pullers stay
  aligned across pages; Custom Query selection (`models` /
  `providers` / `tier`) translates registry provider names
  (`anthropic`) to GRAEAE engine keys (`claude`) so the dispatch
  filter doesn't silently drop muses.
- **v3.2.3** — packaging cleanup: single-source `_version.py`
  consumed by `api_server.py`, `health.py`, `portability.py`;
  `.dockerignore` to drop stale `*.egg-info`; Dockerfile installs
  the package after `COPY . .` so `importlib.metadata.version()`
  agrees with `pyproject.toml`. `/v1/documents/import` now uses
  the canonical `mem_<hex12>` id, sets `verbatim_content` /
  `quality_rating` / `permission_mode`, invalidates search cache,
  and dispatches `memory.created` webhooks per chunk — matching
  the single-create endpoint's contract.
- **v3.3.0** — MORPHEUS slice 2 real cluster + synthesise phases,
  recall tracking, cluster introspection, namespace scoping, MCP
  HTTP/SSE bridge, and the APOLLO/ARTEMIS compression-stack settlement.
- **v3.4.0** — CHARON v0.2 MPF sidecar round-trip for KG triples,
  documents, facts, events, compression manifests, and memory-version
  DAGs; staging compose for PROTEUS; sidecar ownership and conflict
  checks.
- **v3.4.1** — federation schema-compat preflight and dev↔prod MPF
  restore drill.
- **v3.5.0 slice 1** — session history DESC fix and
  `mnemos-os/mnemos` URL sweep (`a62a099`).
- **v3.5.0 slice 2** — shared live-memory read visibility,
  per-snapshot version visibility, same-memory DAG guards, race-safe
  branch creation, feature-branch revert as pure DAG insert, merge
  target-tenancy semantics, `MN001` → HTTP 409, and Docker
  `postgres-upgrade` for existing volumes (`d42c475`).
- **v3.5.0 final hardening** — webhook retry leases/outbox discipline,
  consultation audit scoping, MCP stdio/HTTP registry parity, faithful
  OpenAI-compatible gateway handling, PostgreSQL streaming-replication
  doctrine, namespace-uniform state/journal/entities/sessions/consultations,
  bulk webhook parity, compression cleanup, and audit closure passes.
- **v3.5.1** — documentation-triage patch and version metadata correction;
  no product behavior changes from v3.5.0.
- **v4.0.0** — structural package refactor, `PersistenceBackend`
  abstraction, Postgres + SQLite backends, `server` / `edge` / `dev`
  deployment profiles, Redis-coordinated multi-worker support, single-binary
  distribution, unified `mnemos` CLI, seven import-linter contracts, Pydantic
  Settings singleton, and seven validated GRAEAE modes.

---

# Appendices

## A. Subsystem inventory (enumerated)

1. memories        2. versions       3. dag
4. kg              5. entities        6. state
7. journal         8. consultations   9. providers
10. openai_compat  11. sessions       12. health
13. admin          14. oauth          15. federation
16. webhooks       17. ingest         18. portability
19. document_import 20. distillation worker 21. registry_sync
22. MCP stdio/HTTP server 23. persistence backends 24. unified CLI

## B. REST endpoint inventory (102 mounted application routes, router-grouped)

(see §5.1 for the count breakdown; full list in the router modules
at `mnemos/api/routes/*.py`)

## C. Table inventory (32)

api_keys, compression_quality_log, consultation_memory_refs,
entities, federation_peers, federation_sync_log, graeae_audit_log,
graeae_consultations, groups, journal, kg_triples, memories,
memory_branches, memory_compressed_variants,
memory_compression_candidates, memory_compression_queue,
memory_versions, model_registry, model_registry_sync_log,
morpheus_runs,
oauth_identities, oauth_providers, oauth_sessions,
session_memory_injections, session_messages, sessions, state,
user_groups, users, webhook_deliveries, webhook_subscriptions,
memory_stats.

## D. Migration inventory

Postgres and SQLite migration chains are ordered as applied (see §4.2 for the
Postgres list and `db/migrations_sqlite/` for the SQLite mirror).

## E. Test inventory (74 test files)

Unit + integration + live-GPU-gated E2E:

admin_compression_enqueue, admin_federation_role,
admin_user_namespace, apollo_adversarial, apollo_code,
apollo_commit, apollo_decision, apollo_engine, apollo_event,
apollo_fallback, apollo_person, apollo_portfolio, artemis_engine,
audit_high_fixes, branch_visibility, charon_roundtrip,
compression_base, compression_hot_paths,
compression_manifests_endpoint, contest, contest_judge, contest_store,
custom_query, dag_cross_memory, dag_tenancy, dag_visibility_gap,
document_import, e2e, federation,
gateway_provider_routing, gpu_guard, installer_api_keys_schema,
integration, judge, judge_cross_encoder, kg_tenancy, knossos_phase1,
live_e2e, mcp_stdio_wire, migration_lists_sync, models_registry,
morpheus_clusters_endpoint, morpheus_slice2, namespace_enforcement,
narrate_endpoint, oauth, observability, portability, recall_tracking,
search_owner_filter, trigger_concurrency_lock, unit,
update_memory_trigger_conflict, user_namespace, v3_integration,
versions_tenancy, webhooks, webhooks_entities_namespace, worker_contest.

## F. Python dependency list

See §8.2 (required) and §8.3 (optional).

## G. Environment variable surface

See §9.1.

## H. Named concepts (glossary)

**MNEMOS** — the memory operating system as a whole; named after
Mnemosyne, Titan goddess of memory.
**GRAEAE** — the multi-LLM consensus reasoning layer; Greek myth, the
three sisters sharing one eye.
**THE MOIRAI** — the compression platform (ARTEMIS / APOLLO); named
after the three Fates.
**ARTEMIS** — CPU extractive compression engine.
**APOLLO** — schema-aware dense encoding engine; god of oracles.
**MPF** — MNEMOS Portability Format (v0.1).
**CERBERUS** — deployment hostname for the test instance (RTX 4500 ADA).
**PYTHIA** — deployment hostname for the production instance.
**DAG** — content-addressed directed acyclic graph of memory versions.
**Custom Query mode** — operator-specified provider/model/tier
selection on `/v1/consultations`.
**Provider Registry File** — MNEMOS-native config for per-vendor API
keys (`~/.config/mnemos/api_keys.json`).
**GPUGuard** — per-endpoint circuit breaker governing GPU-consuming
compression engines.
**Stranded-running sweep** — v3.1.1 queue-recovery mechanism that
reclaims rows stuck in `running` past a threshold.
**Tier 3 tenancy** — per-user `namespace` column + enforcement (v3.1.2).
**Custom Query** — operator-specified lineup on `/v1/consultations`.
**Namespace** — tenancy axis orthogonal to `owner_id` (added v3.2).
**Federation source** — loop-guard metadata on pulled memories.

---

*End of specification. Revisions land in the same commit that changes
the behavior described; PRs modifying behavior without updating the
spec are blocked by convention.*

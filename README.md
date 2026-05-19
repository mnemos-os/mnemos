<p align="center">
  <img src="docs/images/logo.png" alt="MNEMOS" width="220" />
</p>

# MNEMOS + GRAEAE

**MNEMOS v5.0.1 is the memory operating system for serious agentic work: a
packaged FastAPI runtime, multi-backend persistence layer, GRAEAE reasoning bus,
operator-audited compression stack, divergent dream-state pipeline (REPLAY →
CLUSTER → CONSOLIDATE → SYNTHESISE → EXTRACT), GDPR right-to-be-forgotten
worker, PERSEPHONE archival subsystem, PANTHEON unified LLM facade, KRONOS
recall observability, and CLI-first deployment surface.**

MNEMOS is not just a place to put bytes. It is a runtime of named subsystems that
manage the full lifecycle of agent memory across providers, agents, and time
horizons: **write, embed, search, compress, version, reason-over, audit,
federate, export, import, and operate**.


## Quick Start

Memory and reasoning runtime for AI agents: persistent search, versioned storage, webhook fanout, and a unified LLM routing bus — all behind a single MCP interface.

---

### 1. Agent-driven install

Paste into Claude Code, Cursor, or Codex. The agent runs the install; you confirm.

```
Install MNEMOS on this machine.

Steps:
1. pip install 'mnemos-os[server]==5.0.1'
2. mnemos init                         # scaffold config + token
3. mnemos serve                        # start API on :5002
4. mnemos doctor                       # verify subsystems
5. Set MNEMOS_BASE=http://localhost:5002 and MNEMOS_API_KEY=<token from step 2>
   in shell env and any agent config that needs to reach it.

Edge device (SQLite, no Postgres): pip install 'mnemos-os[edge]==5.0.1' instead.
Full install with all subsystems: pip install 'mnemos-os[full]==5.0.1'
```

---

### 2. Connect an agent via MCP

Add to `~/.claude/mcp_servers.json` (Claude Code) or equivalent:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://<host>:5002",
        "MNEMOS_API_KEY": "<token>"
      }
    }
  }
}
```

For HTTP/SSE transport (ChatGPT, remote agents): `mnemos serve mcp-http` on `:5004`.

Key MCP tools the agent gets:

| Tool | What it does |
|---|---|
| `search_memories` | Semantic + filtered search across the memory store |
| `create_memory` | Write a new memory with category, tags, and content |
| `get_memory` | Fetch a memory by ID |
| `kg_search` | Query the knowledge-graph triple store |
| `kronos_anomalies` | Surface recall anomalies and memory health signals |

---

### 3. Webhooks + integrations

| Integration | What connects | How |
|---|---|---|
| **Claude Code** | Hooks fire on session-start, prompt-submit, stop — auto-log to MNEMOS | `integrations/claude-code/` — copy hooks + set `MNEMOS_BASE` |
| **ZeroClaw** | Zeroclaw agent reads/writes memories via MCP | `integrations/zeroclaw/` + `mnemos serve mcp-stdio` in zeroclaw config |
| **OpenClaw** | OpenClaw gateway routes memory ops through MCP | `integrations/openclaw/` + MCP server entry in `openclaw.json` |
| **Hermes** | Optional memory skill mounts MNEMOS as a tool provider | `integrations/hermes/optional-skills/memory/mnemos/` |
| **Webhooks (any)** | Push `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed` events to any HTTPS endpoint | `POST /api/webhooks/register` with `{"url": "...", "events": [...]}` |
| **Cursor / Cline / Continue.dev / Zed / Aider** | Any MCP-capable IDE connects via stdio or HTTP transport | See `docs/connectors/` |

---

Full documentation: [docs/](docs/)

## What MNEMOS Is

- **MNEMOS** is the memory kernel and the overall system name. Storage,
  versioning, operator-batched compression, portability, and lifecycle sit here.
- **GRAEAE** is the reasoning bus: a multi-provider routing and consensus layer
  with routing-strategy modes (`auto`, `local`, `external`, `all`) and reasoning
  shapes (`single`, `debate`, `majority`).
- **THE MOIRAI** is the compression subsystem. The current built-in stack is
  **APOLLO + ARTEMIS**, with a written receipt on every transformation.
- A **self-maintaining model registry**, when the shipped sync timer is enabled,
  keeps itself current from provider APIs and Arena.ai Elo rankings.
- **Federation**, **webhooks**, **OAuth**, **RLS**, **DAG versioning**, MCP, and
  the `/v1/` REST surface are services built on top of the kernel, not retrofits
  onto a vector store.

The v5.x codebase is a coherent `mnemos/` package: `api/routes`, `core`, `db`,
`domain` (compression, morpheus, persephone, pantheon, kronos, federation,
graeae, portability), `persistence`, `mcp`, `webhooks`, `workers`,
`installer`, `nats`, `tools`, and `cli`. The old top-level script sprawl is
gone; operators use the single `mnemos` command.

## What You Get

- A FastAPI service on port 5002 with three deployment profiles: `server`,
  `edge`, and `dev`.
- A `PersistenceBackend` abstraction with Postgres (`asyncpg`, pgvector, RLS,
  LISTEN/NOTIFY) and SQLite (`aiosqlite`, sqlite-vec, FTS5, JSON1, WAL)
  implementations.
- Multi-worker production support when Redis backs shared rate-limit,
  circuit-breaker, and concurrency state. The in-process fallback remains for
  single-worker dev and edge installs and logs a warning if used with multiple
  workers.
- A single `/v1/*` REST surface covering memories, consultations, providers,
  sessions, webhooks, federation, portability, and an OpenAI-compatible
  chat-completions gateway.
- Git-like DAG versioning on memory: `log`, `branch`, `merge`, `revert`. Every
  mutation snapshots; history reads are filtered per snapshot and branch writers
  refuse cross-memory parent edges.
- Operator-batched compression contests through the `CompressionEngine` ABC,
  competitive APOLLO/ARTEMIS selection, and persisted winner/loser audit rows.
- Single-binary builds for `linux-x86_64`, `linux-aarch64`, and
  `macos-aarch64` with sqlite-vec bundled.
- Seven import-linter contracts in CI enforcing the package architecture, plus a
  Pydantic Settings singleton that replaces ad-hoc environment reads outside the
  config and installer layers.
- 1055 unit tests passing, with GitLab integration tiers for cross-namespace
  isolation, multi-worker smoke, and Postgres/SQLite persistence parity.

The current release line is **v5.0.1**. See [`CHANGELOG.md`](./CHANGELOG.md)
for release history and [`ROADMAP.md`](./ROADMAP.md) for forward-looking scope.

## Quick Install

```bash
pip install 'mnemos-os[edge]==5.0.1'
mnemos serve --profile dev
```

| Shape | Command |
|---|---|
| Core | `pip install mnemos-os==5.0.1` |
| Edge | `pip install 'mnemos-os[edge]==5.0.1'` |
| Server | `pip install 'mnemos-os[server]==5.0.1'` |
| ML | `pip install 'mnemos-os[ml]==5.0.1'` |
| Full | `pip install 'mnemos-os[full]==5.0.1'` |

Mix bundles as needed, for example `pip install 'mnemos-os[server,ml]==5.0.1'`.
See [`docs/INSTALL.md`](./docs/INSTALL.md) for the full matrix and migration
notes.

```bash
docker pull ghcr.io/ncz-os/mnemos:5.0.1
```

For a single binary with no host Python:

```bash
curl -L https://github.com/ncz-os/mnemos/releases/download/v5.0.1/mnemos-linux-x86_64 -o mnemos
chmod +x mnemos
./mnemos install --profile edge
./mnemos serve --profile edge
```

See [`docs/SINGLE_BINARY.md`](./docs/SINGLE_BINARY.md) for platform-specific
filenames, macOS quarantine notes, limitations, and build-from-source
instructions.

## Deployment Topologies

| Profile | Backend | Coordination | Use it for |
|---|---|---|---|
| `server` | Postgres + pgvector | Redis-backed shared state | Production, teams, multi-worker services |
| `edge` | SQLite + sqlite-vec | In-process single-worker state | Laptops, Pi-class hosts, local appliances, Termux-style installs |
| `dev` | SQLite + sqlite-vec | In-process single-worker state + DEBUG logging | Local development and tests |

## Architecture

```text
Agents / MCP clients / OpenAI-compatible SDKs
        │
        │  REST, MCP stdio, MCP HTTP/SSE
        ▼
mnemos.api.routes  ->  mnemos.domain  ->  mnemos.db
        │                    │                │
        │                    ▼                ▼
        │             GRAEAE / MOIRAI    PersistenceBackend
        │                                  ├─ PostgresBackend
        │                                  └─ SqliteBackend
        ▼
mnemos.core lifecycle/config/visibility
        │
        ├─ mnemos.webhooks
        ├─ mnemos.workers
        ├─ mnemos.mcp
        └─ mnemos.cli
```

---

## What works now

This is the current state of the v5.x release line (current release `v5.0.1`). Features described here are implemented unless explicitly called out as forward-looking in [`ROADMAP.md`](./ROADMAP.md).

The API surface is namespaced under `/v1/*`.

### Memory API (v1)

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/memories` | Store a memory with category, subcategory, content, and optional provenance |
| `GET /v1/memories` | List memories, filterable by category and subcategory |
| `GET /v1/memories/{id}` | Retrieve a single memory |
| `POST /v1/memories/search` | Full-text or semantic search with category/score filters |
| `POST /v1/memories/bulk` | Bulk create memories |
| `PATCH /v1/memories/{id}` | Update memory content or metadata |
| `DELETE /v1/memories/{id}` | Delete a memory |
| `POST /v1/memories/rehydrate` | Token-budgeted context load for prompt injection; uses the compression contest's winning variant when present |
| `POST /ingest/session` | Ingest a session transcript |
| `GET /v1/memories/{id}/log` | DAG commit history for a memory |
| `POST /v1/memories/{id}/branch` | Create a branch from a specific commit |
| `POST /v1/memories/{id}/merge` | Merge a branch back to main |
| `GET /v1/memories/{id}/versions` | Version history |
| `GET /v1/memories/{id}/compression-manifests` | v3.1 contest audit: current winning variant + every historical contest's candidates with scoring fields and reject reasons. `?include_content=true` for full content, default is a 200-char preview |
| `GET /health` | Health check (not namespaced) |
| `GET /stats` | Memory counts by category, compression statistics |

Read access for `GET /v1/memories`, `GET /v1/memories/{id}`, search, rehydrate, and gateway context now flows through the same application predicate: owner, federated, world-readable, or Unix group-readable (`mnemos/core/visibility.py`). Writes remain owner-scoped; being able to read a world/group/federated row does not grant update or delete rights.

### Multi-user and provenance (v1, shipped)

Each memory carries full ownership and LLM provenance. Since v3.2, tenancy is two-dimensional: `owner_id` names the account and `namespace` names the caller's logical workspace. Non-root writes require both to match the caller context; root can override for import, repair, and audit work.

- `owner_id` — which user owns this memory
- `group_id` — optional group for shared access
- `namespace` — logical partition (e.g. `myapp/analyst`)
- `permission_mode` — UNIX-style octal (600 = owner only, 640 = group readable, 644 = world readable)
- `source_model` — the LLM model that produced this memory
- `source_provider` — the provider (openai, groq, ollama, etc.)
- `source_session` — session ID at time of creation
- `source_agent` — agent name or identifier

**Row Level Security** is defined in PostgreSQL and remains opt-in for multi-user server deployments. The application layer mirrors the RLS read contract so SQLite/edge installs and RLS-backed server installs behave consistently. The v3.5 hardening work closed task #25 with `db/migrations_v3_5_rls_group_select_unix_bits.sql`: `read_visibility_predicate` and the `mnemos_group_select` RLS policy now use identical Unix group-bit math, `((permission_mode / 10) % 10) >= 4`.

**Deployment topologies** — selected with `mnemos install --profile ...`,
`mnemos serve --profile ...`, or `MNEMOS_PROFILE`:

| Profile | Backend | Shared state | Use case |
|---------|---------|--------------|----------|
| `server` | PostgreSQL | Redis | Production service, shared agents, multi-worker capable |
| `edge` | SQLite | In-process | Laptop, Pi/edge appliance, Consumer MNEMOS, Termux on S21 |
| `dev` | SQLite | In-process | Local development with debug logging |

`personal` is the legacy v3.x profile name and now resolves to `edge`.

Search hits update recall telemetry in the background: `recall_count` increments and `last_recalled_at` is set for the returned memory IDs. The counters are observability and future archival input, not authorization state; failures are logged and do not block the user-visible search response.

### Admin API (v1)

| Endpoint | What it does |
|----------|-------------|
| `POST /admin/users` | Create a user |
| `GET /admin/users` | List all users |
| `POST /admin/users/{id}/apikeys` | Generate an API key (raw key returned once) |
| `GET /admin/users/{id}/apikeys` | List API keys for a user |
| `DELETE /admin/apikeys/{id}` | Revoke an API key (soft-delete) |
| `POST /admin/compression/enqueue` | v3.1: enqueue specific memories into `memory_compression_queue` for the contest path. Body: `{memory_ids, reason, scoring_profile, priority}`. Silently skips unknown IDs |
| `POST /admin/compression/enqueue-all` | v3.1: bulk-enqueue up to `limit` (default 500, max 10,000) memories. `only_uncompressed=true` (default) skips memories that already have a variant; set `false` to re-contest under new rules |

All admin endpoints require root role. On personal installs (no auth), they are accessible without a key.

### Knowledge graph

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/kg/triples` | Create a subject → predicate → object triple |
| `GET /v1/kg/triples` | List triples with filters |
| `GET /v1/kg/timeline/{subject}` | All triples for a subject in temporal order |
| `PATCH /v1/kg/triples/{id}` | Update a triple |
| `DELETE /v1/kg/triples/{id}` | Delete a triple |

### Consultations — reasoning domain (v3, shipped)

Multi-LLM consensus reasoning with cited memory artifacts and cryptographic audit chain.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/consultations` | Create a consultation (prompt + task_type) |
| `GET /v1/consultations/{id}` | Retrieve a consultation record |
| `GET /v1/consultations/{id}/artifacts` | Cited memories used to answer |
| `GET /v1/consultations/audit` | Hash-chained audit log, owner-scoped for non-root callers |
| `GET /v1/consultations/audit/verify` | Verify audit chain integrity; root verifies globally, non-root verifies their own consultation rows |

### Providers — model routing domain (v3, shipped)

Unified provider catalog with health tracking and task-aware recommendation.

| Endpoint | What it does |
|----------|-------------|
| `GET /v1/providers` | List all configured providers with metadata |
| `GET /v1/providers/health` | Per-provider availability + circuit-breaker state |
| `GET /v1/providers/recommend` | Recommend a model for a task-type + budget |

### OpenAI-compatible gateway (v3, shipped)

Drop-in replacement for the OpenAI Chat Completions API — so any SDK that speaks OpenAI can speak to MNEMOS.

| Endpoint | What it does |
|----------|-------------|
| `GET /v1/models` | List registry-backed models only |
| `GET /v1/models/{model_id}` | Registry model details; unregistered IDs return 404 |
| `POST /v1/chat/completions` | Chat completion; routes to the appropriate provider; optional memory injection; supports generation controls, SSE streaming, tools/tool_choice and response_format where provider-supported |

Gateway field support is pass-or-reject: OpenAI-style providers receive `temperature`, `max_tokens`, `top_p`, `stop`, `n`, presence/frequency penalties, and `response_format`; OpenAI and Anthropic receive tool schemas where supported; Gemini maps generation controls and JSON response format to its native fields. Providers that cannot honor a requested tool call, response format, penalty, or multimodal content block return a clear HTTP 400 instead of silently dropping it.

Memory injection is enabled by default. Disable it for one request with either
`X-Mnemos-Inject-Memory: false` or the non-OpenAI extension body field
`"mnemos_inject_memory": false`; malformed header values are treated as
default-on. When the header is supplied, non-streaming JSON responses include
`mnemos_metadata.memory_injected`.

### Stateful sessions (v3, shipped)

Multi-turn conversation state with memory injection at turn boundaries. Sessions carry accumulated context across requests and are scoped by the same owner+namespace pair as memories. v3.5 removed the legacy per-session compression columns; session history is ordered by deterministic pinned system rows plus recent turns, and compression output comes from `memory_compressed_variants` when a read path explicitly asks for compressed memory.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/sessions` | Start a new session |
| `GET /v1/sessions/{id}` | Retrieve session state |
| `POST /v1/sessions/{id}/messages` | Post a turn; memory injection at turn boundary |
| `GET /v1/sessions/{id}/history` | Full message history |
| `DELETE /v1/sessions/{id}` | End a session |

### MORPHEUS — dream-state generator (v3.3, shipped)

MORPHEUS is the append-only dream-state subsystem: it scans a time window of existing memories, clusters related records, synthesises per-cluster summary memories, tags every generated memory with `morpheus_run_id`, and records run state in `morpheus_runs`. It is operator-triggered, not an automatic mutation daemon; rollback deletes memories generated by a run and marks the run `rolled_back`.

| Endpoint | What it does |
|----------|-------------|
| `GET /v1/morpheus/runs` | List runs newest-first |
| `GET /v1/morpheus/runs/{id}` | Inspect one run |
| `GET /v1/morpheus/runs/{id}/clusters` | Inspect cluster membership and synthesized memory IDs |
| `POST /admin/morpheus/runs` | Trigger a run (root only, synchronous in this release) |
| `DELETE /admin/morpheus/runs/{id}` | Roll back a run (root only) |

### Webhooks (v3, shipped)

Outbound notifications when events happen. Receivers verify an HMAC-SHA256 signature to trust the payload.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/webhooks` | Subscribe; secret returned once |
| `GET /v1/webhooks` | List the caller's subscriptions |
| `GET /v1/webhooks/{id}` | Retrieve a subscription |
| `DELETE /v1/webhooks/{id}` | Revoke (soft-delete) |
| `GET /v1/webhooks/{id}/deliveries` | Recent delivery attempts |

Events: `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed`. Delivery is durable: every attempt is logged to `webhook_deliveries`, retried 4 times with exponential backoff (1m / 5m / 30m / 2h), and replayed from disk on restart by the delivery recovery worker. Recovery claims due current-writer rows (`writer_revision=1`) with `lease_token` / `lease_expires_at` in the dequeue transaction before scheduling lifecycle-tracked delivery attempt tasks, and sizes each claim batch to the send semaphore's current free capacity. A live partial unique index on `(subscription_id, event_type, payload_hash, attempt_num)` prevents duplicate pending/retrying successor attempts for the same chain, and a terminal partial unique index on `(subscription_id, event_type, payload_hash)` enforces exactly one `status='succeeded'` row per chain. Workers release the database connection before DNS validation or HTTP POST, and use the lease to bound whether they are allowed to send. Failure terminal updates require an unexpired live lease; 2xx finalization after response headers requires the matching lease token, a still-live row, and no succeeded chain peer. Success finalization writes `status='succeeded'` and abandons any free live successors as one chain-locked database transaction. Recovery-preclaimed sends re-check for live successors under the pre-POST chain lock and abandon/supersede the older attempt before any outbound POST. Active sends are capped per process with `WEBHOOK_MAX_CONCURRENT_SENDS` (default `64`); lease duration is tunable with `WEBHOOK_LEASE_SECONDS` (default minimum `90s`). The claim UPDATE uses PostgreSQL `clock_timestamp()` for both `lease_expires_at` and the returned `claim_db_now`, so advisory-lock waits cannot backdate the lease window. The dispatcher subtracts elapsed time since the pre-claim monotonic anchor plus `WEBHOOK_FINALIZE_BUFFER_SECONDS` (default `5s`) before starting DNS validation or HTTP POST, and releases the lease without consuming a retry if the remaining send window is gone. A separate repair worker runs the burst-then-periodic sweep independently of delivery sends, skips rows with an unexpired lease, and idempotently normalizes out-of-order `pending`/`retrying` overwrites of already superseded attempts whenever a newer successor exists or any chain peer has succeeded. Graceful shutdown cancels perpetual webhook workers first, then waits up to `WEBHOOK_SHUTDOWN_DRAIN_SECONDS` for in-flight delivery attempts to finish finalization before last-resort cancellation. Webhook POST acknowledgement is determined by the HTTP status code as soon as response headers arrive: 2xx succeeds and non-2xx remains retryable regardless of response-body completeness. Response-body capture is best-effort audit data, bounded by `WEBHOOK_RESPONSE_BODY_MAX_BYTES` (default `2048`) and the capture timeout.

Round 24 closure: `db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql` makes `status='succeeded'` terminal at the database layer. Stale writers are structurally prevented from reverting an ACK back to `pending` or `retrying`, while response-body audit updates and lease clearing still work.

Round 20 closes the post-header ACK race by finalizing the status code before response-body capture or stream/client cleanup. The first terminal UPDATE stores `response_status` with `response_body=NULL`; post-finalize body capture fills `response_body` with a no-token audit UPDATE only if it completes within its own timeout, and cleanup remains post-finalize best effort. Round 21 narrowed the 2xx ACK durability window to the chain advisory lock plus the committed success UPDATE. Round 22 moved free-successor cleanup back into that same success transaction. Round 23 drops the per-successor savepoints and removes the post-commit fallback, so the success commit and all free-successor `status='abandoned'` updates are truly atomic. Cleanup failure or cancellation rolls back the whole success transaction and retries from the lease-owned state; in the rare failure path, that can resend one ACKed POST up to the retry limit, with warnings logged for observability. Recovery-preclaimed attempts also re-check for live successors under the pre-POST chain lock and abandon the older attempt instead of sending a duplicate.

Signature header: `X-MNEMOS-Signature: sha256=<hex>`. Verify with `hmac.new(secret, body, sha256).hexdigest()`.

### OAuth / OIDC authentication (v3, shipped)

Browser-based login via external identity providers. Coexists with API-key auth — the same user can have both a key and an OIDC identity.

| Endpoint | What it does |
|----------|-------------|
| `GET /auth/oauth/providers` | List enabled providers (public, no secrets) |
| `GET /auth/oauth/{provider}/login` | Start authorization-code + PKCE flow |
| `GET /auth/oauth/{provider}/callback` | Handle provider redirect; sets `mnemos_session` cookie |
| `POST /auth/oauth/logout` | Revoke session (optionally `?all_devices=true`) |
| `GET /auth/oauth/me` | Who am I (works with either auth method) |

Admin side (`/admin/oauth/*` — root only):

| Endpoint | What it does |
|----------|-------------|
| `POST /admin/oauth/providers` | Register a provider (Google, GitHub, Azure AD, or generic OIDC) |
| `GET /admin/oauth/providers` | List configured providers (client_secret redacted) |
| `PATCH /admin/oauth/providers/{name}` | Update provider config |
| `DELETE /admin/oauth/providers/{name}` | Remove a provider |
| `GET /admin/oauth/identities` | List all OAuth identities (optionally filter by user) |

Sessions are DB-backed, revocable, and expire after 30 days by default. User provisioning: same external-id reuses the user; matching email links to an existing user; otherwise a fresh user is created.

### High availability / replication doctrine

For single-site HA, use PostgreSQL streaming replication: one writable primary,
read-only standbys, and a stable writer endpoint for MNEMOS. Federation remains
first-class, but it is for genuinely remote data flows: multi-site deployments,
multi-org curated feeds, and developer laptop or edge replicas with
intermittent connectivity using the v4 SQLite-backed `edge` profile.

Do not use federation between same-LAN MNEMOS nodes for HA; it creates
application-level dedup work that PostgreSQL WAL streaming already solves below
the app. See [`DEPLOYMENT.md`](./DEPLOYMENT.md#high-availability-and-replication)
and [`docs/STREAMING_REPLICATION.md`](./docs/STREAMING_REPLICATION.md).

### Portability — MPF import/export (v3.4, shipped)

MNEMOS Portability Format (MPF) is the native envelope for moving memories between MNEMOS instances and across compatible external systems. `GET /v1/export` writes an MPF v0.1.x envelope; `POST /v1/import` reads the envelope back. Root callers may use `preserve_owner=true` for authoritative restore and migration work; non-root imports are scoped to the caller's owner+namespace.

CLI helpers live in [`mnemos/tools/memory_export.py`](./mnemos/tools/memory_export.py), [`mnemos/tools/memory_import.py`](./mnemos/tools/memory_import.py), and [`mnemos/tools/mpf_validate.py`](./mnemos/tools/mpf_validate.py). The schema is documented in [`docs/MEMORY_EXPORT_FORMAT.md`](./docs/MEMORY_EXPORT_FORMAT.md), and v3.4.1 added CHARON schema-compat preflight so federating peers can refuse incompatible migration sets before sync.

### Federation — cross-instance memory sync (v3, shipped)

Pull-based one-way federation between genuinely remote MNEMOS instances. Remote peer exposes `/v1/federation/feed`; local instance pulls on a configurable interval, storing remote memories with ids of the form `fed:{peer_name}:{remote_id}` and `federation_source = peer_name`. Federated memories are read-only by application convention.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/federation/peers` | Register a remote peer (root only) |
| `GET /v1/federation/peers` | List registered peers |
| `GET /v1/federation/peers/{id}` | Peer detail |
| `PATCH /v1/federation/peers/{id}` | Update (enable/disable, filters, interval) |
| `DELETE /v1/federation/peers/{id}` | Unregister |
| `POST /v1/federation/peers/{id}/sync` | Manual sync trigger (blocks on completion) |
| `GET /v1/federation/peers/{id}/log` | Sync history for a peer |
| `GET /v1/federation/status` | Aggregate status across all peers |
| `GET /v1/federation/feed` | Serve memories to remote peers (role=`federation` or `root`) |

**Trust model:** mutual — each side registers the other. Side A issues Side B a Bearer token by creating a MNEMOS user with `role='federation'` and minting an API key via the admin API. Side B stores that token in its own `federation_peers.auth_token`. Side A's feed endpoint validates the token and `role IN ('federation', 'root')`.

**Dedup:** re-pulls are safe. Local id `fed:{peer}:{remote_id}` is stable; only rows with a newer `federation_remote_updated` overwrite existing ones.

**Cursoring:** `/v1/federation/feed` uses an opaque compound cursor over `(updated, id)` and orders by the same pair, so pagination cannot skip memories when many rows share one `updated` timestamp. Malformed or missing cursors start an initial fetch from the beginning.

**Filters:** `namespace_filter` and `category_filter` (both arrays) restrict what gets pulled from a peer; NULL = pull everything the peer will serve.

**Loop prevention:** the feed endpoint excludes memories where `federation_source IS NOT NULL`, so federated memories don't propagate hop-by-hop through a chain of peers.

### GRAEAE engine internals (all operational)

The reasoning engine behind `/v1/consultations` provides:

- **Circuit breaker** — per-provider CLOSED/OPEN/HALF_OPEN state machine, 5-minute cooldown
- **Consensus response cache** — per-process LRU keyed on `sha256(task_type + normalized_prompt)`, 1-hour TTL, 500-entry cap. Exact-match dedup, not embedding similarity (embedding round-trip would negate the win for the less-common near-duplicate case).
- **Quality scorer** — success / failure / latency tracking per provider, combined with Arena.ai Elo weights from the model registry (see next section) for dynamic consensus weighting
- **Rate limiter** — single-level request rate limit with graceful backoff
- **Audit chain** — SHA-256 hash-chained prompt/response log for compliance

### Model registry and dynamic provider weighting

Most multi-LLM routers hardcode a provider list. MNEMOS ships a **self-populating persistence-backed model registry** that keeps itself current when the sync job is installed.

**What's in the registry.** Every known model from every configured provider — OpenAI, Groq, xAI, Together, Nvidia, Gemini, Anthropic — with per-model metadata: provider + model_id, display name, family (grok-4, gpt-5, gemini-3, …), capabilities (`chat`, `vision`, `code`, `reasoning`, `web_search`), context window, max output tokens, input / output / cache pricing (USD per million tokens), availability, deprecation flag, **Arena.ai Elo score**, **Arena.ai rank**, and the normalized `graeae_weight` (0.50–1.00) actually used by the consensus scorer.

**How it populates.**

- **Daily provider sync** — `scripts/sync_provider_models.py` (systemd: `graeae-model-sync.timer`) calls `graeae.provider_sync`, hits each provider's model-list endpoint where one exists (Gemini uses `/v1beta/models`; Anthropic uses a static list because Anthropic does not expose a public `/models` surface), and upserts into `model_registry`. New models appear automatically when the timer is installed and provider keys are configured; deprecated ones get flagged.
- **Arena.ai ranking sync** — the same sync script refreshes Arena.ai/LMArena ranking data unless run with `--skip-arena`, maps ranks back to providers + models, and writes `arena_score`, `arena_rank`, and `graeae_weight` into the registry. `scripts/refresh_elo_weights.py` remains available for an Elo-cache-only refresh.
- **Online quality signals** — the GRAEAE engine tracks per-provider success / failure / latency in memory; those signals combine with the registry's Elo-derived `graeae_weight` to pick the winning response on each consultation.

**What it's used for.**

- **`/v1/providers/recommend?task_type=...&budget=...`** — returns the cheapest available model that meets the task's capability + quality floor. Uses `graeae_weight` as the quality signal and `input_cost_per_mtok + output_cost_per_mtok` as the cost signal.
- **GRAEAE consensus scoring** — provider responses are weighted by `graeae_weight` before the consensus pick. A provider that drops on Arena.ai also drops in MNEMOS's internal routing after the sync timer updates the registry, without code changes.
- **OpenAI-compatible gateway model routing** — when a caller passes `model="auto"`, `model="best-coding"`, `model="best-reasoning"`, `model="fastest"`, `model="cheapest"`, the gateway resolves against the registry rather than a hardcoded alias table.
- **OpenAI-compatible model discovery** — `/v1/models` and `/v1/models/{model_id}` expose registry rows only; chat routing can still fall back to provider-name heuristics for explicit requests, but discovery does not invent synthetic models.

**Fresh-install behavior.** If the registry is empty (first boot, no sync run yet), `/v1/providers/recommend` falls back to the static GRAEAE provider config so new deployments don't 404. The first `provider_sync` run typically populates 30–50 models depending on which provider API keys you've configured.

### What runs under the hood (infrastructure you don't have to think about)

A lot of the v3.x surface is held up by background work that doesn't show up in the route table but does show up in the failure modes it prevents. For anyone who wants to know what's there:

- **Webhook retry repair and delivery recovery workers** — the repair worker runs its startup burst and periodic sweep as its own lifespan task, marking lease-free `pending` or `retrying` rows that already have a successor or any other succeeded chain peer as `abandoned` with `superseded=TRUE`, even when an out-of-order writer has overwritten an already superseded row's status. The delivery worker separately claims due current-writer `pending` rows plus due `retrying` rows only when no successor attempt exists, then schedules tracked delivery-attempt tasks instead of owning the HTTP send itself. Recovery writes the short persisted lease in the dequeue transaction, caps each claim batch to currently free send slots, and releases the DB connection before DNS/HTTP; the outbound send runs under a wall-clock deadline derived from that lease so semaphore slots and external POSTs cannot outlive the lease, and a pre-send lease-window miss releases the lease without burning a retry. A 2xx response header is the external ACK, so success finalization can record it with matching token ownership even if the lease expires before finalize commit, provided free-successor cleanup commits in the same transaction. Non-2xx failure paths still require an unexpired lease. Per-chain advisory locks serialize direct claims, preclaimed-send guards, successor inserts, and succeeded-chain convergence checks; preclaimed-send guards also reject older attempts when a live successor appears after recovery claim but before POST. A live partial unique index prevents duplicate successor rows, and a terminal partial unique index enforces one succeeded row per chain.
- **Distillation worker supervision** — the compression worker runs under an exponential-backoff supervisor (1s → 2s → 4s → … capped at 5 min). A crash is logged and retried; the worker does not silently die and leave memories un-compressed for the rest of the process lifetime.
- **Search stays out of the compression control plane** — large `/v1/memories/search` result sets keep the standard raw response shape and no longer probe the retired legacy distillation backend. Compression health and output come from the queue-driven APOLLO/ARTEMIS worker, `memory_compression_queue`, `memory_compressed_variants`, and the APOLLO GPU guard path.
- **OAuth session garbage collector** — hourly sweep of expired and long-revoked sessions. Bounds the `oauth_sessions` table so a long-running install doesn't accumulate dead rows forever.
- **Federation sync worker** — for genuinely remote peers, iterates enabled peers on their individual sync intervals, pulls batches, reconciles local + remote timestamps before overwriting, logs per-sync results to `federation_sync_log`. Single-site HA uses PostgreSQL streaming replication instead.
- **Advisory-lock-serialized audit chain writer** — the hash chain writer takes `pg_advisory_xact_lock` before reading the chain tip, so concurrent consultations cannot compute against the same stale previous hash. Closes a TOCTOU window in tamper-evident logging that most implementations leave open.
- **Advisory-lock-serialized DAG writers** — merges and feature-branch reverts share `_branch_advisory_lock_key` in `mnemos/api/routes/dag.py`, then take row locks in the same order. Concurrent writers on the same `(memory_id, branch)` serialize instead of orphaning branch heads.
- **Trigger-level DAG parent guard** — `db/migrations_v3_5_trigger_same_memory_parent.sql` replaces `mnemos_version_snapshot()` so UPDATE/DELETE resolve branch HEADs under lock, reject missing/NULL/foreign heads with SQLSTATE `MN001`, and keep delete snapshots live for deployments that still attach the delete trigger.
- **ASGI body-size middleware** — native ASGI (not `BaseHTTPMiddleware`), so it rejects chunked uploads whose running byte count exceeds `MAX_BODY_BYTES` *as they arrive*, before the full body lands in memory. Content-Length–declared uploads are rejected before the app is even invoked.
- **SSRF-hardened webhook dispatch** — URLs are re-validated at send time (not just at subscription time); DNS resolves asynchronously so a slow resolver can't freeze the event loop; cloud metadata hostnames (AWS IMDS, Google `metadata.google.internal`, Tencent, Alibaba, IPv6 variants) are on a deny list alongside the RFC1918 / loopback / link-local filter.
- **Rate limiter with X-Forwarded-For trust** — default keys on direct socket peer (safe behind no proxy); set `RATE_LIMIT_TRUST_PROXY=true` only when you run behind a proxy you control. Prevents clients from blowing out the global limit via spoofed headers.
- **pgvector query sanitization** — embedding vectors returned by the embedder are `float()`-cast before being stringified into the query. A poisoned embedder cannot inject SQL via a non-numeric vector "component".
- **Full-text search operator filtering** — `/v1/memories/search` uses `plainto_tsquery` rather than `to_tsquery`, so `|`, `&`, `!` and friends get treated as literal text instead of tsquery operators. User input cannot construct adversarial FTS queries.
- **Federation size caps** — an abusive peer cannot fill your disk: pulled content capped at 1 MB per memory, metadata at 64 KB, name fields at 256 chars.
- **Rate-limited audit endpoints** — `/v1/consultations/audit/verify` walks the entire chain from genesis for root callers and verifies only the caller's consultation audit rows for non-root callers; capped at 5/min so an authenticated caller cannot force O(N) scans on a large log. `/audit` list is owner-scoped for non-root callers and capped at 30/min.
- **Quality manifest on every compression** — every compression engine in the active stack writes a receipt: `{what_was_removed, what_was_preserved, quality_rating, risk_factors, safe_for, not_safe_for}`. Compression-as-data, not compression-as-side-effect.

### Referential integrity (the -ism, spelled out)

Every cross-table reference in the schema is a real PostgreSQL foreign key with an explicit `ON DELETE` semantic — not a loose string column you have to trust the application layer to honour. Twenty-two FK edges across the system, and every one carries a deliberate decision about what happens when the thing it points at goes away. The schema has opinions.

Two patterns, picked per edge:

**`ON DELETE CASCADE`** — when lifecycle is genuinely owned:

- `api_keys.user_id → users(id)` — delete a user, their keys go with them.
- `sessions.user_id → users(id)`, `session_messages.session_id → sessions(id)` — close a session, its messages go.
- `user_groups.user_id → users(id)`, `user_groups.group_id → groups(id)` — membership is owned by both endpoints.
- `webhook_subscriptions.owner_id → users(id)`, `webhook_deliveries.subscription_id → webhook_subscriptions(id)` — subscriber deletion collapses the whole delivery subtree (soft-delete via `revoked=true` is the normal path; CASCADE only matters on hard deletes).
- `oauth_identities.user_id → users(id)`, `oauth_identities.provider → oauth_providers(name)`, `oauth_sessions.user_id → users(id)` — OAuth bindings follow their owner.
- `federation_sync_log.peer_id → federation_peers(id)` — unregister a peer, its sync history goes.
- `graeae_audit_log.consultation_id → graeae_consultations(id)`, `consultation_memory_refs.consultation_id → graeae_consultations(id)` — audit rows are owned by the consultation they describe. Chain integrity comes from the SHA-256 hash chain, not from the FK, so deleting a consultation does not break the chain's verifiability.

**`ON DELETE SET NULL`** — when audit history has to *survive* the referenced row's deletion:

- `memory_versions.parent_version_id → memory_versions(id)` — admin-path deletion of a mid-history commit leaves the DAG with a gap rather than cascading through every descendant.
- `memory_branches.head_version_id → memory_versions(id)` — same reasoning; branches get re-pointed, not destroyed.
- `session_memory_injections.memory_id → memories(id)` — if a memory is deleted later, the *record that we once injected it into a session* stays. The audit outlives the artifact.
- `compression_quality_log.memory_id → memories(id)` — the quality manifest survives the thing it was a manifest for. Compliance cares that the transformation happened, not that the output still exists.
- `consultation_memory_refs.memory_id → memories(id)` — a consultation's cited memory may be deleted; the *record of the citation* is an audit artifact and must not vanish.
- `oauth_sessions.identity_id → oauth_identities(id)` — rotating an identity doesn't invalidate a session row that was already in flight.

The FK graph prevents accidental loss. The application and trigger layer add the same-memory checks the FK alone cannot express: branch HEAD JOINs are scoped by `memory_id`, recursive logs only walk same-memory parents, and the v3.5 trigger raises `MN001` instead of writing a cross-memory parent edge.

This is the part most projects that call themselves "memory" skip, because if the whole point is "store a blob, retrieve a blob", the relationships *between* blobs are out of scope. MNEMOS's design asserts the opposite: memories relate to consultations relate to audit entries relate to sessions relate to users, and the system has strong opinions about which of those relationships is load-bearing and which is historical.

The constraints are enforced at the database level. Application bugs cannot violate them. Migration bugs cannot silently create orphan rows. The constraint travels with the row.

### Compression — the MOIRAI contest

Compression has been operator-batched since v4.0. It is not an automatic session-column flag and it no longer uses the retired LETHE / ANAMNESIS / ALETHEIA engines. Operators enqueue work through the admin endpoints; the distillation worker runs a competitive contest over the active engines and persists the winner plus the losing candidates for audit.

- **ARTEMIS** — CPU-only extractive compression with identifier preservation, labeled-block handling, and evidence-based self-scoring.
- **APOLLO** — schema-aware dense encoding for LLM-to-LLM wire use. Rule-based schema detection with optional LLM fallback for fact-shaped content that misses a known schema.
- Quality manifest on every compression: what was removed, what was preserved, risk factors, safe/unsafe use cases.
- Original content always retained; compressed and original stored independently.
- Configurable quality thresholds per task type (security review: 95%, architecture: 90%, general: 80%).
- The plugin `CompressionEngine` ABC is open to operator-registered engines. The contest logs the winner plus every loser with its score and rejection reason.
- `/v1/memories/search` still carries reserved `compression_applied` / `compression_metadata` response fields from the v3.2 API shape; current search responses set `compression_applied=false`. Use `/v1/memories/rehydrate` or `/v1/memories/{id}/compression-manifests` when you need to know whether a compressed variant was used.

### Versioning and audit

- Memory version history (`memory_versions` table) — every mutation auto-snapshots previous state
- Diff and revert API: `GET /v1/memories/{id}/versions`, `GET /v1/memories/{id}/versions/{n}`, `GET /v1/memories/{id}/diff`, `POST /v1/memories/{id}/revert/{n}`. Non-root callers see only snapshots whose own `owner_id` / `namespace` / `permission_mode` pass `version_visibility_predicate` (`mnemos/core/visibility.py`).
- DAG (git-like) versioning: `GET /v1/memories/{id}/log`, `POST /v1/memories/{id}/branch`, `POST /v1/memories/{id}/merge`, `GET /v1/memories/{id}/commits/{commit}`. Logs do not bridge across invisible snapshots; a visible child whose immediate parent is hidden reports `parent_hash=null`.
- SHA-256 hash-chained audit log for consultations: `GET /v1/consultations/audit`, `GET /v1/consultations/audit/verify`

---

## Quick start

### Edge install (single binary)

```bash
curl -L https://github.com/ncz-os/mnemos/releases/download/v5.0.1/mnemos-linux-x86_64 -o mnemos
chmod +x mnemos
./mnemos install --profile edge
./mnemos serve --profile edge
```

### Package install

```bash
pip install 'mnemos-os[edge]==5.0.1'
mnemos install --profile dev
mnemos serve --profile dev
```

### Source install

```bash
git clone https://github.com/ncz-os/mnemos.git
cd mnemos
python -m pip install -e ".[dev,edge]"
mnemos install --profile dev
mnemos serve --profile dev
```

For the `server` profile, create or point at a Postgres database, set
`MNEMOS_PROFILE=server`, and provide `MNEMOS_DATABASE_URL` / Postgres settings
plus a Redis-backed `RATE_LIMIT_STORAGE_URI`. For Docker installs with an
existing `postgres_data` volume, `docker-compose.yml` and
`docker-compose.staging.yml` still run a one-shot `postgres-upgrade` service
because `/docker-entrypoint-initdb.d` files only execute on fresh database
initialization.

### Start

```bash
mnemos serve --profile server
curl http://localhost:5002/health
```

MNEMOS runs one worker by default. Increase `MNEMOS_WORKERS` only with Redis
backing shared rate-limit, circuit-breaker, and concurrency state; see
[`docs/SCALING.md`](./docs/SCALING.md).

### OpenAI-compatible gateway

Point OpenAI SDK clients at MNEMOS:

```bash
export OPENAI_BASE_URL=http://localhost:5002/v1
export OPENAI_API_KEY=<mnemos-api-key>
```

Memory context is injected by default on `POST /v1/chat/completions`. To bypass
the memory lookup for one request, send either header
`X-Mnemos-Inject-Memory: false` or body field
`"mnemos_inject_memory": false`.

---

## API reference

### Store a memory

```bash
# Basic
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{"content": "...", "category": "decisions", "subcategory": "architecture"}'

# With provenance
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "...",
    "category": "decisions",
    "namespace": "myagent/analyst",
    "source_model": "gemma4-consult",
    "source_agent": "background-enricher"
  }'

# Team/enterprise: include API key
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <your-api-key>' \
  -d '{"content": "...", "category": "decisions"}'
```

### Search

```bash
# Full-text search
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "topic keywords", "limit": 10}'

# Filtered by category
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "category": "solutions", "limit": 5}'

# Semantic (vector) search
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "semantic": true, "limit": 10}'
```

### Admin: create user and API key

```bash
# Create a user
curl -X POST http://localhost:5002/admin/users \
  -H 'Content-Type: application/json' \
  -d '{"id": "alice", "display_name": "Alice", "role": "user"}'

# Generate API key — raw_key shown once only
curl -X POST http://localhost:5002/admin/users/alice/apikeys \
  -H 'Content-Type: application/json' \
  -d '{"label": "cli-key"}'

# Revoke a key
curl -X DELETE http://localhost:5002/admin/apikeys/<key-id>
```

### GRAEAE reasoning

```bash
curl -X POST http://localhost:5002/v1/consultations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Your question", "task_type": "architecture_design"}'

# Extract best result by score
curl -X POST http://localhost:5002/v1/consultations \
  -d '{"prompt": "...", "task_type": "reasoning"}' | \
  jq '.all_responses | to_entries | sort_by(-.[1].final_score)[0]'
```

### Memory categories

| Category | Use for |
|----------|---------|
| `infrastructure` | Configs, endpoints, system state |
| `solutions` | Workarounds, resolved problems |
| `patterns` | Reusable approaches |
| `decisions` | Rationale and tradeoffs |
| `projects` | Per-project context |
| `standards` | Quality gates, conventions |

### GRAEAE task types

| Task type | Notes |
|-----------|-------|
| `architecture_design` | Full consensus |
| `reasoning` | Full consensus |
| `code_generation` | Speed-optimized provider subset |
| `web_search` | Real-time capable providers |

---

## Compression quality manifest

```json
{
  "compression_id": "uuid",
  "quality_rating": 92,
  "what_was_removed": ["2 introductory sentences", "3 supporting examples"],
  "what_was_preserved": ["Complete reasoning chain", "All main conclusions"],
  "risk_factors": ["Missing examples may reduce convincingness"],
  "safe_for": ["Initial consultation", "Quick decision making"],
  "not_safe_for": ["Security-critical decisions", "Detailed technical review"]
}
```

---

## FAQ

### Why is it called MNEMOS, and why are you using all these mythological names — who do you think you are, a fantasy novelist?

Fair question, and we get it more than you'd think. Short answer: the names aren't set dressing. Each one is a functional tag that happens to line up with a real Greek concept, because memory is one of the domains where Greek already had the vocabulary we needed.

- **MNEMOS** — short for Mnemosyne, the Titan goddess of memory and mother of the Muses. The system stores and retrieves memory. "MemoryService" felt like underselling it.
- **GRAEAE** — the three sisters in the Perseus myth who shared one eye and one tooth, passing them back and forth to see and speak. GRAEAE is the multi-LLM consensus layer: several providers sharing one prompt and converging on one consolidated answer. The metaphor was already sitting there.
- **THE MOIRAI** — the three Fates, who spin, measure, and cut the thread of life. The compression stack is collectively THE MOIRAI because each tier decides what part of a memory's thread survives. The current built-ins are **ARTEMIS** for CPU extractive compression and **APOLLO** for schema-aware dense encoding.

No, we are not fantasy novelists. The naming scheme is what happens when the domain you're working in is literally the thing a pre-Socratic culture wrote whole theogonies about, and you decide to use their vocabulary instead of inventing a worse one. Every name is aligned to what the component does, not chosen for atmosphere.

If you strongly prefer `MemoryService` / `LLMRouter` / `CompressorTier1`, the code does exactly the same thing regardless of the label. They're just tags. We like ours.

### Do I need GPU hardware?

No. CPU-only installs run fine. ARTEMIS runs on CPU, and the API server itself never needs a GPU. APOLLO's optional LLM fallback and local inference backends only kick in when `GPU_PROVIDER_HOST` is configured. For most deployments, CPU plus one external LLM provider is enough.

### Does it work with [OpenAI / Anthropic / Groq / Together / local Ollama]?

Yes. GRAEAE routes across any configured provider. Together AI and Groq are the default free-tier providers (no paid account required to get started). OpenAI, Anthropic, and Perplexity are supported as fallback providers. Local Ollama is first-class — MNEMOS can run fully offline with Ollama plus `nomic-embed-text` for embeddings.

### Is there a hosted version?

Not today. Self-hosted only.

### How is this different from Mem0 / Zep / MemPalace / LangChain memory?

See the *MemPalace and MNEMOS: different problems, not competitors* section above, plus the comparison table. Short version: those are in-process libraries or conversation-history stores designed for single-user / single-agent deployment. MNEMOS is a network service with multi-tenant isolation, a cryptographic audit chain, and a DAG-versioned memory model. Different form factor, different primary user.

### Does it phone home or collect telemetry?

No. There is no outbound telemetry of any kind. The only outbound traffic is the LLM provider calls you configure yourself, the webhook deliveries you register, and the federation syncs you set up. The code is all here; grep `httpx` if you want to confirm.

### Can I use it in production?

Yes — we have been since December 2025. v3.0 is the first public release line, not a greenfield experiment; the codebase has been operated continuously for roughly four months before being cut for open source. The honest caveat: it has been single-operator-tested, not yet battle-tested across many independent deployments. File issues against the live install and we'll track them.

### What's the migration story from [Mem0 / Zep / raw PostgreSQL]?

Currently manual — write a one-shot script that hits `POST /v1/memories/bulk` with your source data. Direct-import adapters for major competitors are on the roadmap but not yet shipped.

### Why port 5002 and not something normal like 8080?

Historical. Earlier versions split MNEMOS (5000) and GRAEAE (5001) across two services; v3 unified them on 5002 to signal "this is the combined single service". Override with `MNEMOS_PORT` if 5002 is taken.

### Does it run in Docker / Kubernetes?

Yes. `Dockerfile` and `docker-compose.yml` ship in the repo; `docker compose up -d` gets you a working MNEMOS + PostgreSQL instance for local evaluation. For Kubernetes, the Docker image is the starting point — no Helm chart yet, but the service is stateless on its own (Postgres is the only state), so a standard Deployment + Service + ConfigMap pattern works.

### How do I secure it in production?

- Set `MNEMOS_API_KEY` and require Bearer auth on all requests.
- Enable `RATE_LIMIT_ENABLED=true` (it's on by default).
- Set `MNEMOS_SESSION_SECRET` to a stable value so OAuth flows survive restarts.
- Set `OAUTH_TRUST_PROXY=true` + `RATE_LIMIT_TRUST_PROXY=true` only when you're behind a reverse proxy you control.
- Keep `WEBHOOK_ALLOW_PRIVATE_HOSTS=false` (the default). SSRF defense is on by default.
- Run behind a TLS-terminating reverse proxy. Don't expose the Uvicorn socket directly.
- Review `SECURITY.md` for the full checklist.

---

## License

MNEMOS is licensed under the Apache License, Version 2.0. See [`LICENSE`](./LICENSE) for the full text.

Contributions are accepted under the Developer Certificate of Origin (DCO) — see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

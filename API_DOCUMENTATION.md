# MNEMOS API Documentation

**Base URL**: `http://localhost:5002`
**Version**: v5.0.1 current (on top of v5.0.0 GA shipped 2026-05-02)
**Format**: JSON

---

> **Full OpenAPI reference**: once the server is running, the complete, live,
> always-accurate endpoint list is at `http://localhost:5002/docs` (Swagger UI)
> and `http://localhost:5002/redoc`. This document describes the core surface
> and common examples; `/docs` is the source of truth.

The unified `/v1/` namespace is the supported API surface.

---

## Table of Contents

1. [Authentication](#authentication)
2. [Health & Status](#health--status)
3. [Memory Operations](#memory-operations)
4. [Knowledge Graph](#knowledge-graph)
5. [Consultations (GRAEAE)](#consultations-graeae)
6. [Providers & Models](#providers--models)
7. [OpenAI-Compatible Gateway](#openai-compatible-gateway)
8. [Sessions](#sessions)
9. [MORPHEUS](#morpheus)
10. [Webhooks](#webhooks)
11. [OAuth / OIDC](#oauth--oidc)
12. [Federation](#federation)
13. [Portability (MPF)](#portability-mpf)
14. [Error Handling](#error-handling)
15. [Examples](#examples)

---

## Authentication

Two authentication surfaces coexist:

- **Bearer API key** — set `MNEMOS_API_KEY` on the server and send
  `Authorization: Bearer <key>` on every request. Suitable for service-to-service
  and CLI access.
- **Browser session** (cookie-based, see [OAuth / OIDC](#oauth--oidc)) — for
  users who sign in through Google / GitHub / Azure AD / generic OIDC.
  `get_current_user` checks Bearer first, then the `mnemos_session` cookie.

Personal installs may run without auth; team and production installs should
always set `MNEMOS_API_KEY` and/or enable OAuth.

---

## Health & Status

### GET /health

Liveness + readiness check (no auth required).

**Response** (200):
```json
{
  "status": "healthy",
  "timestamp": "2026-05-02T14:30:00.000Z",
  "database_connected": true,
  "version": "5.0.1",
  "profile": "edge",
  "distillation_worker": "idle"
}
```

**Example**:
```bash
curl -X GET http://localhost:5002/health
```

### GET /stats

System statistics — memory counts by category and task type, compression
stats, unreviewed compressions.

```bash
curl -H "Authorization: Bearer $MNEMOS_API_KEY" \
  http://localhost:5002/stats
```

---

## Memory Operations

All memory routes are under `/v1/memories`.

Read behavior is symmetric across REST, gateway context, and MCP:
non-root callers can read a memory when they are the owner, when the
row is federated, when the Unix world-read bit is set, or when the
Unix group-read bit is set for one of their groups. The shared helper
is `read_visibility_predicate` (`mnemos/core/visibility.py`). Writes
remain owner+namespace scoped.

### POST /v1/memories

Create a memory. This writes the memory row and emits a transactional
`memory.created` webhook event. Compression has been operator-batched since v4.0:
root users enqueue memories through `/admin/compression/enqueue` or
`/admin/compression/enqueue-all`; the distillation worker then drains
`memory_compression_queue`.

**Request Body**:
```json
{
  "content": "Memory content (required)",
  "category": "facts|identity|preferences|projects (required)",
  "task_type": "reasoning",
  "metadata": { "source": "import" }
}
```

### GET /v1/memories/{memory_id}

Retrieve a single memory. Non-root callers get `404` when the memory
does not pass the shared read predicate; the API does not reveal
cross-tenant existence.

### PATCH /v1/memories/{memory_id}

Update content, category, subcategory, metadata, or verbatim content.
The update is owner+namespace scoped. If the version trigger detects a
missing, NULL, or cross-memory branch HEAD, the API returns `409` with
manual reconciliation guidance (`handle_trigger_pgerror` in
`mnemos/core/visibility.py`).

### DELETE /v1/memories/{memory_id}

Delete a memory under the same owner+namespace write scope. The v3.5
trigger migration keeps DELETE snapshot handling live for deployments
that still attach `trg_memory_version_delete`; broken branch state maps
to `409`.

### POST /v1/memories/search

Semantic + keyword search.

```json
{ "query": "infrastructure", "limit": 5 }
```

Search hits update `recall_count` and `last_recalled_at` in the background.
`compression_applied` and `compression_metadata` are reserved response fields
on `MemoryListResponse`; current search responses set
`compression_applied=false`. Use `/v1/memories/rehydrate` to receive compressed
variants when present, or `/v1/memories/{id}/compression-manifests` to inspect
contest output.

### Compression manifests and rehydration

- `GET /v1/memories/{id}/compression-manifests` — current winning variant plus
  every historical contest candidate, score, manifest, and reject reason.
- `POST /v1/memories/rehydrate` — token-budgeted context assembly. It prefers a
  `memory_compressed_variants` winner when one exists and reports
  `compression_applied=true` only when a variant was actually used.

### DAG versioning (git-like)

- `GET /v1/memories/{id}/versions` — version summaries on a branch, filtered per snapshot by `version_visibility_predicate` (`mnemos/core/visibility.py`).
- `GET /v1/memories/{id}/versions/{n}` — one version on a branch, filtered by that snapshot's own owner/namespace/permission mode.
- `GET /v1/memories/{id}/diff` — diff between visible snapshots.
- `POST /v1/memories/{id}/revert/{n}` — revert to version n. Main-branch revert updates the live row under the trigger after a live-row/main-HEAD drift guard; feature-branch revert inserts a new DAG row and leaves `memories` tracking main.
- `GET /v1/memories/{id}/log` — commit history from branch HEAD. Recursive walks stay within one memory, and `parent_hash` is returned only when the actual immediate parent is visible.
- `GET /v1/memories/{id}/commits/{commit}` — one visible commit by hash.
- `GET /v1/memories/{id}/branches` — branch list. Non-root callers do not see branches whose head snapshot is invisible; corrupt heads are omitted and logged.
- `POST /v1/memories/{id}/branch` — create a branch from main HEAD or a specified commit. The handler locks the parent memory row with `FOR SHARE` and uses `ON CONFLICT DO NOTHING RETURNING`; duplicate branch names return `409`.
- `POST /v1/memories/{id}/merge` — merge source branch into target branch. Latest-wins is implemented; manual strategy returns not-implemented. Merge commits copy content/provenance from source and tenancy from target, and branch writers serialize on `_branch_advisory_lock_key` (`mnemos/api/routes/dag.py`).

See `ANTI_MEMORY_POISONING.md` for the rationale and drift-detection
workflow.

---

## Knowledge Graph

Structured triples live beside free-text memories and use the same
owner+namespace tenancy model.

- `POST /v1/kg/triples` — create a subject/predicate/object triple
- `GET /v1/kg/triples` — list triples with filters
- `GET /v1/kg/timeline/{subject}` — subject timeline
- `PATCH /v1/kg/triples/{triple_id}` — update a triple
- `DELETE /v1/kg/triples/{triple_id}` — delete a triple

KG tenancy columns were added in v3.1.x and namespace parity became the
standard across memory and entity surfaces in v3.2-v3.5.

---

## Consultations (GRAEAE)

`ConsultationRequest.mode` is a literal string. The seven accepted modes are:

| Mode | Type | Meaning |
|---|---|---|
| `auto` | routing strategy | Use the engine default for the task type. |
| `local` | routing strategy | Prefer local/self-hosted muses. |
| `external` | routing strategy | Prefer external provider muses. |
| `all` | routing strategy | Fan out to every available muse. |
| `single` | reasoning shape | Select one highest-weighted muse for a fast or low-cost answer. |
| `debate` | reasoning shape | Run a two-round multi-muse debate and synthesize the result. |
| `majority` | reasoning shape | Query up to three muses and report whether quorum was reached. |

Unknown modes are rejected by request validation with HTTP 422.

### POST /v1/consultations

Run a multi-LLM consensus consultation. Writes a tamper-evident audit row.

**Request Body**:
```json
{
  "prompt": "Design a microservices architecture.",
  "task_type": "architecture_design",
  "mode": "debate",
  "context": "optional context to prepend",
  "limit_chars": 12000,
  "format": "full",
  "models": ["openai/gpt-5.2-chat-latest"],
  "providers": null,
  "tier": null
}
```

Schema summary:

```python
mode: Literal["auto", "local", "external", "all", "single", "debate", "majority"] = "auto"
```

**Response** includes: `id`, `consensus_response`, `consensus_score`,
`winning_muse`, `cost`, `latency_ms`, `memory_refs` (citations).

### GET /v1/consultations/{consultation_id}

Retrieve a consultation by ID.

### GET /v1/consultations/{consultation_id}/artifacts

Retrieve citations and injected memory refs for a consultation
(EMIR Article 57 audit support).

### Audit chain

- `GET /v1/consultations/audit` — list audit entries
- `GET /v1/consultations/audit/verify` — verify SHA-256 hash chain integrity

Static `/audit` routes are mounted before dynamic `/{consultation_id}` so
path-param matching does not shadow them.

---

## Providers & Models

- `GET /v1/providers` — unified catalog (health-tracked)
- `GET /v1/providers/health` — per-provider availability + circuit-breaker state
- `GET /v1/providers/recommend?task_type=...&budget=...` — task-aware routing

On a fresh install with an empty `model_registry` table, `/recommend` falls
back to the static GRAEAE provider config so new deployments aren't 404.

---

## OpenAI-Compatible Gateway

Drop-in for OpenAI SDK consumers. Point `OPENAI_BASE_URL` at MNEMOS.

- `POST /v1/chat/completions` — chat completions with memory injection,
  propagated generation controls, and OpenAI-format SSE when `stream=true`.
- `GET /v1/models` — registry-backed model list only.
- `GET /v1/models/{model_id}` — registry lookup; unregistered IDs return 404.

Field support is intentionally pass-or-reject:

| Request field | OpenAI-style providers | Anthropic | Gemini | Other/text-only providers |
|---------------|------------------------|-----------|--------|---------------------------|
| `temperature`, `max_tokens`, `top_p` | Passed through (`max_tokens` maps to `max_completion_tokens` for GPT-5 models) | Mapped to Messages API fields | Mapped to `generationConfig` | Provider default unless adapter supports it |
| `stream` | Native SSE where available | Single-shot fallback wrapped as OpenAI SSE | Single-shot fallback wrapped as OpenAI SSE | Single-shot fallback wrapped as OpenAI SSE |
| `tools`, `tool_choice` | Passed for the OpenAI provider | Converted to Claude tool schema | 400 | 400 |
| `response_format` | Passed through | 400 | `json_object` maps to `responseMimeType=application/json` | 400 |
| `stop`, `n`, penalties | Passed through | `stop` maps to `stop_sequences`; unsupported penalties return 400 | Native `generationConfig` mapping | 400 when not honored |
| content blocks / images | OpenAI vision-capable models | Claude vision | Gemini vision | 400 |

Memory injection is enabled by default. Disable it for one request with either
header `X-Mnemos-Inject-Memory: false` or the non-OpenAI extension body field
`"mnemos_inject_memory": false`. Malformed header values are treated as
default-on. When the header is supplied, non-streaming JSON responses include
`mnemos_metadata.memory_injected`.

---

## Sessions

Stateful multi-turn chat with memory injection at turn boundaries.

- `POST /v1/sessions` — create
- `POST /v1/sessions/{id}/messages` — post a turn
- `GET /v1/sessions/{id}` — retrieve transcript
- `GET /v1/sessions/{id}/history` — full history
- `DELETE /v1/sessions/{id}` — close

---

## MORPHEUS

Dream-state generation is operator-triggered. v5.0.0 includes the REPLAY,
CLUSTER, SYNTHESISE, CONSOLIDATE, and EXTRACT phases. Generated memories are
tagged with `morpheus_run_id`; rollback deletes generated memories for that run.

- `GET /v1/morpheus/runs` — list runs
- `GET /v1/morpheus/runs/{run_id}` — run detail
- `GET /v1/morpheus/runs/{run_id}/clusters` — cluster membership and
  synthesized memory IDs
- `POST /admin/morpheus/runs` — trigger a synchronous run (root only)
- `DELETE /admin/morpheus/runs/{run_id}` — roll back a run (root only)

---

## Webhooks

Outbound event delivery with HMAC-SHA256 signatures and a durable retry log
(1m / 5m / 30m / 2h). Delivery log is replayed on server restart via the
recovery worker.

- `POST /v1/webhooks` — subscribe
- `GET /v1/webhooks` — list
- `DELETE /v1/webhooks/{id}` — revoke (soft-delete; delivery log retained)
- `GET /v1/webhooks/{id}/deliveries` — per-subscription delivery log

Events emitted: `memory.created`, `memory.updated`, `memory.deleted`,
`consultation.completed`.

---

## OAuth / OIDC

Browser-based sign-in for Google, GitHub, Azure AD, or any generic OIDC
provider (Keycloak, Authentik, Auth0, Okta).

- `GET /auth/oauth/{provider}/login` — redirect to provider
- `GET /auth/oauth/{provider}/callback` — provider → MNEMOS
- `POST /auth/oauth/logout` — invalidate session
- `GET /auth/oauth/me` — current-user profile
- Admin: `GET /admin/oauth/providers`, `GET /admin/oauth/identities`

Sessions are DB-backed (revocable, 30-day default TTL), with an hourly GC
worker. Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when
served over HTTPS.

OAuth state (PKCE verifier + CSRF nonce) lives in a separate short-lived
signed cookie (`mnemos_oauth_state`, 10-min TTL) distinct from the
application session cookie. Signing key via `MNEMOS_SESSION_SECRET`;
auto-generated on startup if unset.

---

## Federation

Pull-based one-way sync between MNEMOS instances. Federated memories are
stored with IDs `fed:{peer_name}:{remote_id}`, `owner_id='federation'`, and
are read-only by convention. Loop prevention via
`federation_source IS NOT NULL` exclusion.

- `GET/POST /v1/federation/peers` — admin peer CRUD
- `DELETE /v1/federation/peers/{id}` — remove peer
- `POST /v1/federation/peers/{id}/sync` — trigger sync
- `GET /v1/federation/peers/{id}/log` — per-peer sync log
- `GET /v1/federation/status` — aggregate status
- `GET /v1/federation/feed` — outbound feed (requires `role IN ('federation','root')`)

Background sync runs every 60 seconds.

---

## Portability (MPF)

MNEMOS Portability Format (MPF) is the native import/export envelope.

- `GET /v1/export` — export MPF v0.1.x envelope. `include_sidecars=true`
  includes KG triples, memory-version DAGs, and compression manifests where
  available.
- `POST /v1/import` — import an MPF envelope. Root plus
  `preserve_owner=true` is for authoritative restore/migration; non-root
  imports are scoped to the caller's owner+namespace.

CLI helpers: `mnemos export`, `mnemos import`, `mnemos validate-mpf`, and the
modules under `mnemos/tools/`.

---

## Error Handling

Standard HTTP status codes. Error responses are JSON:

```json
{ "detail": "Consultation not found" }
```

Common codes:

- `400` — request validation error
- `401` — missing or invalid auth
- `403` — role check failed
- `404` — entity not found
- `409` — conflict requiring operator action, including incompatible federation schema and v3.5 `MN001` branch-state reconciliation
- `413` — request body exceeds `MAX_BODY_BYTES` (default 5 MB)
- `429` — rate-limited (when `RATE_LIMIT_ENABLED=true`)
- `503` — database pool unavailable

---

## Examples

```bash
BASE_URL="http://localhost:5002"
AUTH="Authorization: Bearer $MNEMOS_API_KEY"

# Health
curl -s $BASE_URL/health | jq .

# Create a memory
curl -s -X POST $BASE_URL/v1/memories \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"content":"Example fact","category":"facts"}' | jq .

# Semantic search
curl -s -X POST $BASE_URL/v1/memories/search \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"query":"project completion","limit":5}' | jq .

# Run a consultation
curl -s -X POST $BASE_URL/v1/consultations \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"prompt":"Design a REST API for restaurant inspections","task_type":"architecture_design"}' | jq .

# Verify the consultation audit chain
curl -s -H "$AUTH" $BASE_URL/v1/consultations/audit/verify | jq .

# OpenAI-compatible chat completion
curl -s -X POST $BASE_URL/v1/chat/completions \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}' | jq .
```

For the full, always-accurate list of routes and schemas, use the live
OpenAPI at `http://localhost:5002/docs`.

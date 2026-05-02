# PANTHEON — Unified LLM Provider Facade

**Status:** Implemented in MNEMOS v5.0.0 for the `/pantheon/v1` slice; this
document also preserves forward-looking worker-pool design notes.
**Position in stack:** Above Triton (CERBERUS) + GRAEAE (PYTHIA); below every OpenAI-compatible client.
**Greek-name fit:** *Temple of all gods.* One facade, many providers behind it. Pairs with CHARON (the ferryman who carries memories across systems): same interop posture, different surface.

## v0.2 Implementation Note

PANTHEON v0.2 closes the v0.1 deferred items while keeping the surface opt-in
under `/pantheon/v1`. The shipped slice now includes per-(user, session)
hard caps for `usage_tier=consultation_only` models, best-effort MNEMOS
`pantheon_routing` memory writes for successful and failed gateway calls,
rolling-window adaptive selection for `auto:*` aliases, and expanded
`/pantheon/v1/route/explain` output with candidates, scores, selected backend,
and the selection reason.

The consultation cap bucket is intentionally process-local in v0.2. It is
correct for a single MNEMOS process and test/dev deployments; horizontally
scaled deployments need a Redis-backed bucket so every replica shares the same
per-session count.

Deferred to v0.3: Redis-backed cap buckets, full tool-use streaming passthrough
across provider adapters, real-time provider health via NATS, and KRONOS
forecasting integration for proactive routing.

---

## Mission

> One place to store provider keys. One URL for clients. One catalog spanning every model the operator has access to.

Today: configuring an agent stack means juggling 5–10 provider keys (OpenAI, Together, Groq, Gemini, Perplexity, vLLM, local Ollama, …) across every tool that wants LLM access. Each tool reimplements rate-limit handling, fallback, model-name normalization. Each tool maintains its own key vault.

PANTHEON collapses that to:

- **One PANTHEON URL** as the OpenAI-compat endpoint for every client (Cursor, Continue, langchain, openai-python, custom-provider configs).
- **One PANTHEON token** per tool — a personal token that PANTHEON uses to identify and rate-limit the caller, while it holds the real provider keys server-side.
- **One `/v1/models` catalog** that's the union of every backend, with extended metadata (cost tier, capabilities, context length, current health) so clients pick intelligently.

Every existing tool keeps working — PANTHEON is OpenAI-shape. The win is the centralization.

---

## CHARON v0.2 contract note (related work)

The CHARON v0.2 portability subsystem (shipped in the v3.4 line and available
to later PANTHEON work) restricts the trigger-suppressed `memory_versions` sidecar
import path to the **root + preserve_owner=true** admin/migration
path. Non-root callers can ship `kg_triples` and
`compression_manifest` sidecars without restriction, but
`memory_versions` requires a root bearer token (`--preserve-metadata`
on `mnemos/tools/memory_import.py`). This restriction is structural:
the interaction of caller-scoped deterministic id derivation,
ON CONFLICT idempotency, and `memory_versions` surviving memory
deletion makes the non-root path a defect-prone surface where
adversarial review surfaced a sequence of stale-state edge cases
that each required extending the equality check on every column.
The architectural restriction collapses the entire class.

**Practical impact for PANTHEON clients:** none. PANTHEON callers
hitting `/v1/import` for non-DAG-history use cases (typical agent
memory sync) still work as before. Cross-system migrations go
through the documented root path. If a peer-system adapter wants
to preserve authoritative version history across systems, it
needs a root token — the same constraint as any administrative
data movement.

## What we are NOT building

- **A new message queue.** PANTHEON uses **NATS JetStream** (Apache 2.0, single binary, ~30MB RAM, native Python client). Building bespoke MQ infrastructure is not the project. Same posture as MNEMOS choosing pgvector over a custom vector store: pick the boring proven option, focus engineering on the layer that's actually novel.
- **A new provider catalog.** PANTHEON auto-populates from GRAEAE's existing provider/muses database. GRAEAE already knows which providers have keys configured, which models each one offers, and recent health stats. PANTHEON's catalog is a *view* over GRAEAE's provider table plus per-worker advertisements. See "Catalog auto-population" below.
- **A new auth system.** Tokens map to existing MNEMOS owner_id + namespace identity. The same auth that gates `/v1/memories` gates `/v1/chat/completions` here.

## OpenAI-Compatible Memory Injection Control

The OpenAI-compatible gateway injects MNEMOS memory context by default on
`POST /v1/chat/completions`. Callers can bypass retrieval for a single request
without changing gateway configuration:

- Header: `X-Mnemos-Inject-Memory: false`
- Body extension: `"mnemos_inject_memory": false`

Malformed header values are treated as default-on. When the header is supplied,
non-streaming JSON responses include `mnemos_metadata.memory_injected` so callers
can verify whether the gateway searched and injected memory for that request.

## Position in the fleet

```
                     ┌───────────────────────────────────────────┐
                     │ Clients (Cursor, Continue, langchain,     │
                     │ openai-python, MCP-aware agents…)         │
                     └───────────────┬───────────────────────────┘
                                     │ /v1/chat/completions
                                     │ /v1/models, /v1/embeddings
                                     ▼
                     ┌───────────────────────────────────────────┐
                     │ PANTHEON frontend (FastAPI, MCP server)   │
                     │ - /v1/* OpenAI-compat surface             │
                     │ - extended /v1/models catalog             │
                     │ - usage_tier policy enforcement           │
                     │ - per-tenant token + cost cap             │
                     │ - streaming bypass                        │
                     └───────────────┬───────────────────────────┘
                                     │ NATS JetStream subjects:
                                     │ work.{vllm.cerberus, groq,
                                     │       together, openai,
                                     │       gemini, perplexity}
                                     ▼
                     ┌───────────────────────────────────────────┐
                     │ Worker pool                               │
                     │ - per-provider rate-limit token bucket    │
                     │ - retry/failover via NACK + redeliver     │
                     │ - local key vault unsealed at start       │
                     └───────────────┬───────────────────────────┘
                                     │
       ┌─────────────────────────────┼───────────────────────────┐
       ▼                             ▼                           ▼
   Triton/vLLM                 Cloud APIs                Cloud APIs
   (CERBERUS)                  (Together, Groq,          (OpenAI,
   local GPU                    Perplexity)               Gemini)
```

GRAEAE remains a peer service — but PANTHEON's catalog can advertise GRAEAE-backed virtual models like `consensus:reasoning` that route through GRAEAE under the hood. Clients don't need to know the difference.

---

## Catalog: extended `/v1/models`

Stock OpenAI returns `{id, object, created, owned_by}`. PANTHEON returns the same plus structured metadata; clients that ignore the new fields still see a normal model list.

```json
{
  "object": "list",
  "data": [
    {
      "id": "mistral-7b-instruct",
      "object": "model",
      "created": 1714000000,
      "owned_by": "pantheon:vllm.cerberus",
      "pantheon": {
        "backend": "vllm.cerberus",
        "cost_tier": "free",
        "usage_tier": "agentic_ok",
        "context_window": 32768,
        "capabilities": ["chat"],
        "latency_p50_ms": 850,
        "health": "ok",
        "rate_limit_rpm": null
      }
    },
    {
      "id": "claude-opus-4-7",
      "object": "model",
      "created": 1714000000,
      "owned_by": "pantheon:anthropic",
      "pantheon": {
        "backend": "anthropic",
        "cost_tier": "premium",
        "usage_tier": "consultation_only",
        "context_window": 200000,
        "capabilities": ["chat", "tool_use", "vision"],
        "latency_p50_ms": 4200,
        "health": "ok",
        "rate_limit_rpm": 50,
        "advisory": "Anthropic ToS forbids agentic-loop usage; PANTHEON enforces a per-(user,session) hard cap. For agent workflows use a model with usage_tier=agentic_ok."
      }
    }
  ]
}
```

### Catalog auto-population from GRAEAE

GRAEAE already maintains a provider database (the muses registry) — which providers have keys configured, what models each one offers, recent health from consultation runs. PANTHEON does NOT duplicate this. The catalog is computed at startup and refreshed on heartbeat:

```
                  ┌─────────────────────────────┐
                  │ GRAEAE muses_api_keys.json  │
                  │ + provider/muse registry    │
                  │ (existing on PYTHIA)        │
                  └─────────────┬───────────────┘
                                │ read on PANTHEON startup
                                │ + on `catalog reload` event
                                ▼
                  ┌─────────────────────────────┐
                  │ PANTHEON catalog cache      │
                  │ - provider list             │
                  │ - models per provider       │
                  │ - usage_tier per model      │
                  └─────────────┬───────────────┘
                                │ + per-worker `catalog.advertise`
                                │   for live model availability
                                ▼
                          /v1/models response
```

Adding a new provider becomes a one-step operation: drop the key into GRAEAE's existing key store + register the muse. PANTHEON picks it up next reload (or on a SIGHUP). No PANTHEON-side config changes.

The `usage_tier` annotation per model is configured once in the GRAEAE registry (e.g. Anthropic models tagged `consultation_only`). PANTHEON reads that tag verbatim — it's not a separate file to keep in sync.

### Required fields per entry

| Field | Type | Purpose |
|---|---|---|
| `backend` | string | which worker subject serves this. e.g. `vllm.cerberus`, `together`, `groq` |
| `cost_tier` | enum: `free`, `paid`, `premium` | for `prefer:free` style routing |
| `usage_tier` | enum: `agentic_ok`, `consultation_only`, `embedding_only` | enforcement boundary |
| `context_window` | int | max prompt+completion tokens |
| `capabilities` | list | `chat`, `tool_use`, `vision`, `json_mode`, `embedding`, `reasoning` |
| `latency_p50_ms` | int | observed median; updated from rolling window |
| `health` | enum: `ok`, `degraded`, `down` | from worker heartbeats |
| `rate_limit_rpm` | int or null | provider's stated limit (null = local/unbounded) |
| `advisory` | string (optional) | human-readable warning surfaced to clients |

The catalog is computed (not configured): each worker advertises its models on startup, the frontend aggregates, refreshes on worker heartbeat events.

---

## Routing

### Capability-based model names

Beyond literal names like `mistral-7b-instruct`, PANTHEON resolves alias prefixes:

```
auto:reasoning            → highest-quality reasoning model the caller can afford
auto:cheap-fast           → free-tier first, paid fallback only
free:embedding            → free embedding model (Nomic / MiniLM via vLLM)
tool:json                 → model with capabilities=["tool_use", "json_mode"]
consensus:reasoning       → routes through GRAEAE for multi-LLM consensus
```

The alias is resolved server-side at request time, using:

1. The caller's tenant policy (cost cap, allowed_tiers).
2. Current worker health (skip degraded/down).
3. MNEMOS-stored history for this caller (which backend has been winning recently).

### Hint headers (alternative to alias)

```
X-Pantheon-Cost-Tier: free
X-Pantheon-Latency: low
X-Pantheon-Capability: tool_use
X-Pantheon-Mode: agentic       # locks out usage_tier=consultation_only
```

Stock OpenAI clients ignore these headers (no harm). Smart clients use them to express intent without changing model name.

### Deterministic policy (no LLM in the loop)

The routing decision is a pure function of:
- catalog state (worker health + advertised metadata)
- caller policy (tenant config + recent usage)
- request hints (model name + headers)
- MNEMOS rolling stats (last N minutes per provider × outcome)

**No Claude / GPT call is ever made to decide routing.** That would (a) waste credits and (b) reintroduce the agentic-loop pattern Anthropic flags. Policy improvements come from operator-tuned weights or A/B-tested adjustments, not from another LLM choosing.

---

## `usage_tier` and the Anthropic boundary

**Default fleet posture (from CLAUDE.md):** Anthropic is forbidden as an LLM provider for agent frameworks. Single-shot consultation is fine.

PANTHEON encodes this directly:

| Tier | Meaning | Allowed clients | Anthropic example |
|---|---|---|---|
| `agentic_ok` | Free-form use, fine for repeated calls in a session | All | local vLLM, Together, Groq |
| `consultation_only` | One-shot reasoning; per-(user,session) hard cap | Hint-aware clients only; agentic clients filtered out by default | Claude Opus / Sonnet via Anthropic Max sub |
| `embedding_only` | Embedding endpoints (no chat) | All embedding clients | OpenAI embed, Nomic via vLLM |

### Enforcement mechanism

1. Client sends request with `model: "claude-opus-4-7"` or `auto:reasoning` resolving to it.
2. Frontend checks `usage_tier`. If `consultation_only`:
   - Look up the caller's recent dispatch history (MNEMOS).
   - If they've made >N calls to this tier in the last hour: 429 with `{"error": "consultation_tier_cap", "suggested_alternative": "...", "rationale": "..."}`.
   - If their request looks agentic (header `X-Pantheon-Mode: agentic`, OR detected via session-id repetition rate): 403.
3. Otherwise dispatch normally.

### Why this matters for the fleet

The user runs Anthropic Max as a personal sub. They're not abusing it; consultation use IS in-scope. PANTHEON makes the boundary explicit so:
- Personal consultation calls still flow.
- Agentic-loop misuse (which would draw Anthropic flags) is structurally prevented.
- The doc + UX surface tells external users where the line is, so PANTHEON-as-OSS doesn't accidentally encourage ToS violations.

---

## Streaming

**Streaming requests bypass the queue** and proxy directly from frontend to backend. Reason: NATS-stream-token-by-token works but adds 5–20ms hop latency, and clients streaming chat completions are extremely sensitive to that delay.

**Non-streaming, batch, embeddings, and async-completion requests flow through the queue** and benefit from worker-pool load balancing, retry, and rate-limit smoothing.

The frontend decides at request time:

```
streaming = body.get("stream") == True

if streaming:
    pre_select_backend()  # one-shot pick from healthy workers
    proxy_directly_with_sse()
else:
    publish_to_subject(work.<backend>)
    return await await_response(request_id)
```

The split is invisible to clients — same `/v1/chat/completions` URL.

---

## Worker contract

A PANTHEON worker is a tiny daemon that:

1. **Connects to NATS** and consumes one subject (e.g. `work.groq`).
2. **Loads its provider key** from the local key vault on startup.
3. **Maintains a token bucket** sized to the provider's rate limit.
4. **Advertises its models** on startup (publishes to `catalog.advertise`).
5. **Heartbeats** to `catalog.heartbeat` every 30s with current health.

Workers are stateless and horizontally scalable: run two `groq-worker`s and they share the queue.

### Reference shape (Python)

```python
class PantheonWorker:
    SUBJECT = "work.groq"
    PROVIDER = "groq"

    async def run(self):
        await self.nats.subscribe(
            self.SUBJECT,
            cb=self.handle,
            queue="groq-workers",  # competing consumers
        )
        await self.advertise_models()
        await self.start_heartbeat()

    async def handle(self, msg):
        req = json.loads(msg.data)
        async with self.token_bucket.acquire():
            try:
                resp = await self.provider_call(req)
                await self.reply(msg, resp)
            except RateLimited:
                await msg.nak(delay=req.get("retry_delay", 5))
            except ProviderDown:
                await msg.nak(delay=30)
                await self.report_health("degraded")
```

---

## Auth model

### Per-tenant tokens

PANTHEON issues tokens per tenant (or per-tool-per-tenant for finer audit). The token carries:

- `tenant_id` — for cost ceilings, audit attribution
- `allowed_tiers` — subset of `{agentic_ok, consultation_only, embedding_only}`
- `cost_ceiling_usd_per_day` — optional
- `model_allowlist` / `model_denylist` — optional

The frontend validates the token, attaches the policy to the request envelope, then dispatches.

### Provider keys

Stored centrally in PANTHEON's vault. Default: encrypted at rest under a master key. Production: HashiCorp Vault / AWS KMS / sealed secrets. Workers unseal on startup.

```
~/.pantheon/keys/
├── openai.enc         ← OpenAI API key
├── together.enc       ← Together API key
├── groq.enc           ← Groq API key
├── gemini.enc         ← Google AI key
├── perplexity.enc     ← Perplexity key
├── anthropic.enc      ← (consultation-only flag enforced)
└── master.key         ← root key (mode 600, owner only)
```

A new tool integrating with PANTHEON gets ONE token. Adding a new backend = dropping a key file in the vault + starting a new worker. No client config changes anywhere.

---

## Client-side: agent model discovery is the hard problem

The single-API-config-on-the-client pitch only delivers if the agent ALSO knows how to use that single config to discover and pick from many models. Most existing agents don't:

| Agent | Model discovery today | What needs to change |
|---|---|---|
| **OpenClaw** | Hardcoded model list at compile time. `models.providers.<name>.model = "..."` in config. | Add `discover-from-endpoint` mode: on connect, GET `/v1/models`, populate the available-model list. Honor PANTHEON catalog metadata (cost_tier, usage_tier, capabilities). Allow runtime model switching in the active session. |
| **Hermes** | Similar — provider config has a fixed model name per provider. | Same patch: discover-from-endpoint, runtime switching, capability-aware aliases (`auto:reasoning`). |
| **zterm** | Talks to whatever its single configured backend exposes. | Already simple; can use PANTHEON's `auto:` aliases without code changes. |
| **Cursor / Continue / langchain** | Custom-provider config takes a base URL and a model name. They DO call `/v1/models` for autocomplete but don't use the result for live switching. | Lighter touch: PANTHEON works today with a static model name; capability aliases need an extension on their side. |

### The contribution work

PANTHEON ships with PR-ready patches for the agents we own/influence:

1. **OpenClaw model-discovery PR** — adds a `discovery: auto` config option. When set, the agent calls `/v1/models` at startup, builds its model list dynamically, and exposes a `/model <name>` slash command for runtime switching. Catalog metadata (`pantheon.cost_tier`, `pantheon.usage_tier`) drives the agent's filter (e.g. agentic-mode auto-skips `consultation_only`).

2. **Hermes model-discovery PR** — same shape, different file.

3. **MCP-aware agents (Claude Code, etc.)** — the MCP front-door is the easier path here. Agent calls `pantheon_list_models(filter_capabilities)` via MCP, gets a typed response with metadata baked in.

4. **OpenAI-shape ecosystem (langchain, openai-python)** — these aren't ours to patch. PANTHEON's solution is the alias convention: clients pass `model="auto:reasoning"` and the resolution happens server-side. No client changes required.

The PRs land before PANTHEON's v4 cut, ideally upstream-merged. If upstream is slow, ship the patch as a doc + sidecar branch that operators can apply manually.

### Why client-side changes matter

Without them, PANTHEON degrades to "single endpoint with one default model." That's still useful (key-vault consolidation, audit, cost cap) but it's not the unlock. The unlock is the agent treating PANTHEON as a fleet of capabilities and switching across them based on the task at hand. That requires the agent to KNOW it can switch — which means it has to discover.

## MCP front-door

Alongside the HTTP/v1 surface, PANTHEON exposes an MCP server. Tools:

```
pantheon_chat(messages, model_or_alias, capability_hint?, max_cost?)
pantheon_embed(texts, model_or_alias?)
pantheon_list_models(filter_capabilities?, filter_tier?)
pantheon_route_explain(messages, model_or_alias)  # diagnostic
```

MCP-aware agents (Claude Code with custom MCP, Cursor, Continue) discover capabilities through the standard MCP advertising mechanism. The HTTP path remains for everything else.

---

## MNEMOS feedback loop

Every routing decision + outcome lands in MNEMOS as a structured memory:

```python
create_memory({
    "category": "pantheon_routing",
    "content": json.dumps({
        "request_id": "...",
        "tenant_user_id": "alice",
        "alias_or_model": "auto:reasoning",
        "resolved_to": "llama-4-405b",
        "outcome": "success",
        "latency_ms": 2400,
        "tokens_in": 1200,
        "tokens_out": 380,
        "cost_usd": 0.012,
        "error_class": null,
    }),
    "namespace": "pantheon",
    "owner_id": "system:pantheon",
    "metadata": {
        "pantheon_version": "0.2",
        "session_id": "session-123",
        "usage_tier": "agentic_ok",
        "resolved_to": "llama-4-405b",
        "outcome": "success",
        "latency_ms": 2400,
        "cost_usd": 0.012,
    },
})
```

The routing policy queries this rolling window at decision time:

```sql
SELECT metadata->>'resolved_to' AS backend,
       AVG((metadata->>'latency_ms')::FLOAT) AS avg_latency_ms,
       SUM(CASE WHEN metadata->>'outcome' = 'error' THEN 1 ELSE 0 END)::FLOAT
         / COUNT(*)::FLOAT AS error_rate,
       AVG((metadata->>'cost_usd')::FLOAT) AS avg_cost
FROM memories
WHERE category = 'pantheon_routing'
  AND created > NOW() - INTERVAL '15 min'
  AND metadata->>'resolved_to' = ANY($candidate_list)
GROUP BY backend
```

Result: **the routing improves with use, automatically.** Backends that have been winning get more traffic; ones that are degraded shed load before the catalog's `health` field flips. This is the key differentiator from LiteLLM / Portkey, which use static config.

---

## Migration story

For the user's current fleet:

1. **Day 0:** PANTHEON deployed on PYTHIA next to GRAEAE. Empty catalog, no workers.
2. **Day 1:** First worker = vLLM-CERBERUS. PANTHEON advertises Mistral-7B as `pantheon:vllm.cerberus`. One client (zterm? Cursor?) points at PANTHEON; rest of fleet still uses old configs.
3. **Day 2–5:** Together / Groq / Gemini workers come online. Catalog grows.
4. **Day 6:** Anthropic worker added with `usage_tier: consultation_only`. GRAEAE consultation flow now goes through PANTHEON instead of GRAEAE's direct provider calls (or alongside).
5. **Day 7+:** Other clients migrate (`~/.zeroclaw/config.toml`, `~/.openclaw/config.toml`, etc. — replace per-provider sections with single PANTHEON URL).

Old configs keep working throughout — PANTHEON adds a path, doesn't remove one.

---

## Out of scope for v4 launch

The following are interesting follow-ons but explicitly NOT in the v4 cut:

- **Cross-fleet PANTHEON** (a peer instance you can route to as a backend). Future story for federation, beyond v4.
- **Streaming-via-MQ** (NATS-token-by-token). Bypass-direct is sufficient.
- **Caching layer** (content-hash lookups). Real wins for reasoning workloads but needs careful invalidation rules; defer to v4.1.
- **Cost-cap enforcement at the request level** (mid-stream kill switch). Token-bucket level is enough for v4.

---

## Open design questions

1. **MQ choice: NATS JetStream vs Redis Streams vs Postgres LISTEN/NOTIFY.** Recommendation: NATS JetStream (~30MB binary, native Python client, request/reply semantics built-in, persistent subjects for replay). Redis adds a dependency we can avoid; Postgres LISTEN/NOTIFY isn't durable for crash recovery.
2. **Streaming bypass: pre-select vs pre-flight.** Pre-select (pick worker before forwarding) is simpler; pre-flight (call catalog first, then select) is cleaner but adds 10–20ms. Recommend pre-select for v4 launch.
3. **Catalog refresh interval.** Worker heartbeat every 30s, frontend cache TTL 60s. Health flips visible within 60–90s.
4. **Tenant model: single (operator-only) or multi (per-user tokens with caps)?** Recommend multi from day one — even for personal use, having a separate token per tool gives audit + revocation.

---

## Naming + repo layout

- **Project name:** PANTHEON
- **Repo:** `github.com/perlowja/pantheon` (new repo, not under MNEMOS)
- **Top-level dirs:**
  ```
  pantheon/
  ├── frontend/        FastAPI + MCP surface
  ├── workers/         per-provider workers (one subdir each)
  ├── catalog/         model advertising + heartbeat aggregation
  ├── auth/            token verification, tenant policy
  ├── docs/            this doc + per-worker config recipes
  └── pyproject.toml
  ```
- **Ships with MNEMOS v4.** Co-released. MNEMOS gets a `pantheon_routing` category convention; PANTHEON depends on MNEMOS for the feedback loop. They're loosely coupled — MNEMOS has no PANTHEON imports.

---

## Concrete next steps

1. **This doc → review.** Operator + outside-eyes pass. Codex adversarial review at design-doc level (catch policy / interop holes before code).
2. **Skeleton repo.** New `perlowja/pantheon` with frontend stub + one vLLM worker. End-to-end happy path: client → /v1/chat/completions → vLLM.
3. **Catalog + heartbeat.** Workers advertise, frontend aggregates.
4. **Second worker (Groq).** Validates the multi-backend story. Alias resolution + cost-tier policy.
5. **Anthropic worker with usage_tier enforcement.** Validates the consultation-only boundary.
6. **MNEMOS routing-log integration.** Adaptive policy goes live.
7. **MCP front-door.** Optional surface, lights up agentic discovery.
8. **MNEMOS v4 release** with PANTHEON co-launch.

Estimated calendar: design + repo skeleton in week 1; multi-backend + adaptive policy in weeks 2–3; v4 cut after CHARON cross-system rig + MemPalace announcement push (parallelizable).

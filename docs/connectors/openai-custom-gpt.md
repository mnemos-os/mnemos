# Connector: OpenAI Custom GPTs (Actions)

> **Status: experimental.** This guide covers the OpenAI Custom-GPT
> **Actions** path — a separate surface from the
> [ChatGPT Pro Developer Mode (MCP)](./chatgpt-pro-developer-mode.md)
> connector. Pick this surface when the user is on a free / Plus
> ChatGPT tier (which doesn't have MCP) but wants to expose a
> single MNEMOS endpoint as a Custom GPT tool.

## What this gets you

Any ChatGPT Plus / Pro / Team user can build a Custom GPT in
ChatGPT's GPT Builder, point an Action at your MNEMOS HTTP API,
and call its REST endpoints from inside a conversation:

- `search_memories`, `get_memory`, `list_memories`, `create_memory`
- `kg_search`, `kg_create_triple`
- `recommend_model`
- DAG read endpoints (`/v1/memories/{id}/log`,
  `/v1/memories/{id}/commits/{hash}`)

The transport is plain HTTPS request/response with a Bearer token
in the `Authorization` header. **No SSE, no MCP** — Actions are
synchronous JSON-over-HTTP.

## Prerequisites

- ChatGPT Plus / Team / Pro / Enterprise / Edu tier — any tier
  that allows custom GPTs with Actions.
- A MNEMOS instance reachable from the public internet over HTTPS
  (Cloudflare Tunnel, ngrok, an existing reverse proxy with a
  public hostname, etc.). Same constraint as the Developer Mode
  guide.
- A bearer token (e.g. `$MNEMOS_API_KEY`) that ChatGPT will send
  on every Action call.

## Step 1: Build the OpenAPI artifact

Custom GPT Actions take an **OpenAPI 3.x JSON spec**. MNEMOS
ships a CLI command that produces one in the OpenAI-Actions-
compatible form:

```bash
mnemos dump-openapi --target gpt-actions --output mnemos-openapi.json
```

What `--target gpt-actions` does (vs. `--target full`):

- Endpoint `summary` and `description` fields truncate at 300
  characters — OpenAI's documented Custom GPT Actions limit.
- Parameter `description` and `requestBody.description` fields
  truncate at 700 characters — also per the docs.
- Truncated values end with a single `…` so it's visible at a
  glance which fields were capped.

If you skip the flag (or use `--target full`), Custom GPT import
will reject the spec with a "field too long" error or silently
truncate, neither of which is a great experience.

## Step 2: Set the public base URL

The OpenAPI spec's `servers[0].url` is what ChatGPT calls. FastAPI
does NOT auto-populate `servers[]` from any env var, so the
default-rendered spec has no `servers[0].url` at all (consumers
fall back to `/` which is useless from ChatGPT's network).
Inject it explicitly:

```bash
mnemos dump-openapi --target gpt-actions \
  --server-url https://mnemos.example.com \
  --output mnemos-openapi.json
```

The Custom GPT Actions importer is strict about `servers[0].url`
matching the actual host that responds to the requests, so set
it to the FQDN your tunnel/proxy/cert resolves at.

If you forget the flag, the artifact will still parse — but
ChatGPT will reject it at import time with a "missing server URL"
error or attempt to call against the relative path `/`. Adding
`--server-url` is mandatory for OpenAI Actions consumption.

## Step 3: Upload to the GPT Builder

In ChatGPT:

1. **Explore GPTs → My GPTs → Create**.
2. Configure tab → switch to **Configure** view.
3. **Actions → Create new action**.
4. Paste the contents of `mnemos-openapi.json` into the schema box.
5. Authentication → API Key → Bearer → paste your token.
6. Privacy Policy → enter a URL (anything; ChatGPT requires the
   field but doesn't validate it for personal GPTs).
7. Save.

The Builder will list every endpoint the spec advertises.
Test in the preview pane:

> Search my MNEMOS memories for "pgvector benchmarks"

ChatGPT will call `POST /v1/memories/search` with your query,
forward the results back into the conversation, and the model
will summarise them.

## Step 4: Iterate

When the MNEMOS API surface evolves, regenerate the artifact:

```bash
mnemos dump-openapi --target gpt-actions -o mnemos-openapi.json
```

…and re-paste the schema into the GPT's Actions panel. The GPT
Builder doesn't currently support file-watch / auto-refresh, so
this is a manual step on each MNEMOS upgrade that adds new
endpoints.

## Operational notes

- **Read scope is the bearer token's scope.** Anything the token
  can read via the REST API, ChatGPT can read via the Action.
  Mint a dedicated low-privilege token for the Custom GPT
  rather than re-using your interactive operator token.
- **Action calls go through your normal auth path.** The same
  `api_keys` / `users` lookup that gates `curl`'d REST calls
  applies — RLS, namespace isolation, role checks all work
  exactly as they do for any other HTTP client.
- **No streaming.** Actions are synchronous; long-running
  operations like bulk import or `/v1/morpheus/runs` will hit
  ChatGPT's per-call timeout. For those, use the Developer Mode
  (MCP) connector instead, where the streaming surface is
  designed for it.
- **Rate limiting.** The slowapi limiter in MNEMOS still applies.
  A heavy GPT-driven workload may need `RATE_LIMIT_ENABLED=false`
  or a higher per-route limit; the same operator knobs as for
  any other HTTP client.

## Known caveats

- **Long endpoint descriptions get truncated, even with
  `--target gpt-actions`.** The 300/700-char limits are real.
  Some MNEMOS routes have multi-paragraph docstrings explaining
  the visibility predicate, the per-row tenancy gate, etc. —
  those summaries will arrive at the GPT shortened. If a GPT's
  reasoning depends on a specific endpoint detail, embed that
  detail in the GPT's *Instructions* field instead of relying
  on the OpenAPI description to carry it.
- **No `Vary: Accept` content negotiation in the Actions form.**
  Custom GPT Actions don't natively send `Accept: text/plain`
  to get prose narration. Use the JSON `MemoryItem` shape and
  let the model summarise.
- **No MCP-style discovery.** A GPT Action call is a fixed REST
  endpoint, not a generic tool registry like MCP. Adding a new
  MNEMOS endpoint requires re-uploading the spec to the Builder.

## Troubleshooting

- **Builder rejects "schema invalid":** Check the JSON validates
  (`jq . mnemos-openapi.json` should succeed). Inspect the error
  detail — usually a missing required field or a malformed
  `$ref`. The `--target gpt-actions` form should pass cleanly;
  if it doesn't, file an issue with the offending endpoint name.
- **All actions return 401:** the bearer token is wrong or has
  been revoked. Verify with
  `curl -H "Authorization: Bearer <token>" https://mnemos.example.com/v1/health`.
- **Actions return 5xx:** look at MNEMOS logs (`docker logs
  mnemos-server` or wherever your stdout lands). The 5xx will
  have a request-id; cross-reference it in the log.
- **Builder shows the schema but the GPT doesn't use it:** ChatGPT
  sometimes silently falls back to "answer without tool use" if
  the description / parameter shape is ambiguous. In the GPT's
  Instructions, add a one-liner: "When the user asks anything
  involving past memory or knowledge from prior sessions, you
  MUST call the MNEMOS Action."

## Cross-references

- [ChatGPT Pro Developer Mode (MCP)](./chatgpt-pro-developer-mode.md)
  — for the MCP / SSE path on the Pro / Team / Enterprise tiers.
- [Connector gallery README](./README.md) — landing page for all
  surfaces.
- `mnemos dump-openapi --help` — full CLI surface.

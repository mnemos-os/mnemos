# ChatGPT Pro Developer Mode → MNEMOS

> **Status: experimental.** See [README.md](./README.md) for the
> stability framing. This recipe targets developers and power users
> running their own MNEMOS instance who want ChatGPT Pro to read and
> write the same memory their other agents use.

## What this gets you

ChatGPT remembers across conversations by querying *your* MNEMOS
instance for relevant memories at prompt-time and writing new ones
when you ask it to. Same memory backs Claude Desktop, Cursor, Codex
CLI, and any other MCP-aware client. No data goes to OpenAI's memory
service; everything stays in your MNEMOS.

## Prerequisites

- A ChatGPT subscription on **Pro, Team, Enterprise, or Edu** tier.
  Developer Mode connectors are not available on free or Plus.
- Developer Mode enabled on your account: ChatGPT → Settings →
  Connectors → Advanced → Enable Developer Mode.
- A running MNEMOS instance you control (see `DEPLOYMENT.md`).
- A way to expose MNEMOS over HTTPS to the public internet. Options:
  - **ngrok** (easiest): free tier rotates the URL on every restart;
    the $10/mo tier gives a stable subdomain.
  - **Cloudflare Named Tunnel** (free, stable URL, requires owning a
    domain you manage in Cloudflare).
  - **Tailscale Funnel** (free, stable URL on `*.ts.net`, requires a
    Tailnet — which you might already have).
  - **Your own reverse proxy + DNS** (Caddy, nginx, etc.) — most
    control, most setup work.

## Architecture

```
┌────────────────────┐                ┌──────────────────┐
│  ChatGPT Pro web   │                │  MNEMOS          │
│                    │                │                  │
│  Custom Connector  │  HTTPS  ───▶   │  mnemos-mcp-http │
│  Bearer auth       │  /sse          │  :5004 (SSE)     │
└────────────────────┘                │                  │
                                       │  ↓               │
                                       │  Postgres + KG   │
                                       │  + MORPHEUS etc. │
                                       └──────────────────┘
                  via tunnel: ngrok / cloudflared / Tailscale
```

The MCP HTTP/SSE bridge (`mcp_http_server.py`) shares the exact same
`Server("mnemos")` instance and 18 tool definitions as the stdio MCP
server. A memory written from Claude Desktop is queryable from
ChatGPT and vice versa.

## Setup — manual path (works today)

### 1. Bring up the MCP HTTP/SSE bridge

Add to your `docker-compose.override.yml` (PYTHIA prod example):

```yaml
services:
  mnemos-mcp-http:
    image: mnemos-v3x-mnemos
    pull_policy: never
    depends_on:
      - mnemos
    restart: unless-stopped
    command: ["python3", "/app/mcp_http_server.py", "--host", "0.0.0.0", "--port", "5004"]
    ports:
      - "5004:5004"
    environment:
      MNEMOS_MCP_TOKENS: "alice:${MNEMOS_MCP_TOKEN}:${MNEMOS_API_KEY}"
      MNEMOS_BASE: "http://mnemos:5002"
      MNEMOS_API_KEY: "<your existing MNEMOS bearer>"
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Generate a bearer token (this is what ChatGPT will send on every request).
`MNEMOS_MCP_TOKENS` maps that edge token to a backend MNEMOS API key; the
legacy single-user `MNEMOS_MCP_TOKEN` mode still exists but shares one backend
principal for every client:

```bash
export MNEMOS_MCP_TOKEN="$(openssl rand -hex 32)"
```

Bring up the service:

```bash
docker-compose up -d --build mnemos-mcp-http
```

Verify:

```bash
curl http://localhost:5004/healthz
# → ok

curl http://localhost:5004/sse  # without auth
# → 401 with WWW-Authenticate: Bearer realm="mnemos-mcp"

curl -H "Authorization: Bearer $MNEMOS_MCP_TOKEN" \
     http://localhost:5004/sse
# → 200 with content-type: text/event-stream
```

### 2. Open a tunnel

**ngrok (default recommendation):**

```bash
# One-time setup if you haven't:
brew install ngrok        # or: snap install ngrok
ngrok config add-authtoken <your-ngrok-authtoken-from-dashboard>

# Each session:
ngrok http http://192.168.207.67:5004
```

ngrok prints something like:

```
Forwarding   https://abc-123.ngrok-free.app → http://192.168.207.67:5004
```

The `https://abc-123.ngrok-free.app` is your public connector URL.
On free tier this rotates every restart. On paid tier you can pin a
subdomain with `--domain=mnemos.ngrok.app`.

**Cloudflare Tunnel (stable URL, free):**

```bash
cloudflared tunnel login                # one-time, requires domain in CF
cloudflared tunnel create mnemos
cloudflared tunnel route dns mnemos mnemos.yourdomain.com
cloudflared tunnel run --url http://192.168.207.67:5004 mnemos
```

Resulting URL: `https://mnemos.yourdomain.com`.

**Tailscale Funnel:**

```bash
tailscale funnel 5004
```

Resulting URL: `https://<your-machine>.<tailnet>.ts.net`.

### 3. Register the Custom Connector in ChatGPT

ChatGPT → Settings → Developer Mode → Connectors → Add custom

| Field | Value |
|---|---|
| Name | `MNEMOS` |
| Connector URL | `https://abc-123.ngrok-free.app/sse` |
| Authentication | `Bearer Token` |
| Token | `$MNEMOS_MCP_TOKEN` (the value you generated) |
| Description | `Memory across conversations` (whatever you want) |

Click Save. ChatGPT will hit the URL, complete the SSE handshake, and
list the available tools (search_memories, create_memory, get_memory,
list_memories, kg_create_triple, kg_search, DAG tools, recommend_model, etc. —
all 18).

### 4. Use it

In a new ChatGPT conversation, the MNEMOS connector is auto-available.
Ask things like:

- "Search my memory for anything about pgvector benchmarks"
- "Remember that the v3.2.4 release added the APOLLO LLM-fallback warning"
- "What did I decide about the modularization charter?"

ChatGPT calls MNEMOS's MCP tools and folds the results into the
conversation. Same memory is visible from your other agents.

## Setup — assisted path (experimental helper)

The `mnemos-tunnel-setup` helper (`scripts/mnemos_tunnel_setup.py`) is checked
in as an experimental contract. It calls daemon-side `/admin/tunnels/*`
endpoints; use the manual path above unless those endpoints are enabled in
your deployment.

```bash
mnemos-tunnel-setup chatgpt
```

The script walks you through ngrok signup, opens the tunnel, generates the
token, prints the connector config, and copies URL+token to your clipboard.

## Operational notes

- **Token rotation**: change `MNEMOS_MCP_TOKEN`, restart the
  `mnemos-mcp-http` service, update the connector in ChatGPT.
  No coordination with stdio agents needed — they don't use this token.
- **Per-user token model**: prefer `MNEMOS_MCP_TOKENS` so each connector
  bearer maps to a backend user/API key. The legacy `MNEMOS_MCP_TOKEN`
  mode is single-user only; every client sharing it writes as the same
  backend principal.
- **Audit trail**: every connector-driven write goes through
  `/v1/memories` exactly like a normal API call, so the version DAG,
  webhooks, MORPHEUS run tagging, and federation all observe it.
- **Latency**: tunnel hop adds ~30-100 ms depending on geography.
  ChatGPT issues tool calls in parallel; aggregate impact is usually
  invisible.

## Known caveats

- **ngrok free tier URL rotates**: re-paste into ChatGPT after every
  ngrok restart. Use the paid tier or Cloudflare Named Tunnel to fix.
- **TLS is the tunnel's responsibility**: `mcp_http_server.py` listens
  on plain HTTP. Never bind it to a public IP without the tunnel
  providing TLS termination.
- **No OAuth on the MCP edge yet**: bearer auth only. Anyone with the token can
  read/write as the mapped backend principal. Treat the token like an SSH
  private key.
- **Connector tool list updates require reconnect**: if MNEMOS adds
  new tools (e.g., MORPHEUS slice 2 endpoints), ChatGPT may need the
  connector removed and re-added to pick them up.

## Troubleshooting

**ChatGPT shows "couldn't reach connector":**
- Check `curl https://your-tunnel-url/healthz` returns `ok`
- Check `curl -H "Authorization: Bearer $MNEMOS_MCP_TOKEN"
  https://your-tunnel-url/sse` returns 200
- Check the tunnel is still alive (`ngrok` ctrl-C kills it)

**ChatGPT can reach connector but tools don't appear:**
- Look at `docker logs mnemos-v3x-mnemos-mcp-http-1` for SSE handshake
  errors
- Verify the underlying MNEMOS REST API is healthy (the bridge
  delegates everything to it):
  `curl http://localhost:5002/health`

**Bearer token mismatches:**
- The `WWW-Authenticate` header on 401 responses confirms the bridge
  is enforcing bearer auth correctly.
- If ChatGPT specifically shows "401 unauthorized" — the token in
  ChatGPT's connector config doesn't match `$MNEMOS_MCP_TOKEN`. Re-
  copy/paste, or regenerate and update both sides.

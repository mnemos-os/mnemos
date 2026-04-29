# MNEMOS Deployment & Configuration Guide

**Status**: v3.5.x current. v3.5.0 shipped 2026-04-28; v3.5.1 is the 2026-04-28 documentation-triage patch.

---

## Quick Start

### Prerequisites
- PostgreSQL 12+ (for memory storage, audit logs, sessions, DAG versioning)
- Python 3.10+
- LLM provider API keys (Together AI or Groq free tier recommended)
- (Optional) GPU or local inference endpoint for APOLLO's LLM fallback and self-hosted LLMs

### Installation

```bash
# Clone repository
git clone https://github.com/mnemos-os/mnemos.git
cd mnemos

# Copy environment template
cp .env.example .env

# Edit .env with your configuration
nano .env

# Install dependencies (with uv)
uv pip install -r requirements.txt

# Apply database migrations in canonical order
python install.py

# Start MNEMOS server
export $(cat .env | grep -v '#' | xargs)
python -m uvicorn api_server:app --host $MNEMOS_BIND --port $MNEMOS_PORT
```

The API will be available at `http://$MNEMOS_BIND:$MNEMOS_PORT`

---

## Runtime Scaling

MNEMOS runs single-worker by default with `RATE_LIMIT_STORAGE_URI=memory://`.
Horizontal scaling is supported when Redis backs shared rate-limit and
circuit-breaker state; see `docs/SCALING.md`.

---

## SQLite Profile

The SQLite profile is available for local development, laptops, and edge
single-user deployments. Set `MNEMOS_PERSISTENCE_BACKEND=sqlite` with
`MNEMOS_SQLITE_PATH=/path/to/mnemos.sqlite3`, or set
`MNEMOS_PERSISTENCE_BACKEND=auto` with a `sqlite:///...` `MNEMOS_DATABASE_URL`.

See `docs/SQLITE_PROFILE.md` for the constraints: no RLS, no LISTEN/NOTIFY, no
advisory locks, FTS5 instead of PostgreSQL tsvector, and sqlite-vec instead of
pgvector.

---

## MCP HTTP/SSE Auth

The stdio MCP server and HTTP/SSE MCP server expose the same canonical tool
registry. For local single-user stdio clients, set `MNEMOS_BASE` and
`MNEMOS_API_KEY` in the client MCP config so tool calls reach the REST API under
that API key's backend identity.

For HTTP/SSE connectors, prefer per-user token issuance:

```bash
MNEMOS_MCP_TOKENS=alice:<alice-mnemos-api-key>,bob:<bob-mnemos-api-key>
MNEMOS_BASE=http://mnemos:5002
python3 mcp_http_server.py --host 127.0.0.1 --port 5004
```

Each `MNEMOS_MCP_TOKENS` entry is `user_id:token`. The token is accepted at the
MCP edge and is also used as the backend MNEMOS API key, so the REST API applies
that user's normal tenancy. If the connector-facing bearer token must differ
from the backend API key, use `user_id:mcp_token:api_key`.

Legacy `MNEMOS_MCP_TOKEN` remains supported for single-user deployments. In that
mode every accepted MCP HTTP client shares the process-level `MNEMOS_API_KEY`
backend identity; the server logs a WARNING at startup so this collapse is
operationally visible. Do not use shared-token mode for multi-tenant HTTP MCP.

---

## v3.5 Federation Cursor Compatibility

The v3.5 federation feed cursor is opaque and carries both `updated` and
`id`, with feed pages ordered by that same pair. This fixes the timestamp tie
case where a page could end in the middle of many memories sharing one
`updated` value.

No database migration is required. `federation_peers.last_sync_cursor` remains
the existing timestamp column; the puller sends that timestamp as a compound
cursor with the lowest id boundary, uses the peer's compound cursor between
pages during a sync, and persists the timestamp portion for the next completed
sync. Feed servers do not accept timestamp-only cursors; malformed cursors are
treated the same as a missing cursor and start an initial fetch from the
beginning.

---

## v3.5 Webhook Retry Migration Gate

The v3.5 webhook retry migrations change delivery ownership from in-transaction
row locks to persisted attempt leases. Apply this gate when upgrading an
existing deployment:

1. Stop or drain all MNEMOS processes that can write webhook delivery attempts.
2. Run the ordered migrations through `db/migrations_v3_5_webhook_retry_terminal_state.sql`, `db/migrations_v3_5_webhook_attempt_lease.sql`, `db/migrations_v3_5_webhook_writer_revision.sql`, `db/migrations_v3_5_webhook_status_updated_at.sql`, `db/migrations_v3_5_webhook_superseded_marker.sql`, `db/migrations_v3_5_webhook_attempt_unique.sql`, `db/migrations_v3_5_webhook_succeeded_unique.sql`, and `db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql`.
3. Restart MNEMOS workers on the new build.

Draining those writers remains the operationally clean upgrade path.
Superseded attempts use `status='abandoned'` plus `superseded=TRUE`, so retry
chain advancement is visible to audit queries while keeping live recovery
predicates simple.
The live unique index on `(subscription_id, event_type, payload_hash,
attempt_num)` structurally prevents duplicate successor rows if writers race
after a no-successor check. The succeeded unique index on
`(subscription_id, event_type, payload_hash) WHERE status='succeeded'`
structurally enforces one terminal success per retry chain if workers race
past the app-level chain-peer guard. Workers use per-attempt leases plus
per-chain advisory locks, lifecycle starts a dedicated repair worker that runs
repeated sweeps for the first minute independent of delivery send latency, and
current code explicitly writes `NEW_CODE_WRITER_REVISION=1`.
`db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql` adds a database
trigger that fires before any `webhook_deliveries` UPDATE attempting to move a
row away from `status='succeeded'`. A stale writer that tries to revert an ACK
to `pending` or `retrying` fails with SQLSTATE `23514` (`check_violation`) at
the trigger boundary; audit-only updates such as response-body capture or lease
cleanup still succeed.
The startup/periodic repair worker is idempotent and terminalizes any
lease-free `pending` or `retrying` row with a newer successor, including
out-of-order status overwrites of rows already marked `superseded=TRUE`. It skips
rows with an unexpired `lease_token` / `lease_expires_at` pair so an in-flight
new worker can finalize without losing ownership.
Rows with `writer_revision=1` are recoverable immediately.

`WEBHOOK_LEASE_SECONDS` is the authoritative webhook delivery ownership knob.
Claims write and return `lease_expires_at` / `claim_db_now` from PostgreSQL
`clock_timestamp()`, not transaction-snapshot `NOW()`, so time spent waiting
on the per-chain advisory lock cannot backdate the lease. The sender captures
an app-side monotonic anchor immediately before issuing the claim UPDATE, then
subtracts elapsed time since that pre-claim anchor plus
`WEBHOOK_FINALIZE_BUFFER_SECONDS` before starting DNS validation or HTTP POST.
If less than the minimum send window remains, the worker records a retryable
failure instead of posting with a stale lease. Startup still validates that the
configured lease is larger than the finalize buffer. Keep `WEBHOOK_HTTP_TIMEOUT`
at or below the lease-derived send budget if you tune it; `httpx` phase timeouts
are not a replacement for the lease-anchored wall-clock deadline. Outbound
webhook requests send `Accept-Encoding: identity`, response bodies are read
with raw-byte streaming, and any non-identity `Content-Encoding` is not
decompressed; only a small bounded raw preview is retained for audit. Delivery
acknowledgement is based on the HTTP status code once response headers arrive:
2xx is finalized as success even if the body is slow, truncated, or unavailable.
Body capture failures are recorded as audit markers and do not create retries.

`WEBHOOK_SHUTDOWN_DRAIN_SECONDS` controls graceful webhook shutdown. Lifespan
teardown cancels perpetual worker loops first, which stops new recovery
scheduling, then waits for in-flight webhook delivery attempts to finish
finalization without cancellation. The default matches the effective
`WEBHOOK_LEASE_SECONDS` value so a normal lease window can drain. If the drain
timeout expires, shutdown logs the replay risk and cancels remaining delivery
attempts as a last resort.

---

## Configuration

### Minimal Configuration (.env)

This is enough to run MNEMOS with full functionality:

```bash
# Database (required)
PG_HOST=localhost
PG_DATABASE=mnemos
PG_USER=mnemos
PG_PASSWORD=your_secure_password

# API key (required)
MNEMOS_API_KEY=$(openssl rand -hex 32)

# At least one LLM provider (required for /v1/consultations)
# Sign up for free tier at Together AI or Groq
TOGETHER_API_KEY=your_key    # Free tier available
# OR
GROQ_API_KEY=your_key         # Free tier available

# That's it. Everything else is optional.
```

No GPU needed. No inference server needed. Just these 5 variables and you're running MNEMOS in production with full reasoning capability via GRAEAE.

### Full configuration
See `.env.example` for complete options including GPU setup, compression contests, rate limiting, webhooks, OAuth, federation, and observability.

---

## GPU Setup (Optional)

### When You Need GPU

MNEMOS works great on CPU alone. GPU is only beneficial if:
- You want APOLLO's LLM fallback for content that misses a schema (v3.3+)
- You're running large local LLMs (70B+ parameters)

**For most users**: Use external LLM providers (Together AI, Groq) instead. They're cheaper and faster than self-hosting.

### If GPU Makes Sense

**Recommended Hardware:**
- **Mac Mini** (M1/M2/M3, unified memory)
- **ASUS NUC i5** (Intel Arc GPU or iGPU)
- **AMD Ryzen 7/9** (RDNA iGPU)
- **Raspberry Pi 5** (with AI Accelerator kit)
- **NVIDIA Jetson** (Orin, Nano)
- **Any system running vLLM or Ollama**

**Option 1: Ollama (CPU or GPU)**
```bash
# Install Ollama (https://ollama.ai)
ollama serve &

# Pull a small model (works on CPU)
ollama pull phi  # 2.7B, fast on CPU

# Configure MNEMOS (optional — only if using for embeddings)
export GPU_PROVIDER_HOST=http://localhost
export GPU_PROVIDER_PORT=11434

# Start MNEMOS
python -m uvicorn api_server:app
```

**Option 2: vLLM (CPU or GPU)**
```bash
# Install vLLM
pip install vllm

# Run vLLM (works on CPU, much faster on GPU)
python -m vllm.entrypoints.openai.api_server \
  --model mistralai/Mistral-7B-Instruct-v0.1 \
  --port 8000 &

# Configure MNEMOS
export GPU_PROVIDER_HOST=http://localhost
export GPU_PROVIDER_PORT=8000

# Start MNEMOS
python -m uvicorn api_server:app
```

**The real question:** Do you need any of this? **Probably not.** Just use Together AI or Groq (free tier).
```bash
export TOGETHER_API_KEY=your_key
export GROQ_API_KEY=your_key

python -m uvicorn api_server:app
# That's it. No GPU, no inference server, no hassle.
```

---

## Docker Deployment

```bash
# Build image
docker build -t mnemos:latest .

# Run with Docker Compose
docker compose up -d
```

See `docker-compose.yml` for services (PostgreSQL, `postgres-upgrade`,
Ollama, MNEMOS). `postgres-upgrade` is a one-shot migration runner for
existing volumes.

### Docker existing-volume upgrade note

Postgres image init scripts under `/docker-entrypoint-initdb.d` only run
when the data directory is first initialized. They do not re-run when an
existing `postgres_data` volume starts with newer migration files mounted.
For v3.5.x, `docker-compose.yml` and `docker-compose.staging.yml`
therefore include `postgres-upgrade`, which waits for Postgres health and
then applies the v3.5 upgrade tail before the MNEMOS service starts.

The canonical order lives in `install.py` and `installer/db.py`; compose must
mirror those loaders. Current v3.5.x upgrade tail is prefixes 24-38, in
order:
`trigger-same-memory-parent`, `rls-group-select-unix-bits`,
`webhook-retry-terminal-state`, `webhook-attempt-lease`,
`webhook-writer-revision`, `webhook-status-updated-at`,
`webhook-superseded-marker`, `webhook-attempt-unique`,
`webhook-succeeded-unique`, `webhook-succeeded-terminal-trigger`,
`entities-namespace-unique`, `state-journal-namespace`,
`session-compression-ratio-drop`, `session-compression-legacy-drop`, and
`sessions-consultations-namespace`.

Fresh volumes receive these migrations from the initdb mounts, ending at
`/docker-entrypoint-initdb.d/38-sessions-consultations-namespace.sql`. Existing
volumes receive the same SQL through `/migrations/24-...sql` through
`/migrations/38-sessions-consultations-namespace.sql` in the one-shot
`postgres-upgrade` service. Use the `docker-compose.yml` `postgres-upgrade`
service block as the example for manual upgrades.

---

## High Availability and Replication

For single-site deployments (all nodes on same LAN/datacenter), use PostgreSQL
streaming replication. The MNEMOS app talks to a single primary; replicas are
read-only standbys promoted on failure.

Federation is for genuinely-remote replication scenarios — multi-site
deployments, multi-org curated feeds, developer laptop replicas with
intermittent connectivity, and v4 deployment profiles with planned
SQLite-based laptop/local-replica mode.

| Mode | Use when | Latency model | Write model | Layer | Configuration |
|------|----------|---------------|-------------|-------|---------------|
| PostgreSQL streaming replication | Same site / same LAN / same datacenter | Sub-ms latency expected | Single writer; async or sync standby | Postgres-native WAL shipping | Automatic WAL shipping; no MNEMOS app-level config |
| MNEMOS federation | Cross-site, multi-org, laptop replica, or curated remote feed | High latency tolerated | Opt-in per-memory, peer-to-peer | MNEMOS-level (post-write) | Needs `MNEMOS_FEDERATION_PEERS` configuration or registered federation peers |

**Anti-pattern:** Don't use federation between same-LAN nodes — Postgres
streaming replication is faster, simpler, and avoids multi-master dedup work.

See [`docs/STREAMING_REPLICATION.md`](./docs/STREAMING_REPLICATION.md) for the
single-primary + N-standby runbook using `pg_basebackup`, WAL streaming,
promotion, and HAProxy/PgBouncer-style writer endpoints.

---

## Portability, MORPHEUS, and Compression Operations

MPF portability is available through `GET /v1/export` and `POST /v1/import`.
Use root plus `preserve_owner=true` only for authoritative restores or
migrations; non-root imports are scoped to the caller's owner+namespace. The
CLI helpers are `tools/memory_export.py`, `tools/memory_import.py`, and
`tools/mpf_validate.py`.

MORPHEUS dream-state runs are operator-triggered through
`POST /admin/morpheus/runs` and inspected through `/v1/morpheus/runs*`.
Runs are synchronous in v3.5.x, append generated memories tagged with
`morpheus_run_id`, and roll back by deleting memories from that run.

Compression is operator-batched. Use `POST /admin/compression/enqueue` or
`POST /admin/compression/enqueue-all` to feed the contest worker. The active
built-in engines are APOLLO and ARTEMIS; the retired LETHE / ANAMNESIS /
ALETHEIA engines and the legacy session compression columns are not part of
the v3.5.x runtime.

---

## Core API Endpoints

### Consultations (GRAEAE Reasoning)
```bash
# POST /v1/consultations - Create consultation
curl -X POST http://localhost:5002/v1/consultations \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain memory systems",
    "task_type": "reasoning"
  }'

# GET /v1/consultations/{id} - Get consultation
curl -X GET http://localhost:5002/v1/consultations/{id} \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# GET /v1/consultations/audit - List audit log
curl -X GET http://localhost:5002/v1/consultations/audit \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# GET /v1/consultations/audit/verify - Verify audit chain integrity
curl -X GET http://localhost:5002/v1/consultations/audit/verify \
  -H "Authorization: Bearer $MNEMOS_API_KEY"
```

### Memories (MNEMOS Storage)
```bash
# POST /v1/memories - Create memory
curl -X POST http://localhost:5002/v1/memories \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "MNEMOS uses three compression tiers...",
    "category": "solutions"
  }'

# POST /v1/memories/search - Search memories
curl -X POST http://localhost:5002/v1/memories/search \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "compression", "limit": 5}'

# GET /v1/memories/{id} - Retrieve memory
curl -X GET http://localhost:5002/v1/memories/{id} \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# GET /v1/memories/{id}/log - DAG history (git-like)
curl -X GET http://localhost:5002/v1/memories/{id}/log \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# POST /v1/memories/{id}/branch - Create branch
curl -X POST http://localhost:5002/v1/memories/{id}/branch \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "experimental-v2"}'

# POST /v1/memories/{id}/merge - Merge branch
curl -X POST http://localhost:5002/v1/memories/{id}/merge \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source_branch": "experimental-v2", "strategy": "latest-wins"}'
```

### Providers (Model Registry & Routing)
```bash
# GET /v1/providers - List available providers
curl -X GET http://localhost:5002/v1/providers \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# GET /v1/providers/recommend - Get model recommendation
curl -X GET "http://localhost:5002/v1/providers/recommend?task_type=code_generation&cost_budget=5.0" \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# GET /v1/providers/health - Provider health check
curl -X GET http://localhost:5002/v1/providers/health \
  -H "Authorization: Bearer $MNEMOS_API_KEY"
```

Consultation mode selection should match the task risk: use `single` for fast
low-stakes checks, `auto` or `all` for general reasoning, `majority` for binary
decisions where disagreement matters, and `debate` for high-stakes design calls.

### OpenAI-Compatible Gateway
```bash
# POST /v1/chat/completions - OpenAI-compatible (with auto memory injection)
curl -X POST http://localhost:5002/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "What is MNEMOS?"}]
  }'

# GET /v1/models - List available models
curl -X GET http://localhost:5002/v1/models \
  -H "Authorization: Bearer $MNEMOS_API_KEY"
```

### Sessions (Stateful Chat)
```bash
# POST /v1/sessions - Create session
curl -X POST http://localhost:5002/v1/sessions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto"}'

# POST /v1/sessions/{id}/messages - Add message to session
curl -X POST http://localhost:5002/v1/sessions/{id}/messages \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"role": "user", "content": "Hello"}'

# GET /v1/sessions/{id}/history - Get session history
curl -X GET http://localhost:5002/v1/sessions/{id}/history \
  -H "Authorization: Bearer $MNEMOS_API_KEY"

# DELETE /v1/sessions/{id} - Close session
curl -X DELETE http://localhost:5002/v1/sessions/{id} \
  -H "Authorization: Bearer $MNEMOS_API_KEY"
```

---

## Production Deployment

### 1. Database Setup
```bash
# Create database and user
sudo -u postgres createdb mnemos
sudo -u postgres createuser -P mnemos  # Enter password interactively

# Run migrations in canonical order
python install.py

# Existing DBs on v3.4.1 must apply v3.5 migrations 24-38 in order.
# The compose one-shot is the canonical example for existing volumes:
docker compose up postgres-upgrade

# For non-compose installs, follow the same order as install.py / installer/db.py:
psql -U mnemos -d mnemos -v ON_ERROR_STOP=1 \
  -f db/migrations_v3_5_trigger_same_memory_parent.sql \
  -f db/migrations_v3_5_rls_group_select_unix_bits.sql \
  -f db/migrations_v3_5_webhook_retry_terminal_state.sql \
  -f db/migrations_v3_5_webhook_attempt_lease.sql \
  -f db/migrations_v3_5_webhook_writer_revision.sql \
  -f db/migrations_v3_5_webhook_status_updated_at.sql \
  -f db/migrations_v3_5_webhook_superseded_marker.sql \
  -f db/migrations_v3_5_webhook_attempt_unique.sql \
  -f db/migrations_v3_5_webhook_succeeded_unique.sql \
  -f db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql \
  -f db/migrations_v3_5_entities_namespace_unique.sql \
  -f db/migrations_v3_5_state_journal_namespace.sql \
  -f db/migrations_v3_5_session_compression_ratio_drop.sql \
  -f db/migrations_v3_5_session_compression_legacy_drop.sql \
  -f db/migrations_v3_5_sessions_consultations_namespace.sql

# Verify
psql -U mnemos -d mnemos -c "SELECT version();"
```

### 2. Environment Variables
```bash
# Production .env
PG_HOST=db.example.com
PG_DATABASE=mnemos_prod
PG_USER=mnemos
PG_PASSWORD=secure_password_here
PG_POOL_SIZE=50

MNEMOS_BIND=0.0.0.0
MNEMOS_PORT=5002

MNEMOS_API_KEY=$(openssl rand -hex 32)

# LLM providers (minimum: one of these)
TOGETHER_API_KEY=xxx      # Recommended free tier
GROQ_API_KEY=xxx           # Recommended free tier
OPENAI_API_KEY=xxx         # Optional, paid
ANTHROPIC_API_KEY=xxx      # Optional, paid

# GPU provider (OPTIONAL — only for APOLLO LLM fallback or local LLM backends)
# GPU_PROVIDER_HOST=http://gpu.example.com
# GPU_PROVIDER_PORT=8000

CORS_ORIGINS=https://app.example.com,https://api.example.com
ENVIRONMENT=production
LOG_LEVEL=INFO
```

### 3. Systemd Service (Linux)
```ini
# /etc/systemd/system/mnemos.service
[Unit]
Description=MNEMOS Memory System
After=network.target postgresql.service

[Service]
Type=notify
User=mnemos
WorkingDirectory=/opt/mnemos
EnvironmentFile=/opt/mnemos/.env
ExecStart=/usr/bin/python3 -m uvicorn api_server:app \
  --host ${MNEMOS_BIND} \
  --port ${MNEMOS_PORT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable mnemos
sudo systemctl start mnemos
sudo systemctl status mnemos
```

### 4. Reverse Proxy (Nginx)
```nginx
upstream mnemos {
    server 127.0.0.1:5002;
}

server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    location / {
        proxy_pass http://mnemos;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Authorization $http_authorization;
        proxy_pass_header Authorization;
    }
}
```

### 5. Health Monitoring
```bash
# Check health
curl http://localhost:5002/health

# Monitor logs
journalctl -u mnemos -f

# Database statistics
psql -U mnemos -d mnemos -c "SELECT COUNT(*) FROM memories;"
```

---

## Troubleshooting

### GPU Provider Not Found
```bash
# Verify GPU provider is running
curl http://$GPU_PROVIDER_HOST:$GPU_PROVIDER_PORT/health

# Check MNEMOS logs
grep "GPU\|compression\|APOLLO" /var/log/mnemos.log
```

### Memory Search Slow
```bash
# Check indexes
psql -d mnemos -c "SELECT schemaname, tablename, indexname FROM pg_indexes WHERE tablename = 'memories';"

# Re-index if needed
psql -d mnemos -c "REINDEX TABLE memories;"
```

### High Latency
- Reduce `GRAEAE_CONSENSUS_QUORUM_SIZE` (default: 3 providers)
- Enable response caching: `GRAEAE_CACHE_ENABLED=true`
- Check network connectivity to LLM providers

### 409: Memory branch state is inconsistent

v3.5.x maps trigger SQLSTATE `MN001` to HTTP 409 when
`memory_branches` is missing, has `NULL head_version_id`, or points at a
`memory_versions` row from another memory. Inspect and reconcile the
branch rows before retrying:

```sql
SELECT mb.memory_id, mb.name, mb.head_version_id, mv.memory_id AS head_memory_id
FROM memory_branches mb
LEFT JOIN memory_versions mv ON mv.id = mb.head_version_id
WHERE mb.memory_id = '<memory_id>';
```

The correct branch head must be a `memory_versions.id` with the same
`memory_id` as the branch row. Do not repair by pointing at another
memory's version; the v3.5 trigger will reject the next write.

---

## Support

- GitHub: https://github.com/mnemos-os/mnemos
- Issues: https://github.com/mnemos-os/mnemos/issues

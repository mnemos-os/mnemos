# MNEMOS Deployment & Configuration Guide

**Status**: v3.4.1 latest tag; v3.5-dev in flight on branch

---

## Quick Start

### Prerequisites
- PostgreSQL 12+ (for memory storage, audit logs, sessions, DAG versioning)
- Python 3.10+
- LLM provider API keys (Together AI or Groq free tier recommended)
- (Optional) GPU for enhanced compression speeds (Tier 2-3)

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
python -m uvicorn api_server:app --host $MNEMOS_BIND --port $MNEMOS_PORT --workers $MNEMOS_WORKERS
```

The API will be available at `http://$MNEMOS_BIND:$MNEMOS_PORT`

---

## v3.5 Webhook Retry Migration Gate

The v3.5 webhook retry migrations change delivery ownership from in-transaction
row locks to persisted attempt leases. Apply this gate when upgrading an
existing deployment:

1. Stop or drain all MNEMOS processes that can write webhook delivery attempts.
2. Run the ordered migrations through `db/migrations_v3_5_webhook_retry_terminal_state.sql`, `db/migrations_v3_5_webhook_attempt_lease.sql`, `db/migrations_v3_5_webhook_writer_revision.sql`, `db/migrations_v3_5_webhook_status_updated_at.sql`, `db/migrations_v3_5_webhook_superseded_marker.sql`, and `db/migrations_v3_5_webhook_attempt_unique.sql`.
3. Restart MNEMOS workers on the new build.

Draining those writers remains the operationally clean upgrade path, but the
round-8 compatibility path also protects deployments that cannot fully drain.
Superseded attempts use `status='abandoned'` plus `superseded=TRUE`, so old
v3.5-dev workers that only skip `succeeded` and `abandoned` still skip the row.
The live unique index on `(subscription_id, event_type, payload_hash,
attempt_num)` structurally prevents duplicate successor rows if an old writer
races after a new worker's no-successor check. New workers use per-attempt
leases plus per-chain advisory locks, lifecycle starts a dedicated repair
worker that runs repeated sweeps for the first minute independent of delivery
send latency, and the `writer_revision` marker is the technically correct
compatibility path: current code explicitly writes `NEW_CODE_WRITER_REVISION=1`,
while legacy or unknown rows are `NULL` or `0`.
`WEBHOOK_LEGACY_GRACE_SECONDS` remains the safety net for rollouts that skip
the drain: lease-less legacy `pending` and `retrying` rows are not recoverable
until `status_updated_at + WEBHOOK_LEGACY_GRACE_SECONDS` has elapsed. The
`status_updated_at` column is maintained by a database trigger on every status
change, so old-writer `UPDATE status='retrying'` statements automatically
advance the grace clock even though that code knows nothing about the column.
The migration backfill gives live lease-less legacy rows a fresh
`clock_timestamp()` value, so the grace window starts from the migration run
instead of from an old `scheduled_at`.
The startup/periodic repair worker is idempotent and terminalizes any
lease-free `pending` or `retrying` row with a newer successor, including
old-worker status overwrites of rows already marked `superseded=TRUE`. It skips
rows with an unexpired `lease_token` / `lease_expires_at` pair so an in-flight
new worker can finalize without losing ownership.
New-code rows with `writer_revision=1` are recoverable immediately. The default
grace is 300 seconds. Tune it to cover the maximum expected old-writer rollout
overlap; after the grace expires, the new recovery worker treats any
still-running old writer in that gap as crashed.

`WEBHOOK_LEASE_SECONDS` is the authoritative webhook delivery ownership knob.
Claims write and return `lease_expires_at` / `claim_db_now` from PostgreSQL
`clock_timestamp()`, not transaction-snapshot `NOW()`, so time spent waiting
on the per-chain advisory lock cannot backdate the lease. The sender subtracts
app-side monotonic elapsed time plus
`WEBHOOK_FINALIZE_BUFFER_SECONDS` before starting DNS validation or HTTP POST.
If less than the minimum send window remains, the worker records a retryable
failure instead of posting with a stale lease. Startup still validates that the
configured lease is larger than the finalize buffer. Keep `WEBHOOK_HTTP_TIMEOUT`
at or below the lease-derived send budget if you tune it; `httpx` phase timeouts
are not a replacement for the lease-anchored wall-clock deadline. Outbound
webhook requests send `Accept-Encoding: identity`, response bodies are read
with raw-byte streaming, and any non-identity `Content-Encoding` is not
decompressed; only a small bounded raw preview is retained for audit.

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
See `.env.example` for complete options including GPU setup, compression tiers, rate limiting, etc.

---

## GPU Setup (Optional)

### When You Need GPU

MNEMOS works great on CPU alone. GPU is only beneficial if:
- You want fact extraction (ANAMNESIS: 500ms-2s per memory)
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
For v3.5-dev, `docker-compose.yml` and `docker-compose.staging.yml`
therefore include `postgres-upgrade`, which waits for Postgres health and
then applies the v3.5 upgrade migrations through
`db/migrations_v3_5_webhook_attempt_unique.sql` before the MNEMOS service
starts.

Fresh volumes still receive these migrations from the initdb mounts, ending at
`/docker-entrypoint-initdb.d/31-webhook-attempt-unique.sql`. Existing volumes
receive the same SQL through `/migrations/31-webhook-attempt-unique.sql` in
the one-shot service. Keep the compose mounts and `installer/db.py` /
`install.py` migration order in sync.

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
# POST /sessions - Create session
curl -X POST http://localhost:5002/sessions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "compression_tier": 1}'

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

# Existing DBs on v3.4.1 that only need the v3.5 trigger replacement:
psql -U mnemos -d mnemos -v ON_ERROR_STOP=1 \
  -f db/migrations_v3_5_trigger_same_memory_parent.sql

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
MNEMOS_WORKERS=1  # Keep at 1 for in-process state

MNEMOS_API_KEY=$(openssl rand -hex 32)

# LLM providers (minimum: one of these)
TOGETHER_API_KEY=xxx      # Recommended free tier
GROQ_API_KEY=xxx           # Recommended free tier
OPENAI_API_KEY=xxx         # Optional, paid
ANTHROPIC_API_KEY=xxx      # Optional, paid

# GPU provider (OPTIONAL — only if using Tier 2-3 compression)
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
  --port ${MNEMOS_PORT} \
  --workers ${MNEMOS_WORKERS}
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
grep "GPU\|compression\|ANAMNESIS\|APOLLO" /var/log/mnemos.log
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

v3.5-dev maps trigger SQLSTATE `MN001` to HTTP 409 when
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

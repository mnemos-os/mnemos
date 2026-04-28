# MNEMOS Quick Start Requirements

**Applies to**: current v3.5.x release line
**TL;DR**: Python 3.11+, PostgreSQL 16, 8GB RAM, 50GB disk
**Full Details**: See `SYSTEM_REQUIREMENTS.md`

---

## Bare Metal (Fastest to Deploy)

### Install (Ubuntu 22.04 LTS)

```bash
# 1. Python (2 minutes)
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3.11-dev

# 2. PostgreSQL with pgvector (5 minutes)
sudo apt install -y postgresql-16 postgresql-16-pgvector postgresql-client

# 3. System dependencies (1 minute)
sudo apt install -y git curl build-essential libpq-dev

# 4. MNEMOS code (2 minutes)
git clone https://github.com/mnemos-os/mnemos
cd mnemos
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 5. Database setup and migrations (1 minute)
psql -U postgres -d postgres -c "CREATE USER mnemos_user WITH PASSWORD 'mnemos_local';"
psql -U postgres -d postgres -c "CREATE DATABASE mnemos OWNER mnemos_user;"
python install.py

# 6. Configure (if install.py did not already write config)
cp .env.example .env
# Edit .env with your settings

# 7. Run (30 seconds)
python api_server.py
```

**Total Time**: ~15 minutes
**Cost**: Free (if self-hosted)

---

## Docker (Easiest to Deploy)

### Install (Any OS with Docker)

```bash
# 1. Install Docker (5-10 minutes)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. Clone and configure (5 minutes)
git clone https://github.com/mnemos-os/mnemos
cd mnemos
cp .env.example .env
# Edit .env if needed

# 3. Start (2 minutes)
docker compose up -d

# 4. Verify
docker compose logs mnemos
curl http://localhost:5002/health
```

**Total Time**: ~15 minutes
**Cost**: Free (if self-hosted)

For existing Docker volumes, `docker compose up -d` also runs the
one-shot `postgres-upgrade` service before MNEMOS starts. This applies
the v3.5.x migration tail because Postgres initdb scripts do not rerun on
already-initialized volumes.

---

## Minimum Hardware

| Component | Minimum | Why |
|-----------|---------|-----|
| CPU | 2 cores | Asyncio tasks, database threads |
| RAM | 4 GB | Python (500MB) + PostgreSQL (1GB) + OS (2.5GB) |
| Disk | 10 GB | Schema (50MB) + data + buffer |
| OS | Linux/macOS/Windows | Python 3.11+ support |

**Viable**: Raspberry Pi 4 (4GB), ASUS NUC, old laptop
**Recommended**: ASUS NUC i5 or better for production

---

## Required Services

```
PostgreSQL 13+
├─ Extensions: pgvector, pgcrypto, uuid-ossp
├─ Port: 5432 (local or remote)
└─ Storage: 10GB+ (depends on data)

Python 3.11+
└─ asyncio, FastAPI, asyncpg, uvicorn

LLM Provider (pick one)
├─ Together AI (free tier: $5/month)
├─ Groq (free tier: unlimited, rate-limited)
├─ OpenAI (pay-as-you-go, ~$0.01-0.10 per query)
├─ Anthropic (pay-as-you-go, ~$0.01-0.10 per query)
└─ Perplexity (pay-as-you-go, ~$0.01-0.10 per query)
```

---

## Configuration (5 Minutes)

**Minimal Setup (5 required variables)**:
```bash
# .env
PG_HOST=localhost                    # PostgreSQL server
PG_DATABASE=mnemos                   # Database name
PG_USER=mnemos_user                  # Database user
PG_PASSWORD=mnemos_local             # Database password
MNEMOS_API_KEY=my-secret-key-here    # API authentication

# LLM Provider (pick ONE)
TOGETHER_API_KEY=xxx                 # OR
GROQ_API_KEY=xxx                     # OR
OPENAI_API_KEY=xxx                   # etc.
```

**Optional**:
```bash
GPU_PROVIDER_HOST=http://localhost:8000  # For local LLM
REDIS_URL=redis://localhost:6379         # For caching
RATE_LIMIT_ENABLED=true                  # For rate limiting
```

---

## Network Requirements

```
Outbound (MNEMOS to LLM):
  ✅ 443/tcp to api.together.ai OR api.groq.com OR provider
  ✅ 1-10 Mbps bandwidth
  ✅ <500ms latency (OK for up to 2s)

Inbound (Clients to MNEMOS):
  ✅ 5002/tcp (required)
  ✅ Open to world or restricted IPs

Database:
  ✅ 5432/tcp from MNEMOS to PostgreSQL
  ✅ Can be on same machine or remote
```

---

## Performance Expectations

| Task | Time | Notes |
|------|------|-------|
| `/health` check | 10-20ms | Always fast |
| Store memory | 50-200ms | Disk write speed |
| Search memories | 100-500ms | Index size dependent |
| Run consultation | 2-5s | LLM API latency dominates |

---

## Verification (5 minutes)

```bash
# 1. Check Python
python3.11 --version
# Expected: Python 3.11.x or higher

# 2. Check PostgreSQL
psql -U mnemos_user -d mnemos -c "SELECT 1;"
# Expected: returns 1

# 3. Check LLM API
curl https://api.together.ai/status 2>/dev/null | grep -q "ok" && echo "✓ Together AI reachable"
curl https://api.groq.com 2>/dev/null | grep -q "html" && echo "✓ Groq reachable"

# 4. Check MNEMOS
curl http://localhost:5002/health | jq '.version'
# Expected: matches the checked-out tag or branch build
```

---

## Cost Estimates

### Self-Hosted (Bare Metal or VPS)

| Scale | Hardware | Cost/Month | Notes |
|-------|----------|-----------|-------|
| Dev | Old laptop | $0 | Free if you own hardware |
| Small | Raspberry Pi 4 | $0 | Free, just buy Pi once ($100) |
| Medium | VPS 4GB | $20-30 | DigitalOcean, Linode, AWS |
| Large | VPS 16GB | $100-200 | Multiple servers, database replicas |

### LLM Provider (Per Month, Assuming 1000 queries)

| Provider | Free Tier | Cost/1000 queries |
|----------|-----------|-------------------|
| Groq | ✅ Unlimited (rate-limited) | $0 |
| Together AI | ✅ $5 credit | $5-20 |
| OpenAI | ❌ No free | $10-30 |
| Anthropic | ❌ No free | $10-30 |

---

## Scaling Path

```
Development (Single Machine)
├─ Cost: $0-30/month
├─ Hardware: 4GB RAM, 2 cores
├─ Supports: 10K memories
└─ Example: Raspberry Pi or old laptop

Small Production (Single VPS)
├─ Cost: $30-50/month
├─ Hardware: 8GB RAM, 4 cores, 50GB SSD
├─ Supports: 100K memories
└─ Example: DigitalOcean Standard, Linode 8GB

Medium Production (PostgreSQL + Replicas)
├─ Cost: $200-400/month
├─ Hardware: Multiple servers, 16GB RAM each
├─ Supports: 1M memories
└─ Example: AWS RDS + EC2 instances

Large Production (Distributed System)
├─ Cost: $500+/month
├─ Hardware: Load balancer, multiple API servers, database cluster
├─ Supports: 10M+ memories, high availability
└─ Example: Kubernetes cluster, managed database service
```

---

## Troubleshooting

**"ModuleNotFoundError: No module named 'fastapi'"**
```bash
pip install -e .
# or: pip install -r requirements.txt
```

**"psql: error: connection to server at "localhost"**
```bash
# PostgreSQL not running
sudo systemctl start postgresql  # Linux
brew services start postgresql  # macOS
docker compose up postgres      # Docker
```

**"MNEMOS_API_KEY not set"**
```bash
export MNEMOS_API_KEY=$(uuidgen)
# or add to .env file
```

**"Port 5002 already in use"**
```bash
lsof -i :5002
kill -9 <PID>
# or use different port: MNEMOS_PORT=5003 python api_server.py
```

**"Connection timeout to api.together.ai"**
```bash
# Check internet: ping 8.8.8.8
# Check firewall: curl https://api.together.ai
# Check API key: grep TOGETHER_API_KEY .env
```

---

## Next Steps

1. **Review Full Requirements**: `SYSTEM_REQUIREMENTS.md` (detailed info)
2. **Deploy**: Choose bare metal or Docker above
3. **Configure**: Edit `.env` with your API keys
4. **Verify**: Run verification commands above
5. **Test**: `curl http://localhost:5002/health`
6. **Use**: See `DEPLOYMENT.md` for usage examples

---

## Files to Read

| File | Purpose |
|------|---------|
| `SYSTEM_REQUIREMENTS.md` | Complete system requirements (this expanded version) |
| `DEPLOYMENT.md` | How to deploy to production |
| `VERIFICATION_GUIDE.md` | How to verify deployment works |
| `.env.example` | Configuration template |
| `README.md` | General project info |

---

## Support

- **GitHub Issues**: https://github.com/mnemos-os/mnemos/issues
- **Documentation**: https://github.com/mnemos-os/mnemos#readme
- **Community**: Discord/Slack (if applicable)

---

**Version**: v3.5.x; v3.5.1 documentation-triage patch
**Updated**: 2026-04-28
**Accuracy**: Production-verified

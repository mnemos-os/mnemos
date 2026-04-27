# MNEMOS System Requirements

**Applies to**: latest tag v3.4.1 and current v3.5-dev branch
**Updated**: 2026-04-26
**Applies To**: Bare Metal, Docker, and Cloud Deployments

---

## Quick Reference

| Component | Minimum | Recommended | Notes |
|-----------|---------|-------------|-------|
| **CPU** | 2 cores | 4+ cores | More cores = faster GRAEAE consensus |
| **RAM** | 4 GB | 8+ GB | Python + PostgreSQL + service |
| **Disk** | 10 GB | 50+ GB | Depends on memory storage size |
| **Python** | 3.11 | 3.11+ | Required for asyncio features |
| **PostgreSQL** | 13 | 16 (pgvector) | Requires pgvector, pgcrypto, uuid-ossp |
| **Docker** | 20.10+ | 24+ | If using containers |
| **GPU** | Optional | Optional | Not required; fully CPU-capable |

---

## Bare Metal Requirements

### Operating System

**Supported**:
- ✅ Linux (Ubuntu 20.04+, Debian 12+, CentOS 8+, Rocky 9+, AlmaLinux 9+)
- ✅ macOS (12.0+, Intel & Apple Silicon)
- ✅ Windows (WSL2 with Ubuntu 20.04+)
- ✅ BSD (FreeBSD 13+, though untested)

**Recommended**:
- Ubuntu 22.04 LTS (most tested)
- Ubuntu 24.04 LTS (latest, fully supported)
- Debian 12 Bookworm (stable, long-term support)

### Python Runtime

**Minimum**: Python 3.11
**Recommended**: Python 3.12
**Maximum**: No upper limit (test with latest)

**Check Current Version**:
```bash
python3 --version
# Expected: Python 3.11.x or higher
```

**Installation**:
```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.11 python3.11-venv python3.11-dev

# macOS
brew install python@3.11

# Or use pyenv/conda for multiple versions
```

### PostgreSQL Database

**Minimum**: PostgreSQL 13
**Recommended**: PostgreSQL 16 (with pgvector)
**Required Extensions**: pgvector, pgcrypto, uuid-ossp

**Check Current Version**:
```bash
psql --version
# Expected: psql (PostgreSQL) 13.0 or higher
```

**Installation**:
```bash
# Ubuntu/Debian with pgvector
sudo apt install postgresql-16 postgresql-16-pgvector

# macOS
brew install postgresql@16

# Or Docker
docker run -d \
  -e POSTGRES_PASSWORD=password \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

**Extensions Setup** (runs automatically on fresh install):
```bash
psql -U postgres -d mnemos -c "CREATE EXTENSION IF NOT EXISTS pgvector;"
psql -U postgres -d mnemos -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
psql -U postgres -d mnemos -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp;"
```

**Migration note for Docker volumes**: fresh volumes run all mounted
`/docker-entrypoint-initdb.d` SQL. Existing volumes do not. The v3.5-dev
compose files therefore include a one-shot `postgres-upgrade` service
that applies `db/migrations_v3_5_trigger_same_memory_parent.sql` before
MNEMOS starts.

**Storage Requirements**:
- Initial database: ~50 MB (empty schema)
- Per 1M stored memories: +100 MB
- Per 1M consultation records: +50 MB
- Example: 1M memories + 100K consultations = ~200 MB

### Hardware Requirements (Bare Metal)

**Minimum Viable**:
```
CPU:    2 cores (Intel i3 / ARM A72)
RAM:    4 GB
Disk:   10 GB (SSD recommended)
Network: 100 Mbps (for API calls)
```

**Small Production** (10K-100K memories):
```
CPU:    4 cores (Intel i5 / ARM A76)
RAM:    8 GB
Disk:   50 GB SSD
Network: 1 Gbps
```

**Medium Production** (100K-1M memories):
```
CPU:    8 cores (Intel i7/Xeon / ARM A78)
RAM:    16 GB
Disk:   500 GB SSD
Network: 1+ Gbps
```

**Large Production** (1M+ memories):
```
CPU:    16+ cores
RAM:    32+ GB
Disk:   1+ TB NVMe
Network: 10 Gbps
```

**Example Hardware**:
- Minimum: Raspberry Pi 4 (4GB RAM, ARM) — works but slow
- Recommended: ASUS NUC with i5, 16GB RAM — excellent price/performance
- Enterprise: Dell R7515 or HPE DL380 — for 1M+ scale

### System Dependencies

**Ubuntu/Debian**:
```bash
sudo apt install -y \
  build-essential \
  libpq-dev \
  libssl-dev \
  git \
  curl \
  postgresql-client
```

**macOS**:
```bash
brew install postgresql libpq openssl git
```

**Red Hat/CentOS**:
```bash
sudo yum install -y \
  gcc \
  gcc-c++ \
  libpq-devel \
  openssl-devel \
  git \
  curl
```

### Python Dependencies

**Core** (~50 MB):
```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
asyncpg>=0.29.0
psycopg[binary]>=3.1.0
pydantic>=2.8.0
httpx>=0.27.0
```

**Optional** (add only if needed):
```
redis>=5.0.0              # For caching (optional)
sentence-transformers     # For embeddings (optional)
spacy                     # For NLP (optional)
openvino-genai           # For local inference (optional)
```

**Total Python Install Size**: 500 MB - 2 GB (depending on optional deps)

---

## Docker Requirements

### Docker Engine

**Minimum**: Docker 20.10
**Recommended**: Docker 24+ (latest stable)

**Check Version**:
```bash
docker --version
# Expected: Docker version 24.x or higher
```

**Installation**:
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# macOS
brew install docker
```

### Docker Compose

**Minimum**: Docker Compose 2.0
**Recommended**: Docker Compose 2.20+

**Check Version**:
```bash
docker compose version
# Expected: Docker Compose version 2.x or higher
```

**Installation**:
```bash
# Usually included with Docker Desktop
# Or install separately:
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### Container Resources

**Minimum per Container**:
```
MNEMOS API:    200 MB RAM
PostgreSQL:    500 MB RAM
Ollama (optional): 2 GB RAM
Total:         2-3 GB
```

**Recommended per Container**:
```
MNEMOS API:    1 GB RAM
PostgreSQL:    2 GB RAM
Ollama (optional): 4 GB RAM
Total:         7 GB
```

**Host System for Docker**:
```
CPU:    4+ cores
RAM:    8 GB minimum (16+ recommended)
Disk:   20 GB (for images + data volumes)
```

### Docker Image Size

```
Base image:    275 MB (python:3.11-slim)
Dependencies:  400 MB (fastapi, asyncpg, etc.)
Application:   50 MB (MNEMOS code)
Total:         725 MB
```

---

## Network Requirements

### Ports

**Required**:
```
5002/tcp  MNEMOS API (required, must be accessible)
5432/tcp  PostgreSQL (localhost only, not exposed in examples)
```

**Optional**:
```
11434/tcp Ollama (for local embeddings)
6379/tcp  Redis (for caching, if enabled)
8000/tcp  vLLM (for local LLM inference)
```

### External Connectivity

**Required** (for operation):
```
- LLM Provider APIs:
  - api.together.ai:443 (Together AI, recommended)
  - api.groq.com:443 (Groq, recommended)
  - api.openai.com:443 (OpenAI, if used)
  - api.anthropic.com:443 (Anthropic, if used)
  - api.perplexityai.com:443 (Perplexity, if used)

Bandwidth: 1-10 Mbps (depends on query volume)
Latency: <500ms to provider (standard internet acceptable)
```

**Optional** (for features):
```
- News APIs (for web search)
- Stock data APIs (if GRAEAE web_search enabled)

Bandwidth: <1 Mbps
```

### Firewall Rules

**Inbound** (what clients connect to):
```
5002/tcp from 0.0.0.0/0 (open to internet, or restrict to known IPs)
```

**Outbound** (what MNEMOS connects to):
```
5432/tcp to PostgreSQL server (must be reachable)
443/tcp to LLM providers (HTTPS, standard)
80/tcp optional (for HTTP APIs)
```

---

## LLM Provider Requirements

### API Keys Required (Pick One)

**Recommended** (Free Tier Available):
```
Together AI:    TOGETHER_API_KEY
  - Free tier: $5/month
  - No credit card initially (free queries)
  - URL: https://www.together.ai

Groq:          GROQ_API_KEY
  - Free tier: Unlimited (rate-limited)
  - URL: https://console.groq.com
```

**Paid** (More Options):
```
OpenAI:        OPENAI_API_KEY
  - Pay-as-you-go
  - URL: https://platform.openai.com

Anthropic:     ANTHROPIC_API_KEY
  - Pay-as-you-go
  - URL: https://console.anthropic.com

Perplexity:    PERPLEXITY_API_KEY
  - Pay-as-you-go
  - URL: https://www.perplexity.ai
```

### Network Latency Impact

| Latency | User Experience | Recommendation |
|---------|-----------------|-----------------|
| <500ms | Excellent | Normal |
| 500-1000ms | Good | Acceptable |
| 1-2s | Noticeable | For web search only |
| >2s | Slow | Not recommended |

---

## Optional Components

### Redis (Caching)

**Optional**: Yes (fully functional without)
**Use Case**: Reduce API calls for repeated queries

**Minimum**:
```
Version: 6.0+
RAM: 256 MB
Connection: localhost:6379 or remote
```

**Installation**:
```bash
# Ubuntu
sudo apt install redis-server

# macOS
brew install redis

# Docker
docker run -d -p 6379:6379 redis:7-alpine
```

### Ollama (Local Embeddings)

**Optional**: Yes (uses remote provider by default)
**Use Case**: Private deployment, offline operation

**Requirements**:
```
Version: 0.1.0+
GPU: 2 GB VRAM (if using GPU)
CPU: 2 cores (if CPU-only)
RAM: 4 GB
Disk: 5+ GB (for models)
```

**Models**:
```
nomic-embed-text (4 GB) - recommended
all-minilm-l6-v2 (50 MB) - lightweight
```

### vLLM (Local LLM Inference)

**Optional**: Yes (uses remote provider by default)
**Use Case**: Private LLM inference, custom models

**Requirements**:
```
GPU: 4+ GB VRAM (strongly recommended)
CPU Fallback: 8 cores + 16 GB RAM (slow)
RAM: 8+ GB
Disk: 20+ GB
```

---

## Production Deployment Checklist

### Hardware

- [ ] CPU: 4+ cores
- [ ] RAM: 8+ GB
- [ ] Disk: 50+ GB SSD
- [ ] Network: 1+ Gbps, stable connectivity
- [ ] Backup: External backup drive/cloud

### Software

- [ ] OS: Linux (Ubuntu 22.04 LTS recommended)
- [ ] Python: 3.11 or 3.12
- [ ] PostgreSQL: 16 with pgvector extension
- [ ] Docker: 24+ (if using containers)
- [ ] UFW/iptables: Firewall configured

### Configuration

- [ ] .env file created with all required variables
- [ ] Database initialized and migrated
- [ ] PostgreSQL backups configured
- [ ] TLS certificates (if public-facing)
- [ ] Secrets stored securely (not in code)

### Monitoring

- [ ] Log aggregation (optional but recommended)
- [ ] Health checks configured
- [ ] Metrics collection enabled (Prometheus)
- [ ] Alerting configured (if critical)
- [ ] Backup verification script running

---

## Resource Usage Examples

### Small Setup (10K memories, 1K consultations)
```
CPU: 2-4 cores (10-30% utilization)
RAM: 4-6 GB (PostgreSQL: 1GB, MNEMOS: 0.5GB, OS: 2.5GB)
Disk: 15 GB
Annual Cost (VPS): $30-50
Example: DigitalOcean Basic, AWS t3.medium, Linode Nanode
```

### Medium Setup (100K memories, 10K consultations)
```
CPU: 4-8 cores (20-40% utilization)
RAM: 8-12 GB (PostgreSQL: 2GB, MNEMOS: 1GB, OS: 1GB)
Disk: 100 GB
Annual Cost (VPS): $100-200
Example: DigitalOcean Standard, AWS t3.large, Linode Linode 8GB
```

### Large Setup (1M memories, 100K consultations)
```
CPU: 8-16 cores (30-50% utilization)
RAM: 16-32 GB (PostgreSQL: 4GB, MNEMOS: 2GB, OS: 2GB)
Disk: 500 GB
Annual Cost (VPS): $300-600
Example: DigitalOcean Performance, AWS c5.2xlarge, Linode Linode 32GB
```

---

## Performance Characteristics

### API Response Times (p50/p99)

| Operation | Time | Notes |
|-----------|------|-------|
| `/health` | 10/20ms | Always fast |
| `POST /memories` | 50/200ms | Disk I/O bound |
| `/memories/search` | 100/500ms | Depends on index size |
| `POST /consultations` | 2-5s/10s | LLM network latency |
| `/consultations/audit/verify` | 500/2000ms | Hash chain length |

### Memory Per Query

| Operation | RAM Used | Notes |
|-----------|----------|-------|
| Memory search | 5-20 MB | Depends on result count |
| Consultation | 50-500 MB | Depends on prompt size + response |
| Audit log verify | 10-100 MB | Depends on log size |

### Disk I/O

| Operation | I/O | Notes |
|-----------|-----|-------|
| Memory write | 1-10 MB/s | SSD: <10ms latency |
| Full text search | 10-50 MB/s | Index scan |
| Backup | 20-100 MB/s | Depends on backup method |

---

## Scaling Recommendations

### Vertical Scaling (Single Node)

**From**: 10K to 100K memories
```
Increase: RAM from 4GB to 8GB
Cost: ~$20/month additional
Performance: Linear improvement
```

**From**: 100K to 1M memories
```
Increase: RAM to 16GB, CPU to 8 cores, Disk to 500GB
Cost: ~$100-200/month additional
Performance: Linear improvement
```

**From**: 1M+ memories
```
Recommended: Switch to horizontal scaling
Use: PostgreSQL read replicas, load balancer
Cost: $500+/month
```

### Horizontal Scaling

**When**: >1M memories OR >1000 QPS (queries per second)

**Architecture**:
```
Load Balancer
    ↓
[API Server 1] [API Server 2] [API Server 3]
    ↓
PostgreSQL Primary
    ↓
[Read Replica 1] [Read Replica 2]
    ↓
Redis Cluster (optional)
```

**Components**:
- Load Balancer: nginx, HAProxy, or cloud LB
- API Servers: 3+ instances (for redundancy)
- Database: Primary + 2+ read replicas
- Redis: Optional, for caching

---

## Cloud Platform Recommendations

### AWS
- **Compute**: EC2 t3.large (4 vCPU, 8GB RAM) — $60-80/month
- **Database**: RDS PostgreSQL with pgvector — $50-100/month
- **Storage**: 50GB EBS gp3 — $5/month
- **Total**: $115-185/month

### DigitalOcean
- **Compute**: Droplet s-2vcpu-4gb (2 vCPU, 4GB RAM) — $20/month
- **Database**: Managed PostgreSQL 16GB — $35/month
- **Storage**: 50GB Block Storage — $5/month
- **Total**: $60/month

### Linode
- **Compute**: Linode 8GB (4 vCPU, 8GB RAM) — $40/month
- **Database**: Managed DBaaS PostgreSQL — $30-60/month
- **Storage**: Included
- **Total**: $70-100/month

### Google Cloud Platform
- **Compute**: Cloud Run (serverless) — pay-per-request
- **Database**: Cloud SQL PostgreSQL — $60-120/month
- **Storage**: Cloud Storage — $0.02/GB/month
- **Total**: $60-150/month (highly variable)

### Azure
- **Compute**: App Service B2 (1 vCPU, 3.5GB RAM) — $50/month
- **Database**: Azure Database for PostgreSQL — $50-100/month
- **Storage**: Included
- **Total**: $100-150/month

---

## Common Issues & Solutions

### Issue: "Module not found: asyncpg"
```
Cause: Python dependencies not installed
Solution: pip install -e . OR pip install -r requirements.txt
```

### Issue: "PostgreSQL connection refused"
```
Cause: PostgreSQL not running or wrong credentials
Solution: psql -U postgres -d postgres (test connection)
         check PG_HOST, PG_USER, PG_PASSWORD in .env
```

### Issue: "Out of memory"
```
Cause: Insufficient RAM or memory leak
Solution: Increase RAM to 8+ GB
         Check memory usage: free -h (Linux) or top -l1 | head -n 20 (macOS)
         Monitor: docker stats (if Docker)
```

### Issue: "Disk full"
```
Cause: PostgreSQL data growing too large
Solution: Check disk usage: df -h
         Clean up old memories: psql -d mnemos -c "DELETE FROM memories WHERE created_at < NOW() - INTERVAL '90 days';"
         Add storage: expand EBS volume, add new disk, etc.
```

---

## Verification Checklist

- [ ] Python 3.11+ installed: `python3 --version`
- [ ] PostgreSQL 13+ with pgvector: `psql -c "SELECT * FROM pg_extension WHERE extname='vector';"`
- [ ] Network to LLM provider: `curl -I https://api.together.ai/status`
- [ ] Disk space available: `df -h | grep /` shows >5GB free
- [ ] RAM sufficient: `free -h` (Linux) or `system_profiler SPHardwareDataType` (macOS)
- [ ] Port 5002 available: `netstat -an | grep 5002` (should be empty)
- [ ] PostgreSQL running: `psql -c "SELECT 1;"`

---

## Summary

**Absolute Minimum**:
- Python 3.11, PostgreSQL 13, 4GB RAM, 10GB disk
- Works for development/testing, not production

**Production Minimum**:
- Python 3.11+, PostgreSQL 16 (pgvector), 8GB RAM, 50GB SSD
- Supports 10K-100K memories, handles concurrent requests

**Large Scale**:
- Horizontal scaling with load balancer, multiple servers, replicated database
- Supports 1M+ memories, high availability

**GPU (Optional)**:
- Not required; fully functional on CPU
- Recommended for: local embeddings, local LLM inference
- If using: NVIDIA GPU with 4GB+ VRAM, CUDA 11.8+

---

**Last Updated**: 2026-04-26
**Version**: v3.5-dev branch; latest tag v3.4.1
**Status**: Production-ready for tagged releases; v3.5-dev remains unreleased

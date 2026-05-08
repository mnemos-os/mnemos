# MNEMOS Requirements Comparison

**Quick comparison between bare metal and Docker deployments**

> Originally written for v3.1.0 (2026-04-19). The bare-metal vs Docker
> tradeoffs and resource floors below remain accurate as of v5.0.1.

---

## Side-by-Side Summary

| Aspect | Bare Metal | Docker |
|--------|-----------|--------|
| **Setup Time** | 15-30 min | 10-15 min |
| **Complexity** | Moderate (manage services) | Low (single command) |
| **Best For** | Production, custom configs | Development, testing, cloud |
| **Cost** | $0-500/month | $0-500/month (same compute) |
| **Security** | Manual isolation | Container isolation |
| **Upgrades** | Manual (more control) | Automated (easier) |
| **Scaling** | More manual work | Kubernetes-ready |
| **Debugging** | Direct access | Logs via `docker logs` |
| **Performance** | Slightly faster (~5%) | Negligible overhead |

---

## Operating System

### Bare Metal
```
✅ Linux (Ubuntu 20.04+, Debian 12+, CentOS 8+)
✅ macOS (12.0+, Intel & Apple Silicon)
✅ Windows (WSL2 with Ubuntu)
✅ BSD (FreeBSD 13+, untested)

Recommended: Ubuntu 22.04 LTS
```

### Docker
```
✅ Linux (any distribution)
✅ macOS (Intel & Apple Silicon)
✅ Windows (with Docker Desktop)
✅ Any cloud platform

Recommended: Ubuntu 22.04 LTS with Docker Engine
```

**Winner**: Tie (Docker more flexible)

---

## Python Runtime

### Bare Metal
```
Requirement: Python 3.11+
Install:    apt install python3.11 (Linux)
            brew install python@3.11 (macOS)
            python-3.11-installer.exe (Windows)
Virtual Env: python3.11 -m venv venv (required)
Size:       ~500 MB + packages (~200 MB)
```

### Docker
```
Requirement: Bundled in image
Install:    N/A (automatic)
Virtual Env: N/A (containerized)
Size:       275 MB base + 400 MB packages
Isolation:  Complete (separate from system)
```

**Winner**: Docker (no installation, automatic)

---

## PostgreSQL Database

### Bare Metal
```
Requirement: PostgreSQL 13+ with pgvector
Installation Steps:
  1. apt install postgresql-16 postgresql-16-pgvector
  2. sudo systemctl start postgresql
  3. psql -U postgres
  4. CREATE DATABASE mnemos;
  5. Extensions auto-created via migrations

Access:     Direct socket or TCP/IP
Backup:     Manual pg_dump or pg_basebackup
Upgrade:    Manual pg_upgrade (complex)
Default:    Listens on localhost:5432
Size:       Initial 50 MB + data growth
```

### Docker
```
Requirement: PostgreSQL 16 with pgvector (container)
Installation Steps:
  1. docker run -d pgvector/pgvector:pg16
  2. Network auto-configured
  3. Extensions auto-created via migrations

Access:     Via container network (automatic)
Backup:     Via docker cp or volume snapshots
Upgrade:    Pull new image (1 command)
Default:    postgres:5432 (inside container)
Size:       Same 50 MB + data (in volume)
```

**Winner**: Docker (simpler lifecycle, easier upgrades)

---

## System Dependencies

### Bare Metal
```
Required Packages:
  • gcc, g++ (for compiling Python packages)
  • libpq-dev (PostgreSQL client library)
  • libssl-dev (for cryptography)
  • git (for cloning repo)
  • curl (for testing)

Install: apt install -y build-essential libpq-dev libssl-dev git curl

Size:    ~500 MB
Notes:   Must be installed on host, takes time
```

### Docker
```
Required Packages:
  • Bundled in Dockerfile
  • Multi-stage build removes build tools from final image

Install: Automatic (via docker build)

Size:    Minimal in final image (build tools removed)
Notes:   No host system dependencies needed
```

**Winner**: Docker (no system dependencies needed)

---

## Hardware Requirements

### Development/Testing

**Bare Metal**:
```
CPU:    2 cores (Intel i3, ARM A72)
RAM:    4 GB
Disk:   10 GB (any type)
Network: 100 Mbps

Examples:
  • Raspberry Pi 4 (4GB) — works but slow
  • Old laptop/desktop
  • Intel NUC (low-power)
```

**Docker**:
```
CPU:    2 cores (same as bare metal)
RAM:    4 GB (slightly more due to container overhead)
Disk:   15 GB (includes image + volume)
Network: 100 Mbps

Examples:
  • Same hardware as bare metal
  • Cloud VPS (micro tier)
  • Laptop with Docker Desktop
```

**Winner**: Bare metal (slightly lower memory overhead)

---

### Small Production (10K-100K memories)

**Bare Metal**:
```
CPU:    4 cores (Intel i5, Xeon E3)
RAM:    8 GB
Disk:   50 GB SSD
Network: 1 Gbps

Example Hardware:
  • ASUS NUC i5 (~$400-600)
  • Dell OptiPlex 7000 (~$800+)
  • On-premises server
```

**Docker**:
```
CPU:    4 cores (same as bare metal)
RAM:    8 GB (overhead: +512 MB)
Disk:   50+ GB SSD (image cached)
Network: 1 Gbps

Example Platforms:
  • DigitalOcean Droplet s-2vcpu-4gb ($20/month)
  • AWS EC2 t3.medium ($30-40/month)
  • Linode 8GB ($40/month)
  • Azure App Service B2 ($50/month)
```

**Winner**: Docker (easier to scale, pay-per-use)

---

### Large Production (1M+ memories)

**Bare Metal**:
```
CPU:    16+ cores (Xeon E5, AMD EPYC)
RAM:    32+ GB
Disk:   500+ GB NVMe
Network: 10 Gbps

Configuration:
  • Multiple servers (load balancer + API servers)
  • PostgreSQL with replicas
  • Redis cluster (optional)

Cost: $3000-10000+ hardware + hosting

Complexity: High
  • Manual service orchestration
  • Custom replication/backup scripts
  • Complex networking setup
```

**Docker**:
```
CPU:    16+ cores (Kubernetes nodes)
RAM:    32+ GB (across cluster)
Disk:   500+ GB NVMe (distributed storage)
Network: 10 Gbps

Configuration:
  • Kubernetes cluster (EKS/GKE/AKS/self-managed)
  • Managed PostgreSQL RDS/Cloud SQL
  • Redis managed service

Cost: $500-2000+/month cloud platform

Complexity: Moderate
  • Kubernetes YAML files
  • Managed services reduce burden
  • Auto-scaling, self-healing
```

**Winner**: Docker (simpler orchestration, cloud-native)

---

## Network Requirements

### Bare Metal
```
Outbound (Required):
  ✅ 443/tcp to LLM provider (api.together.ai, api.groq.com, etc.)
  ✅ 1-10 Mbps bandwidth
  ✅ <500ms latency

Inbound (Required):
  ✅ 5002/tcp from clients (manually configure firewall)
  ✅ Manual IP whitelisting if needed

Database:
  ✅ 5432/tcp local socket or TCP/IP
  ✅ If remote: requires VPN or secure connection

Setup: Manual firewall rules (UFW, iptables, etc.)
```

### Docker
```
Outbound (Required):
  ✅ Same as bare metal (443/tcp to LLM)
  ✅ Auto-configured via Docker network

Inbound (Required):
  ✅ 5002/tcp from clients (container port mapping)
  ✅ Auto-configured with docker run -p

Database:
  ✅ postgres:5432 internal network
  ✅ No external exposure needed
  ✅ Optional: map to localhost for external access

Setup: Automatic (docker compose handles it)
```

**Winner**: Docker (automatic networking, fewer manual steps)

---

## LLM Provider Connectivity

### Both Identical
```
Requirement: API key for one LLM provider

Recommended (Free Tier):
  • Together AI (TOGETHER_API_KEY) — $5/month free tier
  • Groq (GROQ_API_KEY) — unlimited free (rate-limited)

Optional (Paid):
  • OpenAI (OPENAI_API_KEY) — pay-as-you-go
  • Anthropic (ANTHROPIC_API_KEY) — pay-as-you-go
  • Perplexity (PERPLEXITY_API_KEY) — pay-as-you-go

Latency: <500ms recommended (both bare metal & Docker)
Bandwidth: 1-10 Mbps (identical for both)
```

**Winner**: Tie (exactly the same requirement)

---

## Storage & Persistence

### Bare Metal
```
Database Data:
  • Location: /var/lib/postgresql/16/main/
  • Size: 50 MB + data
  • Backup: Manual pg_dump or file-level snapshots

Configuration:
  • Location: /home/user/mnemos/.env
  • Size: <1 KB
  • Backup: Manual copy

Disk Type:
  • SSD recommended (10x faster than HDD)
  • I/O bound operations (memory search, audit verify)

Expansion:
  • Add new disk and mount
  • OR move database to larger partition
  • Manual process
```

### Docker
```
Database Data:
  • Location: Docker volume (postgres_data)
  • Size: 50 MB + data
  • Backup: docker cp, volume snapshot, or cloud backup

Configuration:
  • Location: .env file (bound to container)
  • Size: <1 KB
  • Backup: Git-tracked (in repo)

Disk Type:
  • SSD recommended (same as bare metal)
  • Performance identical

Expansion:
  • Resize volume automatically (cloud platforms)
  • OR docker cp to larger volume
  • Usually simpler than bare metal
```

**Winner**: Docker (automatic backups, cloud integration)

---

## Monitoring & Logging

### Bare Metal
```
Logs:
  • Application: stdout/stderr in terminal
  • PostgreSQL: /var/log/postgresql/postgresql.log
  • System: /var/log/syslog

Monitoring:
  • Manual: top, htop, iostat
  • Optional: Prometheus, Grafana (must install)

Health Checks:
  • Manual: curl http://localhost:5002/health
  • Systemd: optional service file

Alerting:
  • Manual scripts or third-party services
  • Requires additional setup
```

### Docker
```
Logs:
  • Application: docker logs mnemos
  • PostgreSQL: docker logs postgres
  • All centralized

Monitoring:
  • Built-in: docker stats
  • Optional: docker compose metrics, Prometheus
  • Easier to integrate

Health Checks:
  • Automatic: HEALTHCHECK in Dockerfile
  • Docker recognizes service health
  • Auto-restart on failure

Alerting:
  • Docker integrations available
  • Fewer manual scripts needed
```

**Winner**: Docker (built-in observability, easier to add monitoring)

---

## Upgrade & Maintenance

### Bare Metal
```
MNEMOS Update:
  1. git pull origin main
  2. pip install -e . (reinstall dependencies)
  3. systemctl restart mnemos
  4. Manual database migrations

PostgreSQL Update:
  1. pg_dump mnemos > backup.sql (backup first!)
  2. sudo pg_upgrade (complex)
  3. Fix permissions/extensions
  4. systemctl restart postgresql

Complexity: High (manual steps, potential issues)
Downtime: 5-30 minutes
Rollback: Manual (restore from backup)
```

### Docker
```
MNEMOS Update:
  1. docker compose pull
  2. docker compose up -d (restart, auto-migrate)
  3. Done

PostgreSQL Update:
  1. Change image version in docker-compose.yml
  2. docker compose up -d postgres
  3. Auto-migrates
  4. Extensions pre-configured

Complexity: Low (mostly automated)
Downtime: 1-2 minutes
Rollback: Instant (keep old image/volume)
```

**Winner**: Docker (much simpler upgrades, less downtime)

---

## Cost Comparison (1 Year)

### Bare Metal (10K memories)
```
Hardware (one-time): $300-600 (ASUS NUC)
LLM Provider: $60 (Together AI free tier + Groq)
Electricity: $100-200/year
Internet: $0 (if already have home internet)
Backup Storage: $20-50 (external drive)

Total: $480-910 (amortized)
Per Month: $40-76
```

### Docker on Cloud (10K memories)
```
Compute (VPS): $240/year ($20/month, DigitalOcean)
Database: $420/year ($35/month, managed)
Storage: $60/year ($5/month, block storage)
LLM Provider: $60/year (Together AI/Groq)
Backup: $50/year (cloud snapshots)

Total: $830/year
Per Month: $69
```

### Docker on Kubernetes (1M memories, high availability)
```
Compute: $2400-3600/year (3+ nodes)
Database: $1200/year (managed RDS/Cloud SQL)
Storage: $600/year (persistent volumes)
LLM Provider: $600/year (high query volume)
Monitoring: $200/year (Datadog/New Relic)
Misc: $300/year (backups, security tools)

Total: $5300-6500/year
Per Month: $441-542
```

**Winner**: Tie for small scale, Docker for large scale (no hardware investment)

---

## Summary Table

| Factor | Bare Metal | Docker | Best For |
|--------|-----------|--------|----------|
| **Setup Speed** | 20-30 min | 10-15 min | Docker |
| **Complexity** | Moderate | Low | Docker |
| **Cost (small)** | $40-70/mo | $60-80/mo | Bare Metal |
| **Cost (large)** | $300+/mo | $300-500/mo | Docker (less mgmt) |
| **Reliability** | Good | Better (auto-restart) | Docker |
| **Scaling** | Manual | Auto (Kubernetes) | Docker |
| **Monitoring** | Manual | Built-in | Docker |
| **Upgrades** | Complex | Simple | Docker |
| **Performance** | ~5% faster | Negligible overhead | Bare Metal |
| **Lock-in** | None | Docker ecosystem | Bare Metal |

---

## Recommendation Matrix

### Choose Bare Metal If:
- ✅ Running on existing hardware you own
- ✅ Need absolute maximum performance (5% faster)
- ✅ Prefer direct OS access
- ✅ Want zero container overhead
- ✅ Have strong Linux sysadmin skills
- ✅ Running single, high-security environment

### Choose Docker If:
- ✅ Want fastest setup (<15 minutes)
- ✅ Planning to scale (Kubernetes-ready)
- ✅ Using cloud platforms (AWS, GCP, Azure)
- ✅ Want easy updates and rollbacks
- ✅ Need automated health checks
- ✅ Have small team (less operational burden)
- ✅ Running development/test environments

### Hybrid (Recommended for Most)
- ✅ Docker for development/testing
- ✅ Bare metal for single-node production (existing hardware)
- ✅ Docker + Kubernetes for large-scale production

---

## Final Recommendation

**For First-Time Users**: Use Docker
- Faster setup, less to learn
- Less operational burden
- Easy to migrate to cloud later

**For Existing Infrastructure**: Use Bare Metal
- Leverage existing hardware
- Full control and visibility
- Cost savings if hardware available

**For Production Scale**: Use Docker + Kubernetes
- Auto-scaling, resilience, ease of operations
- Industry-standard deployment pattern

---

**Originally Written For**: v3.1.0  
**Verified Current For**: v5.0.1 (2026-05-08)  
**Audience**: DevOps, SREs, developers making deployment decisions

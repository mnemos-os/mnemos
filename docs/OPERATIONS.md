# MNEMOS Operations — Multi-Node Deployment & Maintenance

**Status:** Canonical (v5.0.1 production line, updated 2026-05-08)
**Audience:** Operators, SREs, release engineers
**Scope:** Continuous operation and maintenance of MNEMOS production + staging + test clusters

---

## 1. Overview

This document specifies the operational practices for running MNEMOS across multiple physical nodes. MNEMOS ships as a single Python package; production deployment spans at least three tiers — production (PYTHIA + CERBERUS), staging (PROTEUS), and test (ephemeral docker-compose + test database). The topology is fronted by a load balancer (nginx on ARGONAS), backed by a replicated PostgreSQL cluster (pgha-primary + pgha-standby), and capable of federation (peer-to-peer memory sync with sister MNEMOS deployments).

This doc covers:
- Topology and roles (what runs where)
- Release cadence and promotion path (feature → staging → production)
- Schema migrations (safe sequencing, idempotency, replication)
- Backup and disaster recovery
- Monitoring and alerting (current state + required additions)
- Blue-green deployment pattern (how to upgrade prod zero-downtime)
- Drift detection (catching version skew and stale containers)
- Federation health (peer heartbeat, detection of silent failures)

What this doc does NOT cover:
- Application code (see `docs/V3_5_CHARTER.md`, `docs/V3_6_CHARTER.md`, etc. for feature roadmap)
- High-level architecture (see `README.md` and `docs/MEMORY_ARCHITECTURE.md`)
- User-facing API (see `API_DOCUMENTATION.md` (root) and `docs/SPECIFICATION.md`)

**Last verified:** 2026-04-26 (all three remotes converged, prod healthy, staging not yet live)

---

## 2. Topology

### 2.1 Diagram and physical nodes

```
                        ARGONAS (.101)
                         nginx LB
                         :80/:443
                      ____|____
                     /         \
                    /           \
              PYTHIA (.67)    CERBERUS (.96)
              v5.3 prod       dark prod / GPU host
              pg17 primary    standby + inference
              11,756 memories + Apollo Gemma 4 (ports 8080/8081)
              5,045 compressions
              pgha-primary
                    |
                    | replication
                    |
                pgha-standby
                (CERBERUS pg17)


              PROTEUS (.25)
              staging / restore-drill target
              Intel i7-6700, 60GB RAM
              Runs latest cut during release drills
              GPU calls proxy to CERBERUS
```

### 2.2 Per-node roles

| Host | IP | OS | CPU | RAM | GPU | Role | MNEMOS version | Status |
|---|---|---|---|---|---|---|---|---|
| **PYTHIA** | 192.168.207.67 | Ubuntu 22.04 | 12-core | 30GB | — | Primary (prod) + GRAEAE + CNXN | v5.3 stable target | ✅ Operational |
| **CERBERUS** | 192.168.207.96 | Debian 12 | 24-core (Threadripper) | 125GB | RTX 4500 ADA 24GB | Secondary/dark prod + Apollo GPU inference | v5.3 stable target | ✅ Operational |
| **PROTEUS** | 192.168.207.25 | Debian 12 | Intel i7-6700 | 60GB | — | Staging + restore-drill target | latest cut / release drills | ✅ Used for drills |
| **ARGONAS** | 192.168.207.101 | TrueNAS | — | — | — | NFS + git origin (planned: LB) | nginx 1.26 (TrueNAS UI proxy only) | ✅ Running |

### 2.3 Network & authentication

> **Current-state caveat (verified 2026-04-26):** The nginx running on ARGONAS today is the TrueNAS web UI proxy, **NOT** a MNEMOS HTTP load balancer. All `proxy_pass` entries route to `127.0.0.1:6000` (TrueNAS middleware). There is **no HTTP LB in front of MNEMOS today** — clients hit PYTHIA at `192.168.207.67:5002` directly. CERBERUS `:5003` is currently *dark prod* (running but not externally routed). Standing up a real LB on ARGONAS (or elsewhere) is a **production rollout prerequisite** for the blue-green deploy pattern below to function. Until that's done, "drain a node" means "stop sending it traffic from clients you control" — there's no upstream pool to manipulate.

- **External (planned):** ARGONAS nginx LB listens on :80 (http) and :443 (https); backends are PYTHIA + CERBERUS on private :5002 + :5003. Status: NOT YET CONFIGURED.
- **External (today):** Clients hit PYTHIA `192.168.207.67:5002` directly. No fronting LB.
- **Internal:** Backends communicate directly with each other via private IP; no SSH tunneling needed
- **Database:** PostgreSQL unix-socket on same host; replication via TCP between PYTHIA and CERBERUS
- **Auth:** Bearer token in `Authorization: Bearer <token>` header; token validation done by FastAPI dependency `get_current_user` in `mnemos/api/dependencies.py`
- **Federation:** Peer-to-peer sync uses HTTP + bearer tokens; same auth model as client API

---

## 3. Three-tier version policy

Production MNEMOS runs on a **canary + ratchet** model: new features bake in staging before any production promotion.

| Tier | Host(s) | Version target | Stability | Deployment source |
|---|---|---|---|---|
| **Prod** | PYTHIA + CERBERUS | *latest stable* (v5.0.x) | GA, no alpha/beta | git tag, N+1 weeks after staging bake |
| **Staging** | PROTEUS | *latest cut* / next release branch | alpha/rc, real federation | release branch, merged + tagged |
| **Test** | docker-compose + mnemos-test-pg on CERBERUS | *feature branches* | ephemeral, parallel | PR builds via CI, cleaned up post-merge |

**Promotion path:**
```
feature branch (local dev)
    ↓ (git push)
master (alpha tag + CI) → deploy PROTEUS
    ↓ (1–2 weeks bake with real federation + load)
stable tag → blue-green upgrade PYTHIA + CERBERUS → both prod
    ↓ (ongoing)
PROTEUS continues to lead (tests features for next release)
```

**Version skew tolerance:** Prod nodes may be at different versions for up to 2 hours during a blue-green upgrade (one node rolling). Staging may lag prod by 1–2 weeks. Test is ephemeral (no SLA).

---

## 4. Release workflow

### 4.1 Merge to master (alpha stage)

When a feature branch is merged to master:
1. CI runs (lint + unit tests + integration tests on CERBERUS pg17 test instance)
2. On success, tag `v<major>.<minor>.<patch>-alpha.<N>` (e.g., `v3.4.0-alpha.1`)
3. Push tag to github + gitlab + argonas
4. Changelog updated in `CHANGELOG.md` with link to alpha tag
5. CI publishes `mnemos:v3.4.0-alpha.1` to ghcr.io

### 4.2 Deploy to PROTEUS (staging bake)

After alpha tag, deploy to staging:
```bash
# On PROTEUS via SSH
sshpass -p $PROTEUS_SUDO_PASS ssh root@192.168.207.25 "
  cd /opt/mnemos && \
  git fetch origin v3.4.0-alpha.1 && \
  git checkout v3.4.0-alpha.1 && \
  pip install -e . && \
  sudo systemctl restart mnemos
"

# Verify
curl -H "Authorization: Bearer $TOKEN" http://192.168.207.25:5002/health
# Expected: {"version": "v3.4.0-alpha.1", "status": "healthy", ...}
```

Staging runs for 1–2 weeks. During this period:
- Federation sync is tested (PROTEUS pulls from PYTHIA and vice versa)
- Real-world query patterns exercised
- Any regressions caught before production

### 4.3 Tag stable + blue-green deploy (production)

When staging is ready, tag stable:
```bash
git tag -a v3.4.0 -m "MNEMOS v3.4.0 — stable" <sha>
git push origin v3.4.0 gitlab v3.4.0 argonas v3.4.0
```

Then **blue-green upgrade** of production:

**Phase 1: Drain PYTHIA from LB**
```bash
# On ARGONAS nginx
ssh root@192.168.207.101 "
  # Edit /etc/nginx/sites-enabled/mnemos-upstream (or equivalent)
  # Change PYTHIA entry from 'up' to 'down' (comment it out)
  nginx -s reload
  # Verify: curl http://localhost/ routes only to CERBERUS
"
```

**Phase 2: Upgrade PYTHIA**
```bash
# On PYTHIA
sshpass -p $PYTHIA_SUDO_PASS ssh root@192.168.207.67 "
  # Pre-upgrade backup
  pg_dump -U postgres mnemos | gzip > \
    /mnt/argonas/backups/mnemos/pre-v3.4.0-upgrade-$(date +%Y%m%d_%H%M%S).sql.gz
  
  # Upgrade
  cd /opt/mnemos && \
  git fetch origin v3.4.0 && \
  git checkout v3.4.0 && \
  pip install -e . && \
  
  # Run migrations (see §5 for safety checks)
  python -m mnemos.installer migrate --db-name=mnemos
  
  # Restart
  sudo systemctl restart mnemos
  
  # Verify
  curl -H 'Authorization: Bearer $TOKEN' http://localhost:5002/health
"
```

**Phase 3: Smoke test PYTHIA**
```bash
# Run a quick integration test
curl -X POST http://192.168.207.67:5002/v1/memories/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "limit": 5}'
# Expected: 200 OK, results list
```

**Phase 4: Rejoin PYTHIA to LB**
```bash
ssh root@192.168.207.101 "
  nginx config: re-enable PYTHIA entry
  nginx -s reload
"
```

**Phase 5: Repeat for CERBERUS**
```bash
# Drain CERBERUS, upgrade docker containers + migrations, smoke test, rejoin
# (Same pattern as PYTHIA; see detailed script in §12)
```

**Rollback (if needed):**
If smoke test fails:
1. Drain upgraded node from LB
2. `git checkout <previous-tag>` + `pip install -e .`
3. `systemctl restart mnemos`
4. Verify `/health` returns OK
5. Rejoin to LB

---

## 5. Schema migration cadence

### 5.1 Idempotency requirement

Every migration must be **safely re-runnable without data loss or corruption**. This means:

- `CREATE TABLE IF NOT EXISTS` (never bare `CREATE TABLE`)
- `CREATE INDEX IF NOT EXISTS` (never bare `CREATE INDEX`)
- `CREATE OR REPLACE FUNCTION` (never `CREATE FUNCTION` without `OR REPLACE`)
- For column additions: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (PostgreSQL 10+)
- For column drops: wrap in a function with exception handling, or use conditional logic

**Empirical validation:** Migration `db/migrations_charon_trigger_guard.sql` (v3.3) verified as idempotent on 2026-04-25: re-ran it twice on test database, schema and data unchanged both times.

### 5.2 Migration execution

Migrations run **privileged** (postgres superuser), never as the MNEMOS app role:

```bash
# Via installer (recommended)
python -m mnemos.installer migrate --db-name=mnemos --db-user=postgres

# Manually (if needed)
sudo -u postgres psql -d mnemos -v ON_ERROR_STOP=1 \
  -f db/migrations_v3_5_trigger_same_memory_parent.sql
```

See `mnemos/installer/db.py` for the canonical migration order. Docker compose,
the CLI installer, and manual runbooks must mirror that loader.

### 5.3 Pre-migration backup (mandatory)

Before any migration on production:

```bash
# Full dump to NFS (preserves both schema and data)
pg_dump -U postgres mnemos | gzip > \
  /mnt/argonas/backups/mnemos/pre-<version>-<timestamp>.sql.gz

# Verify dump
gunzip -t /mnt/argonas/backups/mnemos/pre-v3.4.0-20260426_120000.sql.gz
# (should exit 0 with no errors)
```

### 5.4 Replication and migration propagation

MNEMOS uses **streaming replication** from PYTHIA (primary) to CERBERUS (standby). Schema changes propagate **automatically via WAL** — the standby does not apply migrations independently.

Consequence: After upgrading PYTHIA to a new version with a new migration:
1. PYTHIA runs the migration (superuser-privileged)
2. Changes enter the WAL stream
3. CERBERUS standby replicates the WAL (zero-downtime, automatic)
4. When you promote CERBERUS, it already has the schema

Do not attempt to run a migration directly on the standby.

### 5.5 v3.5 trigger replacement migration

`db/migrations_v3_5_trigger_same_memory_parent.sql` replaces
`mnemos_version_snapshot()` with the same-memory branch HEAD guard.
The migration is `CREATE OR REPLACE FUNCTION`, so it is safe to re-run.

Fresh Docker volumes receive it from:

```text
/docker-entrypoint-initdb.d/24-trigger-same-memory-parent.sql
```

Existing Docker volumes do not re-run initdb scripts. For that case,
`docker-compose.yml` and `docker-compose.staging.yml` include a one-shot
`postgres-upgrade` service that waits for Postgres to be healthy and then
runs:

```bash
psql -h postgres -U mnemos_user -d mnemos \
  -v ON_ERROR_STOP=1 \
  -f /migrations/24-trigger-same-memory-parent.sql
```

Manual equivalent for bare-metal or systemd deployments:

```bash
sudo -u postgres psql -d mnemos -v ON_ERROR_STOP=1 \
  -f db/migrations_v3_5_trigger_same_memory_parent.sql
```

Post-apply smoke check:

```sql
SELECT proname
FROM pg_proc
WHERE proname = 'mnemos_version_snapshot';
```

### 5.6 `MN001` / HTTP 409 branch reconciliation

`MN001` means the trigger found broken branch state while resolving the
parent for an UPDATE or DELETE. The API maps it to HTTP 409 through
`handle_trigger_pgerror` (`mnemos/core/visibility.py`).

Causes:

- `memory_branches` row is missing for the memory + branch.
- `memory_branches.head_version_id` is `NULL`.
- `head_version_id` points at a `memory_versions.id` from another memory.
- The branch row disappeared before the trigger could advance HEAD.

Inspection query:

```sql
SELECT
  mb.memory_id,
  mb.name,
  mb.head_version_id,
  mv.memory_id AS head_memory_id,
  mv.version_num,
  mv.commit_hash
FROM memory_branches mb
LEFT JOIN memory_versions mv ON mv.id = mb.head_version_id
WHERE mb.memory_id = '<memory_id>'
ORDER BY mb.name;
```

Reconciliation procedure:

1. Confirm the intended branch and memory ID from the failing request or logs.
2. If the branch row is missing, reconstruct it only from a
   `memory_versions` row with the same `memory_id`.
3. If `head_version_id` is `NULL`, set it to the highest intended
   visible version for that branch and memory.
4. If `head_memory_id != memory_id`, do not keep the foreign pointer.
   Choose a same-memory version row or quarantine the branch until the
   restore/import that created the pointer is understood.
5. Retry the original write after the branch row resolves to a
   same-memory version.

Do not repair by reusing another memory's version ID. Slice 2 made that
failure explicit so corrupt ancestry cannot be hidden by the next normal
write.

---

## 6. Backup and disaster recovery

### 6.1 Automated daily backup

Daily automated backup at **03:00 UTC** to ARGONAS NFS:

```bash
# Cron on PYTHIA (or ARGONAS as a separate job)
0 3 * * * pg_dump -U postgres mnemos | gzip > \
  /mnt/argonas/backups/mnemos/daily-$(date +\%Y\%m\%d-\%H\%M\%S).sql.gz && \
  find /mnt/argonas/backups/mnemos/ -name 'daily-*.sql.gz' -mtime +30 -delete
```

**Retention:** 30 days rolling (oldest backup is ~29 days old at any given time).

### 6.2 Pre-migration snapshot (explicit)

In addition to daily backup, take an explicit snapshot before any production migration:

```bash
# Named for traceability
pg_dump -U postgres mnemos | gzip > \
  /mnt/argonas/backups/mnemos/<version>-pre-<datestamp>.sql.gz
```

### 6.3 Restore procedure (quarterly drill)

**Status:** Dev↔prod MPF restore drill documented and last run for v3.4.1; repeat before high-risk schema work.
Repeat quarterly and before high-risk schema work.

To restore from backup to PROTEUS (test/staging host):

```bash
# 1. Get latest backup
BACKUP=/mnt/argonas/backups/mnemos/daily-20260426-030000.sql.gz

# 2. Verify integrity
gunzip -t $BACKUP

# 3. Create test database on PROTEUS
ssh root@192.168.207.25 "createdb -U postgres mnemos_restore_test"

# 4. Restore
ssh root@192.168.207.25 \
  "gunzip < $BACKUP | psql -U postgres -d mnemos_restore_test"

# 5. Verify app can read
PG_DATABASE=mnemos_restore_test python -m pytest tests/test_memories.py -k "test_list" -v

# 6. Cleanup
ssh root@192.168.207.25 "dropdb -U postgres mnemos_restore_test"
```

### 6.4 Important: HA is NOT backup

The pgha-primary/pgha-standby replication layer is **failover**, not **backup**. If corruption is silently written to primary, the standby replicates it. Backups are essential.

---

## 7. Monitoring and alerting

### 7.1 Current state (verified 2026-04-26)

**Existing monitoring:**
- Grafana + Prometheus + cAdvisor on PYTHIA (4+ days uptime)
- Per-node `/health` endpoint (returns JSON status + version)
- Structured logging via `structlog` → stdout → journalctl (or docker logs)
- Request-ID correlation via `mnemos/core/observability.py` (soft-optional deps)

**Current gaps:**
- No Slack/Signal/PagerDuty wiring (manual eyeball only)
- No automated alerting on `/health` failures
- No monitoring of replication lag
- No drift detection (version skew between prod nodes)
- No federation peer heartbeat detection

### 7.2 Required alerts (planned)

| Alert | Condition | Action | Severity |
|---|---|---|---|
| **Backend down** | Any node `/health` returns non-200 for >2min | Page on-call | P1 |
| **Replication lag** | `pg_replication_slots` lag >30s | Warn | P2 |
| **Version skew** | Prod nodes at different versions >2h | Warn | P2 |
| **Multiple containers** | Node running 2+ MNEMOS containers | Error (fix immediately) | P1 |
| **Version drift** | Node version ≠ declared target >24h | Warn | P2 |

### 7.3 Health check implementation

Each MNEMOS node exports `/health`:

```bash
curl -H "Authorization: Bearer <token>" \
  http://<host>:5002/health
```

Response (JSON):
```json
{
  "status": "healthy",
  "version": "5.0.1",
  "profile": "server",
  "database_connected": true,
  "distillation_worker": "healthy"
}
```

**Monitoring interval:** 30 seconds (detect failures within ~1 minute).

### 7.4 Dashboard: "What version is running where?"

Simple dashboard (Grafana or custom HTML) that queries each node's `/health` and shows:

```
PYTHIA:   5.0.1         (prod target)
CERBERUS: 5.0.1         (dark prod / GPU host)
PROTEUS:  next cut      (staging / restore-drill target)
```

Update frequency: 5 minutes (sufficient for drift detection).

---

## 8. LB drain and rejoin

### 8.1 ARGONAS nginx configuration (TBD)

**Status:** Config location and exact structure not yet read (permission rate-limit on 2026-04-26). To be confirmed next ops cycle.

**Expected pattern:**

```nginx
upstream mnemos_backends {
    # server 192.168.207.67:5002 max_fails=2 fail_timeout=10s;  # down (draining)
    server 192.168.207.96:5003;                                  # up
}

server {
    listen 80;
    location / {
        proxy_pass http://mnemos_backends;
        proxy_connect_timeout 5s;
        proxy_read_timeout 10s;
    }
}
```

### 8.2 Drain procedure

To remove a node from load balancer (e.g., for maintenance):

```bash
# 1. Comment out the node's upstream entry (or mark 'down')
ssh root@192.168.207.101 "
  sed -i 's/^server 192.168.207.67:5002/# server 192.168.207.67:5002/' \
    /etc/nginx/sites-enabled/mnemos-upstream
  nginx -s reload
"

# 2. Verify: all traffic routes only to CERBERUS
curl http://192.168.207.101/ -v
# (should see X-Request-Server: CERBERUS or similar header)

# 3. Wait for existing connections to drain (30–60 sec)
sleep 60

# 4. Proceed with maintenance (upgrade, restart, etc.)
```

### 8.3 Rejoin procedure

After maintenance:

```bash
# 1. Verify node is healthy
curl -H "Authorization: Bearer $TOKEN" \
  http://192.168.207.67:5002/health
# Expected: 200 OK, "status": "healthy"

# 2. Re-enable in nginx upstream
ssh root@192.168.207.101 "
  sed -i 's/^# server 192.168.207.67:5002/server 192.168.207.67:5002/' \
    /etc/nginx/sites-enabled/mnemos-upstream
  nginx -s reload
"

# 3. Verify: traffic routes to both nodes
for i in {1..5}; do
  curl http://192.168.207.101/ -H "Authorization: Bearer $TOKEN" | \
    jq '.debug.node'
done
# Should see both PYTHIA and CERBERUS in results
```

### 8.4 Alternative: health-check-driven drain

Instead of explicit manual drain, nginx can auto-detect failures via health checks:

```nginx
upstream mnemos_backends {
    server 192.168.207.67:5002 max_fails=2 fail_timeout=10s check interval=5000 rise=2 fall=3;
    server 192.168.207.96:5003 max_fails=2 fail_timeout=10s check interval=5000 rise=2 fall=3;
}
```

With this config, if a node's `/health` returns non-200 three times in a row, nginx stops routing to it automatically. **Status:** Not yet confirmed on ARGONAS; requires ngx_http_upstream_check_module (non-standard).

---

## 9. Drift detection

### 9.1 Problem

During the 2026-04-26 audit:
- CLAUDE.md claimed PYTHIA was v3.2.0; reality was v3.3-alpha.1
- CERBERUS had both v3.1.0 (dev artifact on port 5002) and v3.2.0 (prod on port 5003) running simultaneously
- No automated detection caught the drift

### 9.2 Solution: weekly version check

Implement `scripts/ops/version_check.sh`:

```bash
#!/bin/bash
# Check each node's version vs. declared target

PYTHIA_DECLARED="5.0.1"
CERBERUS_DECLARED="5.0.1"
PROTEUS_DECLARED="next-cut"

PYTHIA_ACTUAL=$(curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.207.67:5002/health | jq -r .version)
CERBERUS_ACTUAL=$(curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.207.96:5003/health | jq -r .version)

if [[ "$PYTHIA_ACTUAL" != "$PYTHIA_DECLARED" ]]; then
  echo "DRIFT: PYTHIA actual=$PYTHIA_ACTUAL, declared=$PYTHIA_DECLARED"
  logger -t mnemos-drift "PYTHIA version mismatch"
fi

if [[ "$CERBERUS_ACTUAL" != "$CERBERUS_DECLARED" ]]; then
  echo "DRIFT: CERBERUS actual=$CERBERUS_ACTUAL, declared=$CERBERUS_DECLARED"
  logger -t mnemos-drift "CERBERUS version mismatch"
fi
```

**Run:** Weekly via cron (e.g., `0 8 * * 1` Monday morning).

### 9.3 Container cleanup

Quarterly, audit running containers on each node and retire stale ones:

```bash
# On CERBERUS (find stale MNEMOS containers)
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

# Example stale artifact from 2026-04-26:
# mnemos-v3.1.0-dev   Exited (137) 3 weeks ago   mnemos:v3.1.0

# Remove:
docker rm mnemos-v3.1.0-dev
docker rmi mnemos:v3.1.0
```

---

## 10. GitHub platform incident response

If the `perlowja` GitHub account becomes unreachable for pushes, run this diagnostic before assuming rate-limit / billing / outage:

| Check | Result if T&S-restricted |
|---|---|
| `curl -H "Authorization: token $GH_TOKEN" https://api.github.com/rate_limit` | All headroom unused — NOT rate-limit |
| `curl -H "Authorization: token $GH_TOKEN" https://api.github.com/user` | 200 OK — auth still works |
| `curl https://api.github.com/users/perlowja` (no auth) | 404 even though account exists — the tell |
| GitHub UI banner | "Plan upgrades blocked" + future-dated "reset" → enforcement, not billing |

The combination `rate-limit-clean + auth-works + public-404` = T&S account-hide. NOT rate-limit, NOT billing.

**Resolution path (established 2026-04-26):**
1. Outreach to GitHub Head of OSPO via LinkedIn-direct, LF-affiliation framing
2. Continue dev on GitLab + ARGONAS bare repos during restriction (NEVER create new GitHub repos/branches/gists during the window — confirms abuse-heuristic suspicion)
3. Resume GitHub pushes only after restriction is lifted

Goodwill is finite — escalation channel is for real platform-enforcement only, not routine support. Routine billing/quota issues go through https://support.github.com/ first.

See `~/.claude/rules/github-behavior.md` for full rate-limit rules and the rationale.

---

## 11. Federation health

### 11.1 Architecture (v5.0.1 current)

Federation (peer-to-peer memory sync) is specified in `mnemos/api/routes/federation.py`. Key points:

- Each MNEMOS node maintains a `federation_peers` table (schema in `db/migrations_v3_federation.sql`)
- Sync is **pull-based:** node A asks node B for updates since the last sync point
- Compound-cursor pagination over `(updated, id)` (not full-dump on every sync)
- **Status as of 2026-05-02:** Schema-compat preflight, restore drills, the
  stable compound cursor, and v5 package boundaries are validated. Peer
  heartbeat and per-peer ACL remain future work.

### 11.2 What happens when a peer is unreachable

Currently: node logs an error and retries on next sync interval (default 1h). **No active heartbeat.**

Risk: Silent failure — if CERBERUS goes down mid-night, PYTHIA won't know for 1–2 hours.

### 11.3 Required: Peer heartbeat detection

Implement lightweight heartbeat check in `mnemos/api/routes/federation.py`:

```python
async def check_peer_health(peer_url: str, token: str, timeout: int = 5) -> bool:
    """Return True if peer is responsive, False otherwise."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{peer_url}/health",
                headers={"Authorization": f"Bearer {token}"}
            )
            return response.status_code == 200
    except Exception:
        return False
```

Wire into the sync loop:

```python
# In FederationManager.sync_all()
if not await check_peer_health(peer.url, peer.token):
    logger.warning(f"peer {peer.name} unreachable for 1h; escalate")
    # Alert + metric increment
```

---

## 12. Pre-flight checklist for any production change

Use this checklist before **any** deployment, upgrade, or migration on PYTHIA or CERBERUS:

- [ ] **Version state:** Confirm current version on each prod node
  ```bash
  curl -H "Authorization: Bearer $TOKEN" \
    http://192.168.207.67:5002/health | jq .version
  ```

- [ ] **Replication lag:** Verify replication is current (<30s lag)
  ```bash
  psql -U postgres -d mnemos -c \
    "SELECT slot_name, restart_lsn, confirmed_flush_lsn FROM pg_replication_slots;"
  ```

- [ ] **Pre-change backup:** Take explicit pg_dump to NFS
  ```bash
  pg_dump -U postgres mnemos | gzip > \
    /mnt/argonas/backups/mnemos/pre-<version>-<date>.sql.gz
  ```

- [ ] **Rollback path documented:** Write down exact git sha/tag to revert to
  ```bash
  # Save for rollback:
  ROLLBACK_TAG=v3.5.0
  ```

- [ ] **Migration path selected:** For Docker existing volumes, confirm
  `postgres-upgrade` completed successfully; for bare metal, run the
  migration manually with `ON_ERROR_STOP=1`.
  ```bash
  docker compose ps postgres-upgrade
  ```

- [ ] **LB has healthy peer:** Verify the OTHER prod node is healthy and receiving traffic
  ```bash
  curl -H "Authorization: Bearer $TOKEN" \
    http://192.168.207.96:5003/health
  ```

- [ ] **Notification sent:** Inform any on-call + relevant teams (Slack, Signal, etc. — currently manual)

- [ ] **Scheduled outside peak:** Confirm no major known user activity (check calendar, ask stakeholders)
  - Typical low-activity windows: 02:00–06:00 UTC, weekend

---

## 13. Deployment artifacts (planned)

The following three shell scripts codify operational patterns and reduce manual error. **Current status:** Not yet written (specifications follow).

### 13.1 `scripts/ops/version_check.sh`

**Purpose:** Detect version drift between actual and declared.

**Inputs:** PYTHIA_DECLARED, CERBERUS_DECLARED (environment vars or config file).

**Outputs:** Logs mismatches to syslog `mnemos-drift` tag.

**Success criteria:**
- [ ] Runs without errors
- [ ] Detects if prod node is >1 version behind declared target
- [ ] Detects if staging node is ahead of prod by >2 weeks
- [ ] Can be run by non-root user (reads `/health`, no privileged operations)
- [ ] CI test: run against mock `/health` responses, verify alert detection

### 13.2 `scripts/ops/migration_apply.sh`

**Purpose:** Safe migration wrapper (backup → apply → smoke → rollback on fail).

**Inputs:** migration file path, target database name.

**Outputs:** Success/failure message, rollback point saved.

**Sequence:**
1. `pg_dump` to `/mnt/argonas/backups/mnemos/pre-<ts>.sql.gz`
2. Run migration via `sudo -u postgres psql -f <file>`
3. Run smoke test: `SELECT 1; SELECT COUNT(*) FROM memories;`
4. If smoke fails: `gunzip < backup | psql` → restore
5. Log result to syslog `mnemos-migration`

**Success criteria:**
- [ ] Idempotent (can be re-run without additional data loss)
- [ ] Rollback is automatic on smoke failure
- [ ] Works for all migration file formats in `db/migrations_*.sql`
- [ ] Confirms backup viability before applying

### 13.3 `scripts/ops/blue_green_deploy.sh`

**Purpose:** Codifies blue-green upgrade of a single prod node (PYTHIA or CERBERUS).

**Inputs:** node IP, new version tag, LB IP, token.

**Outputs:** Upgrade success/failure, rejoin to LB.

**Sequence:**
1. Drain node from LB (via ssh to ARGONAS nginx)
2. Take pre-upgrade backup
3. Upgrade MNEMOS code + deps (git checkout, pip install)
4. Run migrations (via `migration_apply.sh`)
5. Restart systemd unit or docker container
6. Smoke test (curl `/health`)
7. Rejoin LB
8. Wait 2 minutes, verify traffic flowing

**Success criteria:**
- [ ] Zero manual intervention once started
- [ ] Automatic rollback on smoke failure
- [ ] Idempotent (safe to re-run if partial failure)
- [ ] Works for both systemd (PYTHIA) and docker (CERBERUS) deployments
- [ ] End-to-end test: upgrade PROTEUS successfully, then PYTHIA in lower env

---

## 14. Open questions and known gaps

| Item | Status | Impact | Owner | Target |
|---|---|---|---|---|
| ARGONAS nginx config location | TBD (permission rate-limit) | Can't read LB config or verify drain setup | ops | Next cycle |
| Health-check probe interval/timeout | TBD | Don't know if nginx can auto-detect backend failure | ops | Next cycle |
| CERBERUS port 5002 v3.1.0 cleanup | ⏳ Planned | Stale container running, wastes VRAM | ops | v3.5 quarterly pass |
| Restore drill | ✅ Dev↔prod drill documented and run | Repeat quarterly, not a one-time substitute for backup monitoring | ops | quarterly |
| Slack/Signal alerting | ⏳ Not wired | On-call relies on manual checking | ops | v3.5 |
| Federation peer heartbeat | ⏳ No detection | Silent failure if peer unreachable >1h | dev | v3.5 |
| PROTEUS deployment | ✅ Used for v3.4.1 restore/schema drills | Keep as staging proving ground | ops+dev | ongoing |
| Version check script | ⏳ Not written | Drift detection manual-only | ops | v3.5 |
| Migration wrapper script | ⏳ Not written | No safe migration automation | ops | v3.5 |
| Blue-green deploy script | ⏳ Not written | Prod upgrades manual-only | ops | v3.5 |

---

## 15. Cross-references

- **Feature roadmap:** `docs/V3_5_CHARTER.md`, `docs/V3_6_CHARTER.md`, `docs/V4_PLAN.md` (historical planning docs)
- **Architecture:** `README.md`, `docs/MEMORY_ARCHITECTURE.md`, `docs/SPECIFICATION.md`
- **API docs:** `API_DOCUMENTATION.md` (root) + the live FastAPI OpenAPI spec at `/docs` on a running instance
- **Database:** `db/migrations_*.sql` and `db/migrations_sqlite/` (schema changes), `mnemos/installer/db.py` (canonical migration order)
- **Observability:** `mnemos/core/observability.py` (request-ID middleware, Prometheus, OTEL)
- **Federation:** `mnemos/api/routes/federation.py` (peer sync logic)

---

## Appendix: Command quick-reference

### Health and status

```bash
# PYTHIA health
curl -H "Authorization: Bearer $TOKEN" http://192.168.207.67:5002/health | jq .

# CERBERUS prod health
curl -H "Authorization: Bearer $TOKEN" http://192.168.207.96:5003/health | jq .

# Replication lag
psql -U postgres -d mnemos -c \
  "SELECT slot_name, restart_lsn FROM pg_replication_slots;"
```

### Containers (CERBERUS)

```bash
# List all containers
ssh jasonperlow@192.168.207.96 "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'"

# Logs for a container
ssh jasonperlow@192.168.207.96 "docker logs -f <container-name>"

# Stop a container
ssh jasonperlow@192.168.207.96 "docker stop <container-name>"
```

### Database operations

```bash
# Manual backup
pg_dump -U postgres mnemos | gzip > backup-$(date +%Y%m%d).sql.gz

# List backups on ARGONAS
ssh jasonperlow@192.168.207.101 "ls -lh /mnt/argonas/backups/mnemos/"

# Restore from backup
gunzip < backup.sql.gz | psql -U postgres -d mnemos

# Run a migration
sudo -u postgres psql -d mnemos -v ON_ERROR_STOP=1 \
  -f db/migrations_v3_5_trigger_same_memory_parent.sql
```

### Load balancer (ARGONAS)

```bash
# Reload nginx config
ssh root@192.168.207.101 "nginx -s reload"

# Check nginx status
ssh root@192.168.207.101 "systemctl status nginx"

# View nginx error log
ssh root@192.168.207.101 "tail -f /var/log/nginx/error.log"
```

### Systemd (PYTHIA)

```bash
# Restart MNEMOS
sudo systemctl restart mnemos

# Check status
sudo systemctl status mnemos

# View journal
sudo journalctl -u mnemos -f
```

---

**Document version:** 1.0  
**Last updated:** 2026-05-08  
**Maintained by:** Operations team  
**Status:** Active, current for v5.0.1 production line

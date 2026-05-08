# MNEMOS HA Automation: Patroni + etcd3 + HAProxy

Status: v5.3.0 design and configuration artifacts. The automation is designed
for the PYTHIA/CERBERUS pg16 + pgvector deployment and is not yet exercised in
CI.

## Decision

MNEMOS standardizes on Patroni for automatic PostgreSQL failover rather than
repmgr. Patroni's distributed-configuration-store model fits the existing
Podman container operations better than repmgr's daemon-per-node mesh: each
PostgreSQL container can be wrapped by one Patroni process, while failover
authority lives in a small quorum service outside PostgreSQL itself.

The current manual topology remains the source input for this design:

| Role | Host | Address | PostgreSQL |
|---|---|---|---|
| Primary | PYTHIA | `192.168.207.67` | pg16 + pgvector on host port `5433` |
| Standby | CERBERUS | `192.168.207.96` | pg16 + pgvector on host port `5434` |
| Witness | PROTEUS | `192.168.207.25` | etcd3 quorum witness only |

The current physical replication slot is `cerberus_standby`. Patroni can keep
physical slots enabled and will manage member slots after hand-off.

## DCS Rationale

Use etcd3 as a three-node witness cluster:

- PYTHIA runs PostgreSQL + Patroni + an etcd3 member.
- CERBERUS runs PostgreSQL + Patroni + an etcd3 member.
- PROTEUS runs only an etcd3 member as quorum witness.

Do not use Patroni's built-in raft for this two-database-node deployment. A
two-node raft group has no majority after either node is lost, so it cannot
reliably distinguish "peer failed" from "I am partitioned." Adding PROTEUS as
an etcd3 witness gives the control plane a three-member majority while keeping
the data plane limited to PYTHIA and CERBERUS.

The DCS endpoints used by the shipped configs are:

```text
192.168.207.67:2379   # PYTHIA etcd3 client
192.168.207.96:2379   # CERBERUS etcd3 client
192.168.207.25:2379   # PROTEUS etcd3 client
```

## Patroni Configuration

Concrete configs are in:

- `ops/patroni/patroni-pythia.yml`
- `ops/patroni/patroni-cerberus.yml`

Both nodes share these cluster-level fields:

| Section | Purpose |
|---|---|
| `scope` | `mnemos-pg16`, the Patroni cluster name stored in etcd3. |
| `namespace` | `/mnemos/patroni/`, isolating MNEMOS keys from other etcd users. |
| `restapi` | Patroni health API on `:8008`; HAProxy checks `/primary` and `/replica`. |
| `etcd3` | Three endpoints: PYTHIA, CERBERUS, PROTEUS. |
| `bootstrap.dcs` | Cluster TTL, failover timing, pg16 replication parameters, physical slots, and async replication posture. |
| `bootstrap.initdb` | UTF-8 initdb defaults for a fresh cluster; not used when adopting existing data. |
| `bootstrap.users` | Initial local database roles for new clusters. Replace sample passwords before production use. |
| `postgresql` | Node-specific listen/connect addresses, data directory, pg16 binary directory, authentication users, and basebackup replica method. |
| `watchdog` | Disabled until a hardware/software watchdog is explicitly configured and tested. |
| `tags` | Load-balancing and failover hints; both database nodes are eligible for failover and read traffic. |

Node-specific values:

| Node | Patroni name | PostgreSQL listen/connect | REST connect |
|---|---|---|---|
| PYTHIA | `pythia` | `0.0.0.0:5433` / `192.168.207.67:5433` | `192.168.207.67:8008` |
| CERBERUS | `cerberus` | `0.0.0.0:5434` / `192.168.207.96:5434` | `192.168.207.96:8008` |

The local PostgreSQL ports intentionally preserve the current deployment:
PYTHIA stays on `5433`, CERBERUS stays on `5434`, and pgvector remains part of
the PostgreSQL image.

## Primary Discovery

MNEMOS clients should not connect directly to PYTHIA or CERBERUS. Route
database traffic through HAProxy on a stable VIP or stable host address:

| Endpoint | Port | Meaning | HAProxy health check |
|---|---:|---|---|
| `mnemos-postgres-vip:5000` | `5000` | Primary read/write PostgreSQL endpoint | `GET /primary` on each Patroni REST API |
| `mnemos-postgres-vip:5001` | `5001` | Read-only replica PostgreSQL endpoint | `GET /replica` on each Patroni REST API |

The concrete HAProxy config is `ops/patroni/haproxy.cfg`. It forwards
PostgreSQL TCP traffic to `192.168.207.67:5433` or `192.168.207.96:5434`, but
health-checks the Patroni REST API on `:8008`. A promoted standby begins
receiving writes through port `5000` only after Patroni reports it as
`/primary`.

MNEMOS application settings after the cutover should use the VIP:

```env
PG_HOST=mnemos-postgres-vip
PG_PORT=5000
PG_DATABASE=mnemos
PG_USER=mnemos_user
PG_PASSWORD=replace-with-the-app-password
```

Read-only tooling can use `PG_PORT=5001` when stale replica reads are
acceptable.

## Migration Path

Goal: move from the current manual PYTHIA-primary/CERBERUS-standby setup to a
Patroni-managed cluster without data loss.

1. Prepare etcd3 quorum.
   Start the etcd3 member on PYTHIA, CERBERUS, and PROTEUS. Verify a majority
   before touching PostgreSQL:

   ```bash
   ETCDCTL_API=3 etcdctl \
     --endpoints=http://192.168.207.67:2379,http://192.168.207.96:2379,http://192.168.207.25:2379 \
     endpoint status --write-out=table
   ```

2. Freeze MNEMOS writes.
   Stop or drain MNEMOS services that write to PYTHIA. This preserves a clean
   final LSN before hand-off:

   ```bash
   ssh jasonperlow@192.168.207.67 "podman stop mnemos-v3x-podman_mnemos_1 mnemos-v3x-podman_mnemos-mcp-http_1"
   ```

3. Confirm CERBERUS has replayed all WAL from PYTHIA.

   ```bash
   ssh jasonperlow@192.168.207.67 "podman exec mnemos-v3x-podman_postgres_1 \
       psql -U mnemos_user -d mnemos -tAc 'SELECT sent_lsn FROM pg_stat_replication;'"
   ssh jasonperlow@192.168.207.96 "podman exec mnemos-standby \
       psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -tAc 'SELECT pg_last_wal_replay_lsn();'"
   ```

   Continue only when the LSNs match. This is the no-data-loss gate.

4. Take a final physical backup of the primary before changing supervisors.

   ```bash
   ssh jasonperlow@192.168.207.67 "podman exec mnemos-v3x-podman_postgres_1 \
       pg_basebackup -h 127.0.0.1 -p 5432 -U replicator \
       -D /tmp/mnemos-pythia-final-basebackup -Fp -Xs -P"
   ```

   Store that backup off the container host before proceeding.

5. Hand PYTHIA's existing PGDATA to Patroni.
   Stop only the old PostgreSQL container supervisor, mount the same pg16 data
   volume into the Patroni-wrapped pgvector container, and start
   `patroni-pythia.yml`. Because the DCS is empty and PYTHIA has the existing
   primary data directory, Patroni should initialize the DCS with PYTHIA as
   leader rather than running `initdb`.

6. Rebuild CERBERUS under Patroni with `pg_basebackup`.
   Use a fresh CERBERUS pg16 data volume, clone from PYTHIA, and preserve the
   existing slot name during the transition:

   ```bash
   ssh jasonperlow@192.168.207.96 "podman run --rm \
       -v mnemos-patroni-cerberus-pgdata:/var/lib/postgresql/data \
       -e PGPASSWORD=replace-with-replicator-password \
       docker.io/pgvector/pgvector:pg16 \
       pg_basebackup \
           -h 192.168.207.67 -p 5433 -U replicator \
           -D /var/lib/postgresql/data \
           -Fp -Xs -P -R --slot=cerberus_standby --create-slot"
   ```

   Then start CERBERUS with `patroni-cerberus.yml`. Patroni will rewrite
   recovery settings as needed and keep the node following the current leader.

7. Verify Patroni state.

   ```bash
   curl -fsS http://192.168.207.67:8008/primary
   curl -fsS http://192.168.207.96:8008/replica
   ```

   Also verify PostgreSQL recovery status:

   ```sql
   SELECT pg_is_in_recovery();
   ```

   PYTHIA should return `false`; CERBERUS should return `true`.

8. Put HAProxy/VIP in front of the cluster.
   Start HAProxy with `ops/patroni/haproxy.cfg`, point MNEMOS at port `5000`,
   and restart MNEMOS services so new asyncpg pools use the stable endpoint.

9. Unfreeze writes.
   Run an application smoke test through `mnemos-postgres-vip:5000`, then keep
   the manual failover runbook as the break-glass fallback only.

## Failure Modes

| Scenario | DCS quorum | Expected Patroni behavior | Client impact | Operator action |
|---|---|---|---|---|
| PYTHIA down | CERBERUS + PROTEUS retain majority | CERBERUS promotes if its replay lag is within `maximum_lag_on_failover`; HAProxy moves port `5000` to CERBERUS. | Short write outage during promotion; reads may continue after CERBERUS is healthy. | Repair PYTHIA and reinitialize it as a replica from CERBERUS before rejoining. |
| CERBERUS down | PYTHIA + PROTEUS retain majority | PYTHIA remains leader; no failover. | Writes continue on port `5000`; read-only port `5001` may have no backend. | Repair/reinitialize CERBERUS. Watch WAL retention and replication slot growth. |
| PYTHIA partitioned from CERBERUS but PYTHIA can still reach PROTEUS | PYTHIA + PROTEUS retain majority | PYTHIA keeps the leader lock; CERBERUS remains or becomes a non-leader without quorum. | Writes continue through PYTHIA if HAProxy can reach it. | Repair network; confirm CERBERUS follows after partition heals. |
| PYTHIA isolated from both CERBERUS and PROTEUS | CERBERUS + PROTEUS retain majority | PYTHIA loses/ cannot renew leader lock and must stop accepting writes; CERBERUS promotes. | HAProxy should remove PYTHIA and route writes to CERBERUS after promotion. | Fence old PYTHIA until it can be rebuilt as a replica. |
| Network split, both database nodes up, no side has quorum | No majority | No node may safely acquire or renew leadership; Patroni prevents unsafe promotion. | Write endpoint unavailable; avoids split-brain. | Restore etcd connectivity or deliberately execute break-glass manual recovery. |
| PROTEUS witness loss only | PYTHIA + CERBERUS retain majority | Current leader remains valid; failover can still occur while both DB nodes are connected. | No immediate impact. | Restore PROTEUS quickly; a later DB-node failure would remove quorum. |
| Any two etcd3 members lost | No majority | Patroni cannot make safe leadership changes. Existing leader may step down when TTL cannot be renewed. | Write outage likely. | Restore etcd majority first, then recover PostgreSQL nodes. |
| HAProxy node/VIP down | Patroni state unchanged | Database roles do not change. | MNEMOS cannot reach the stable endpoint even if PostgreSQL is healthy. | Move VIP/start backup HAProxy or point clients temporarily to the current primary as break-glass. |

## Break-Glass Boundary

The manual procedures in `docs/STREAMING_REPLICATION.md` remain valid as a
fallback when the DCS or HAProxy layer is unavailable. They should be treated
as break-glass operations: fence the old primary, promote exactly one standby,
and rebuild the losing node before it can accept writes again.

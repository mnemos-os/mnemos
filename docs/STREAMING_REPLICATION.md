# PostgreSQL Streaming Replication Runbook

## v5.3 automatic failover

v5.3.0 adds a Patroni-managed HA design for the deployed PYTHIA/CERBERUS
pg16 + pgvector topology. Use `docs/HA_AUTOMATION.md` for the automatic
failover plan, etcd3 quorum rationale, Patroni configs, HAProxy ports
`5000`/`5001`, and migration path from the current manual replica.

The manual procedures in this runbook remain the fallback/break-glass path.
Use them only when Patroni, etcd3 quorum, or HAProxy is unavailable, and keep
the same split-brain rule: promote exactly one standby and fence the old
primary before it can accept writes again.

This runbook is the single-site HA path for MNEMOS. Use it when the MNEMOS
nodes are in the same LAN, rack, datacenter, or low-latency private network.
The MNEMOS app talks to one PostgreSQL primary endpoint. Standbys continuously
receive WAL from that primary and stay read-only until promoted.

Federation is not the single-site HA mechanism. Keep federation for genuinely
remote data flows: multi-site deployments, multi-org curated feeds, developer
laptop replicas with intermittent connectivity, and v4 SQLite-backed edge
profiles.

References:

- PostgreSQL streaming replication and standby setup:
  <https://www.postgresql.org/docs/current/warm-standby.html>
- `pg_basebackup`: <https://www.postgresql.org/docs/current/app-pgbasebackup.html>
- Patroni automatic failover: <https://patroni.readthedocs.io/en/latest/>
- repmgr: <https://www.repmgr.org/docs/current/>

## Topology

Use one primary and one or more standbys:

```text
MNEMOS app -> HAProxy/PgBouncer/write endpoint -> PostgreSQL primary
                                           \-> PostgreSQL standby 1
                                           \-> PostgreSQL standby N
```

The app should not be configured with multiple writable database endpoints.
Failover moves the write endpoint to the promoted standby.

## Primary Setup

Create a replication user on the primary:

```sql
CREATE ROLE repl WITH REPLICATION LOGIN PASSWORD 'replace-with-a-long-secret';
```

Set replication parameters in the primary `postgresql.conf`:

```conf
listen_addresses = '*'
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
hot_standby = on

# Optional but recommended for catch-up safety if a standby is briefly offline.
wal_keep_size = '1GB'

# Optional WAL archive used by restore_command on standbys.
archive_mode = on
archive_command = 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
```

Allow each standby to connect in `pg_hba.conf`:

```conf
# TYPE  DATABASE     USER  ADDRESS            METHOD
host    replication  repl  192.168.10.0/24    scram-sha-256
```

Reload or restart PostgreSQL after changing config:

```bash
pg_ctl reload -D "$PGDATA"
```

If `wal_level`, `max_wal_senders`, or `max_replication_slots` changed, restart
the primary during a maintenance window.

## Standby Bootstrap

Stop PostgreSQL on the standby and clear the target data directory:

```bash
systemctl stop postgresql
rm -rf "$PGDATA"/*
```

Run `pg_basebackup` from the standby host:

```bash
PGPASSWORD='replace-with-a-long-secret' pg_basebackup \
  -h primary-db.example.lan \
  -p 5432 \
  -U repl \
  -D "$PGDATA" \
  -Fp \
  -Xs \
  -P \
  -R
```

`-R` writes standby connection settings and creates `standby.signal` for modern
PostgreSQL versions. If you prefer to manage those settings manually, create
`$PGDATA/standby.signal` and add this to the standby config:

```conf
primary_conninfo = 'host=primary-db.example.lan port=5432 user=repl password=replace-with-a-long-secret application_name=standby-1'
restore_command = 'cp /var/lib/postgresql/wal_archive/%f %p'
hot_standby = on
```

Store the replication password in `.pgpass` instead of `primary_conninfo` if
your operational policy forbids passwords in PostgreSQL config files:

```conf
primary-db.example.lan:5432:replication:repl:replace-with-a-long-secret
```

Start the standby:

```bash
systemctl start postgresql
```

## Verification

On the primary:

```sql
SELECT application_name, state, sync_state, sent_lsn, write_lsn, flush_lsn, replay_lsn
FROM pg_stat_replication;
```

On a standby:

```sql
SELECT pg_is_in_recovery();
SELECT now() - pg_last_xact_replay_timestamp() AS replay_delay;
```

`pg_is_in_recovery()` should return `true` on standbys.

## Failover (manual fallback / break-glass)

Manual failover is promotion of one standby:

```bash
pg_ctl promote -D "$PGDATA"
```

or from SQL on the standby:

```sql
SELECT pg_promote();
```

After promotion, the old primary must not keep accepting writes. Stop it,
fence it, or rebuild it as a standby from the new primary before returning it
to service.

For automatic failover deployments, v5.3.0 prescribes Patroni with an etcd3
quorum witness cluster. See `docs/HA_AUTOMATION.md`. The manual promotion
steps below are retained as the fallback path when automation is unavailable.

## Reconnecting MNEMOS

Point MNEMOS at a stable write endpoint rather than a node hostname:

```env
PG_HOST=mnemos-postgres-writer.example.lan
PG_PORT=5432
PG_DATABASE=mnemos
PG_USER=mnemos
PG_PASSWORD=replace-with-the-app-password
```

Recommended patterns:

- HAProxy with a primary health check that routes writes only to the current
  primary.
- PgBouncer behind a virtual IP or HAProxy endpoint.
- A Patroni-managed HAProxy/virtual-IP pattern if Patroni owns failover.

When a standby is promoted, update the writer endpoint first, then restart or
reload MNEMOS processes so new asyncpg connections target the promoted primary.
Existing connections to the failed primary will error and should be allowed to
reconnect through the stable endpoint.

## Patroni Note

Patroni is a common automatic-failover layer for PostgreSQL HA. It uses a
distributed configuration store such as etcd, Consul, ZooKeeper, Kubernetes, or
its built-in raft support to coordinate leader state, then manages PostgreSQL
promotion and following behavior. If you use Patroni, keep MNEMOS pointed at
the Patroni/HAProxy writer endpoint, not at individual PostgreSQL nodes.

---

## Live deployment: PYTHIA primary → CERBERUS standby

This section is the operator runbook for the **deployed fleet** as of v5.0.1.
The sections above describe the generic pattern; this section names the actual
hosts, ports, slot names, and commands.

### Topology snapshot

| Role | Host | Container | PG version | Listen | Application identifier |
|---|---|---|---|---|---|
| Primary | PYTHIA `192.168.207.67` | `mnemos-v3x-podman_postgres_1` | pg16 (pgvector) | host port `5433` → container `5432` | (writes from `mnemos-v3x-podman_mnemos_1`) |
| Standby | CERBERUS `192.168.207.96` | `mnemos-standby` (host network) | pg16 (pgvector) | `5434` on the host | `walreceiver` connecting from `10.89.0.105` |

Replication is **async streaming** with a physical slot named
`cerberus_standby` on the primary. The standby connects as PostgreSQL role
`replicator` (password lives in the standby's `postgresql.auto.conf`).

The standby has its own data volume (`mnemos-standby-pg16-data`) and runs
postgres with `-c port=5434` so it does not collide with CERBERUS's separate
`mnemos-prod-pg` (pg17 on `5433`, unrelated to MNEMOS HA).

### Health checks (run any time)

On the **primary** (PYTHIA):

```bash
ssh jasonperlow@192.168.207.67 "podman exec mnemos-v3x-podman_postgres_1 \
    psql -U mnemos_user -d mnemos -c '
SELECT client_addr, application_name, state, sync_state,
       sent_lsn, replay_lsn, write_lag, flush_lag, replay_lag
FROM pg_stat_replication;'"
```

`state=streaming` and replay_lag well under one second is the steady state.
`sync_state=async` is expected — we do not block primary writes on standby
acknowledgement.

On the **standby** (CERBERUS):

```bash
ssh jasonperlow@192.168.207.96 "podman exec mnemos-standby \
    psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -tAc '
SELECT pg_is_in_recovery(),
       pg_last_wal_replay_lsn(),
       now() - pg_last_xact_replay_timestamp() AS replay_delay;'"
```

`pg_is_in_recovery()=t` and `replay_delay < 5 seconds` is healthy.

A row-count parity check is the cheapest end-to-end signal:

```bash
for h in 192.168.207.67 192.168.207.96; do
  ssh -n jasonperlow@$h "podman exec \
    \$(podman ps --format '{{.Names}}' | grep -E 'postgres_1|mnemos-standby' | head -1) \
    psql -U mnemos_user -d mnemos $([ $h = 192.168.207.96 ] && echo '-p 5434 -h 127.0.0.1') \
    -tAc 'SELECT count(*) FROM memories;'"
done
```

The two counts should match to within a few seconds of replication delay.

### Planned failover (PYTHIA → CERBERUS, manual break-glass, no data loss)

Use this when PYTHIA needs maintenance and you can drain writes cleanly.

1. **Stop the MNEMOS service on PYTHIA** so no new writes hit the primary:

   ```bash
   ssh jasonperlow@192.168.207.67 "podman stop mnemos-v3x-podman_mnemos_1 \
       mnemos-v3x-podman_mnemos-mcp-http_1"
   ```

2. **Wait for the standby to drain WAL**. Confirm replay_lsn on the standby
   matches sent_lsn from `pg_stat_replication` on the primary:

   ```bash
   ssh jasonperlow@192.168.207.67 "podman exec mnemos-v3x-podman_postgres_1 \
       psql -U mnemos_user -d mnemos -tAc \
       'SELECT sent_lsn FROM pg_stat_replication;'"
   ssh jasonperlow@192.168.207.96 "podman exec mnemos-standby \
       psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -tAc \
       'SELECT pg_last_wal_replay_lsn();'"
   ```

   If they match, the standby has every byte the primary sent.

3. **Promote the standby**:

   ```bash
   ssh jasonperlow@192.168.207.96 "podman exec mnemos-standby \
       psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -c 'SELECT pg_promote();'"
   ```

   Verify the standby is now writable:

   ```bash
   ssh jasonperlow@192.168.207.96 "podman exec mnemos-standby \
       psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -tAc 'SELECT pg_is_in_recovery();'"
   # Should now return f.
   ```

4. **Stop the old primary** so it cannot accept writes:

   ```bash
   ssh jasonperlow@192.168.207.67 "podman stop mnemos-v3x-podman_postgres_1"
   ```

5. **Repoint MNEMOS at the new primary**. The minimum-impact approach is to
   reconfigure the existing CERBERUS mnemos container to use the promoted
   pg16 standby on `:5434` for its `DATABASE_URL`, and stop pushing reads at
   the old PYTHIA. This requires editing the env file the container was
   launched with:

   ```bash
   ssh jasonperlow@192.168.207.96 "
   cat > /tmp/cerberus_runtime.env << EOF
   $(podman inspect mnemos-cerberus | python3 -c 'import sys,json; d=json.load(sys.stdin)[0]; [print(e) for e in d[\"Config\"][\"Env\"] if not e.startswith((\"PATH\",\"PYTHON_\",\"GPG_\",\"LANG\",\"container\",\"HOME\",\"PWD\",\"HOSTNAME\"))]' | sed 's/PG_PORT=.*/PG_PORT=5434/;s|DATABASE_URL=.*|DATABASE_URL=postgresql://mnemos_user:mnemos_local@127.0.0.1:5434/mnemos|')
   EOF
   podman stop mnemos-cerberus && podman rm mnemos-cerberus
   podman run -d --name mnemos-cerberus --network host --restart unless-stopped \
       --env-file /tmp/cerberus_runtime.env localhost/mnemos-os:5.0.1-full-hot \
       mnemos serve
   "
   ```

   PYTHIA's mnemos can then be repointed at CERBERUS:5434 once the network path
   is open (port-forward, WireGuard, or simply leave PYTHIA's mnemos stopped if
   the failover is permanent).

6. **Smoke check** the new primary serves writes:

   ```bash
   ssh jasonperlow@192.168.207.96 "curl -s -H 'Authorization: Bearer \$MNEMOS_TOKEN' \
       -X POST http://localhost:5002/v1/memories \
       -H 'Content-Type: application/json' \
       -d '{\"content\":\"failover smoke test\",\"category\":\"infrastructure\"}'"
   ```

### Unplanned failover (manual break-glass, PYTHIA is dead)

When PYTHIA is unreachable and you must promote without draining:

1. **Confirm PYTHIA is actually down** (not just slow):
   `nc -zv 192.168.207.67 5433` and `ping -c3 192.168.207.67`. A flapping primary
   that comes back after promotion creates a split-brain — be sure.

2. **Promote CERBERUS standby immediately** (step 3 above).

3. **Accept the data loss**. Async replication means up to a few seconds of
   committed-but-not-replayed writes may be lost. The replay_delay observed at
   the time of failure is your worst-case data-loss window.

4. **Fence PYTHIA** before it can rejoin: stop its postgres container if you can
   reach the host, or block port `5433` at the network layer until you can
   rebuild it as a standby.

### Failback: rebuild PYTHIA as a standby of CERBERUS

After the new primary on CERBERUS has been validated, rebuild PYTHIA as a
fresh standby pointing at CERBERUS:

```bash
ssh jasonperlow@192.168.207.67 "
podman volume rm mnemos-v3x-podman_pgdata 2>/dev/null
podman run --rm \
    -v mnemos-v3x-podman_pgdata:/var/lib/postgresql/data \
    -e PGPASSWORD=replpwd \
    docker.io/pgvector/pgvector:pg16 \
    pg_basebackup \
        -h 192.168.207.96 -p 5434 -U replicator \
        -D /var/lib/postgresql/data \
        -Fp -Xs -P -R --slot=pythia_standby --create-slot
podman start mnemos-v3x-podman_postgres_1
"
```

The `-R` flag writes `standby.signal` and the `primary_conninfo` line
automatically. Verify with the same health-check queries — `pg_is_in_recovery()=t`
on the (now standby) PYTHIA, `pg_stat_replication` shows the new walreceiver
on the (now primary) CERBERUS.

When you're ready to flip back to PYTHIA-primary, the procedure is symmetric:
stop MNEMOS writes, wait for replication to drain, `pg_promote()` PYTHIA,
fence CERBERUS, repoint clients.

### Things that have bitten us

- **Forgetting `port=5434`.** The standby on CERBERUS shares the host network
  with `mnemos-prod-pg` on `:5433`. Every `psql` command must pin
  `-p 5434 -h 127.0.0.1` or it lands on the wrong instance and reports
  misleading state.
- **Replication slot left behind.** If a standby is destroyed without
  dropping its slot, the primary keeps WAL forever and eventually fills the
  disk. After a failback, run
  `SELECT pg_drop_replication_slot('cerberus_standby');` on the *new* primary
  if the old slot is no longer in use, then create a fresh slot for the
  rebuilt standby.
- **`mnemos-standby` postgres user is `mnemos_user`, not `replicator`.**
  `replicator` is the *replication role* (used in `primary_conninfo`).
  Application reads against the standby use `mnemos_user` with the same
  password as the primary — `pg_basebackup` clones the role table verbatim.
- **CERBERUS's `mnemos-cerberus` mnemos service currently points at the local
  pg17 (`mnemos-prod-pg` on `:5433`)**, *not* at the pg16 standby on `:5434`.
  This is **intentional**: CERBERUS is both an HA peer for PYTHIA (via the
  pg16 streaming replica on `:5434`) AND its own writable federation peer
  with its own dataset (the pg17 instance). The two databases are separate
  by design. CERBERUS's mnemos service writes federation-peer data to its
  own pg17; PYTHIA-replica data lives on the pg16 standby and is read-only
  until promoted.
  
  Implication for failover: when PYTHIA dies, the pg16 standby is what gets
  promoted to be the new MNEMOS-primary, NOT the pg17 instance. Repointing
  the existing `mnemos-cerberus` container at `:5434` would lose access to
  CERBERUS's own federation-peer dataset. Either (a) start a NEW container
  pointed at `:5434` for the promoted-primary role and keep the original
  pg17 service for federation peering, or (b) accept that CERBERUS's pg17
  data is unreachable until the rebuilt PYTHIA standby comes back. Pick
  based on the outage's expected duration. The runbook in §"Planned failover"
  above describes (a).
- **No live connection-string swap.** asyncpg pool config is read once at
  process start; changing `PG_PORT`/`DATABASE_URL` requires container
  restart. Plan the failover window accordingly.
- **No automatic split-brain protection.** This is a manual/single-promoter
  setup. Use Patroni or repmgr if you need quorum-based automation.

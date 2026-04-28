# PostgreSQL Streaming Replication Runbook

This runbook is the single-site HA path for MNEMOS. Use it when the MNEMOS
nodes are in the same LAN, rack, datacenter, or low-latency private network.
The MNEMOS app talks to one PostgreSQL primary endpoint. Standbys continuously
receive WAL from that primary and stay read-only until promoted.

Federation is not the single-site HA mechanism. Keep federation for genuinely
remote data flows: multi-site deployments, multi-org curated feeds, developer
laptop replicas with intermittent connectivity, and planned v4 SQLite-based
local-replica profiles.

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

## Failover

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

For automatic failover deployments, use a PostgreSQL HA manager such as
Patroni or repmgr. MNEMOS does not prescribe one; choose based on your
operational environment, quorum model, and existing automation.

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

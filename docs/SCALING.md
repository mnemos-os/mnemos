# MNEMOS Scaling

MNEMOS defaults to one Uvicorn worker. The `edge` and `dev` profiles use
SQLite plus in-process coordination; the `server` profile should use Redis when
you raise the worker count.

Multi-worker deployments are supported when shared rate-limit and resilience
state is backed by Redis. Without Redis, each worker keeps its own in-process
rate-limit, circuit-breaker, and concurrency state; MNEMOS logs a startup
warning but does not block boot.

## Single-worker default

Use the default when one API process can handle the workload:

```bash
MNEMOS_WORKERS=1
RATE_LIMIT_STORAGE_URI=memory://
```

This is the migration-safe path for edge/dev and existing single-host installs.
No Redis service, schema change, or config migration is required.

## Multi-worker pattern

For throughput, add Redis and increase workers:

```bash
RATE_LIMIT_STORAGE_URI=redis://host:6379/1
MNEMOS_WORKERS=2
```

Recommended topologies:

- **1 primary writer + N readers**: route ingest, webhook mutation, admin
  mutation, and background-worker traffic to the primary writer; route read and
  search traffic across reader pods or instances.
- **N+1 writers**: allow every API instance to accept writes when the Postgres
  pool, Redis, webhook lease settings, and downstream provider limits are sized
  for the aggregate concurrency.

Size database pools from the total process count:

```text
total_db_connections = replicas * MNEMOS_WORKERS * PG_POOL_MAX
```

Keep that total under the Postgres or PgBouncer server-side limit with headroom
for migrations, psql sessions, and maintenance tasks.

## Docker Compose

Use Redis as an additional service, or point `RATE_LIMIT_STORAGE_URI` at an
external Redis service:

```yaml
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped

  mnemos:
    environment:
      RATE_LIMIT_STORAGE_URI: redis://redis:6379/1
      MNEMOS_WORKERS: "2"
    depends_on:
      redis:
        condition: service_started
```

The standard compose file can stay single-worker. Apply this as an override
when testing or operating multi-worker mode.

## Kubernetes

Run Redis as a managed service or a dedicated in-cluster StatefulSet. Configure
MNEMOS pods with:

```yaml
env:
  - name: RATE_LIMIT_STORAGE_URI
    value: redis://redis:6379/1
  - name: MNEMOS_WORKERS
    value: "2"
```

HorizontalPodAutoscaler guidance:

- Scale on CPU, request latency, and queue depth rather than request count alone.
- Account for `replicas * MNEMOS_WORKERS * PG_POOL_MAX` before increasing HPA
  max replicas.
- Sticky sessions are not required for rate-limit or circuit-breaker correctness
  because shared state is in Redis.
- Keep startup probes tolerant of Redis/Postgres cold starts so a scale-out event
  does not churn pods before dependencies are reachable.

## PgBouncer and asyncpg

Prefer direct Postgres connections or PgBouncer session pooling for MNEMOS API
pools. PgBouncer transaction mode can conflict with asyncpg assumptions around
prepared statements and connection-local state unless the asyncpg pool is
configured for that shape, such as disabling statement caching.

Current operator notes:

- Keep `PG_POOL_MIN` low in multi-worker pods so scale-out does not stampede
  Postgres with idle connections.
- Set `PG_POOL_MAX` from aggregate worker count, not per-pod intuition.
- Test transaction-mode PgBouncer under write load before production. If it is
  required, add an explicit deployment profile that configures asyncpg for
  transaction pooling.

## Migration Story

Existing single-worker installs continue to work without changing anything.
`memory://` remains the default fallback for dev/edge profiles.

Operators who want more throughput should:

1. Add Redis.
2. Set `RATE_LIMIT_STORAGE_URI=redis://host:6379/1`.
3. Increase `MNEMOS_WORKERS` or pod replicas.
4. Re-check Postgres pool sizing and downstream provider limits.

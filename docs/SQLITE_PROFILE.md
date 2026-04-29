# SQLite Profile

The SQLite persistence backend is the MNEMOS lite profile for laptops, edge
devices, single-user development, and small offline deployments. It implements
the same persistence interface as `PostgresBackend`, but uses SQLite storage,
FTS5, JSON1-compatible JSON text, and `sqlite-vec` when the extension is
available.

## When To Use It

Use SQLite when you want a local MNEMOS node with minimal operational surface:

- Developer laptop or CI smoke tests.
- Single-user personal memory store.
- Edge profile targets such as Pi-class systems or phone-adjacent sync tools.
- Offline-first testing before promoting data to a Postgres deployment.

Use Postgres when you need multi-user hard isolation, concurrent writers across
processes, LISTEN/NOTIFY, advisory locks, pgvector indexing, streaming
replication, or production HA.

## Storage Mapping

The SQLite migration chain lives in `db/migrations_sqlite/` and mirrors the
canonical Postgres migration list.

- `UUID` -> `TEXT`
- `JSONB` -> JSON text, queried through SQLite JSON1 where needed
- `TIMESTAMPTZ` -> ISO-8601 `TEXT`
- `TEXT[]` / `UUID[]` -> JSON text arrays
- `pgvector vector(768)` -> `sqlite-vec` `vec0` when available, plus a JSON
  fallback table for portable tests
- PostgreSQL full-text search -> SQLite FTS5
- Partial unique indexes -> SQLite partial unique indexes

## Deliberate Differences

SQLite has no row-level security. The profile is single-user or
single-namespace by deployment convention; tenancy is enforced at the
application layer through the same visibility predicate used by non-RLS reads.

SQLite has no LISTEN/NOTIFY. Federation and webhook workers use polling.

SQLite has no advisory locks. The backend serializes transactions through one
connection mutex, matching SQLite's serialized-write model.

Postgres enforces webhook `status='succeeded'` as terminal through a trigger.
The SQLite profile enforces that path in `mnemos.webhooks.finalize` because the
profile does not rely on trigger functions for retry-chain safety.

## Configuration

Install the optional dependencies:

```bash
pip install "mnemos-os[sqlite]"
```

Select SQLite explicitly:

```bash
MNEMOS_PERSISTENCE_BACKEND=sqlite
MNEMOS_SQLITE_PATH=/var/lib/mnemos/mnemos.sqlite3
```

Or use URI auto-detection:

```bash
MNEMOS_PERSISTENCE_BACKEND=auto
MNEMOS_DATABASE_URL=sqlite:////var/lib/mnemos/mnemos.sqlite3
```

Postgres remains the default backend.

## Operational Notes

Keep one MNEMOS process writing to the SQLite database. WAL mode is enabled on
open, and foreign keys are enabled for every connection.

Back up the `.sqlite3` file and its WAL/shm companions together, or checkpoint
before copying. For large multi-user deployments, migrate to Postgres instead
of stretching SQLite beyond its intended profile.

# CORPUS-REVIEW-2026-04-29

## Executive Summary

Reviewed `mnemos/` at HEAD `14b8d3d` (`v4.1.1`). Inventory found 159 Python files and 47,156 Python LOC under `mnemos/`, not the 218+/77k LOC stated in the request. No critical findings surfaced, but several high-impact cross-cutting issues remain around outbound SSRF protection, transactional webhook guarantees, cancellation safety, and the SQLite/backend abstraction boundary. I skipped the four already-tracked corpus findings named in the prompt.

## Inventory

Module counts from `find mnemos -type f -name "*.py" | sort`:

| Module | Python files |
|---|---:|
| root | 2 |
| api/ | 4 |
| api/routes/ | 22 |
| cli/ | 2 |
| core/ | 13 |
| db/ | 5 |
| domain/ | 57 |
| hooks/ | 4 |
| installer/ | 8 |
| mcp/ | 9 |
| persistence/ | 6 |
| tools/ | 13 |
| webhooks/ | 12 |
| workers/ | 2 |

## Top Findings

| # | Severity | Location | Summary |
|---:|---|---|---|
| 1 | high | `mnemos/webhooks/validation.py:75`, `mnemos/webhooks/sender.py:118` | Webhook SSRF validation is DNS-rebindable because validation and connection resolution are separate. |
| 2 | high | `mnemos/api/routes/consultations.py:445`, `mnemos/api/routes/dag.py:958`, `mnemos/webhooks/outbox.py:61` | Some event-producing writes still violate the durable outbox contract. |
| 3 | high | `mnemos/domain/graeae/engine.py:658`, `mnemos/domain/graeae/engine.py:695` | Cancelled GRAEAE consultations can leak provider concurrency slots. |
| 4 | high | `mnemos/core/lifecycle.py:247`, `mnemos/core/lifecycle.py:427`, `mnemos/api/routes/sessions.py:33` | SQLite/edge backend is selected as a first-class backend, but many API routes remain Postgres-only. |
| 5 | medium | `mnemos/api/routes/morpheus.py:112`, `mnemos/api/routes/morpheus.py:144`, `mnemos/api/routes/morpheus.py:166` | MORPHEUS read endpoints expose cross-namespace telemetry to any authenticated user. |
| 6 | medium | `mnemos/core/pool.py:45`, `mnemos/api/dependencies.py:99` | Many raw asyncpg acquires bypass the configured acquire timeout. |
| 7 | medium | `mnemos/api/routes/dag.py:43` | DAG read preflight is stricter than memory read visibility, breaking group/world-readable invariants. |
| 8 | medium | `mnemos/api/routes/federation.py:126`, `mnemos/domain/federation.py:151` | Federation peer URLs only validate scheme before sending bearer tokens. |
| 9 | medium | `mnemos/core/lifecycle.py:466`, `mnemos/api/dependencies.py:26`, `mnemos/core/config.py:130` | Auth enablement ignores typed/env settings and can fail open in env-only deployments. |
| 10 | low | `mnemos/persistence/sqlite.py:610` | SQLite insert path silently ignores duplicate memory IDs but reports success. |

## Findings Detail

### 1. Webhook SSRF validation is DNS-rebindable

Severity: high  
Location: `mnemos/webhooks/validation.py:75`, `mnemos/webhooks/sender.py:118`, `mnemos/webhooks/sender.py:136`  
Blast-radius: cross-cutting outbound webhook security

`validate_webhook_url()` resolves the host and rejects private/non-routable addresses, but `_send_once()` then creates a normal `httpx.AsyncClient` and posts to the original URL. A hostile domain can resolve to a public IP during validation and then to localhost, metadata IPs, or private infrastructure during the actual connection.

Recommendation: pin the resolved address used for validation into the connection path, or route all webhook delivery through an egress proxy that enforces destination policy at connect time. Re-run private-IP checks after redirects if redirects are ever enabled; currently `follow_redirects=False` is good and should be preserved.

### 2. Event writes still bypass the durable outbox contract

Severity: high  
Location: `mnemos/api/routes/consultations.py:445`, `mnemos/api/routes/dag.py:958`, `mnemos/webhooks/outbox.py:61`  
Blast-radius: cross-cutting webhook reliability

Memory CRUD mostly uses the new backend transaction path correctly, but consultations and DAG live-merge updates still call legacy `dispatcher.dispatch()` after the data transaction has committed. If dispatch fails, the domain write remains committed and the event is lost. Separately, legacy `outbox._dispatch_on_conn()` inserts delivery rows and schedules send tasks immediately, which can race a caller transaction that has not committed yet.

Recommendation: make all event-producing writes enqueue delivery rows through the backend transaction API and return delivery IDs to schedule only after commit. Deprecate or hard-block legacy `dispatcher.dispatch()` for domain writes unless it is explicitly post-commit best-effort telemetry.

### 3. Cancelled GRAEAE consultations can leak concurrency slots

Severity: high  
Location: `mnemos/domain/graeae/engine.py:658`, `mnemos/domain/graeae/engine.py:695`, `mnemos/domain/graeae/engine.py:699`  
Blast-radius: cross-cutting availability for all non-stream GRAEAE consultations

`consult()` acquires provider concurrency slots before fan-out, then releases them only after `asyncio.gather()` returns. If the request task is cancelled while providers are in flight, the release loop is skipped. Those providers can then remain permanently saturated until process restart, causing cascading “all providers unavailable” responses.

Recommendation: wrap the fan-out in `try/finally`, cancel pending provider tasks on cancellation, and release every acquired provider exactly once. The streaming path already has stronger `finally`-based cleanup; mirror that pattern here.

### 4. SQLite/edge backend is not a full API backend

Severity: high  
Location: `mnemos/core/lifecycle.py:247`, `mnemos/core/lifecycle.py:427`, `mnemos/core/lifecycle.py:545`, `mnemos/api/routes/sessions.py:33`, `mnemos/api/routes/entities.py:48`, `mnemos/api/routes/state.py:32`  
Blast-radius: cross-cutting API/runtime compatibility

The lifecycle auto-selects SQLite for `edge` and `dev`, then sets `_pool = None`. Many routes still hard-require `_lc._pool` or use asyncpg/Postgres SQL directly. This means large parts of the HTTP API return 503 under a supported-looking SQLite profile, and workers are skipped because worker startup is gated on `_pool`.

Recommendation: either mark SQLite as a limited local-memory backend with explicit route gating, or finish routing the API through `PersistenceBackend`. Add a SQLite API smoke suite that exercises non-memory routes, not only repository parity.

### 5. MORPHEUS read endpoints leak cross-namespace telemetry

Severity: medium  
Location: `mnemos/api/routes/morpheus.py:112`, `mnemos/api/routes/morpheus.py:144`, `mnemos/api/routes/morpheus.py:166`  
Blast-radius: cross-tenant read surface

`/v1/morpheus/runs`, `/runs/{id}`, and `/runs/{id}/clusters` are available to any authenticated user and do not scope by owner, namespace, role, or a visibility predicate. The endpoint comments classify runs as operator telemetry, but returned fields include namespace, config, errors, cluster member memory IDs, and synthesized memory IDs.

Recommendation: require root/operator role for MORPHEUS telemetry, or scope reads to the caller namespace and filter cluster member IDs through the normal memory visibility predicate.

### 6. Raw asyncpg acquires bypass pool acquire timeouts

Severity: medium  
Location: `mnemos/core/pool.py:45`, `mnemos/api/dependencies.py:99`, multiple route modules  
Blast-radius: cross-cutting availability under DB pressure

`PoolManager.acquire()` enforces an acquire timeout, but many hot paths still call `_lc._pool.acquire()` directly. Under pool exhaustion, those calls can pile up indefinitely compared with routes using `get_pool_manager()`.

Recommendation: expose one request-safe acquire helper and migrate direct `_pool.acquire()` usage to it. Treat direct pool access as legacy and add a lint/test guard for route modules.

### 7. DAG read access drifts from memory read visibility

Severity: medium  
Location: `mnemos/api/routes/dag.py:43`, `mnemos/persistence/visibility.py`, `mnemos/api/routes/memories.py`  
Blast-radius: cross-route authorization invariant

Memory list/get/search use the shared read visibility model, including owner, group, world, federation, and namespace semantics. DAG `_assert_memory_access()` only allows root or exact owner+namespace. A caller can read a group/world-readable memory through memory routes but receive 404 for its log, commits, branches, or DAG operations.

Recommendation: split DAG checks into read and mutate helpers. Use `VisibilityFilter.for_read()` semantics for read-only DAG endpoints, and keep owner/root checks for mutating operations.

### 8. Federation peer URL validation is weaker than webhook URL validation

Severity: medium  
Location: `mnemos/api/routes/federation.py:126`, `mnemos/domain/federation.py:151`, `mnemos/domain/federation.py:608`  
Blast-radius: outbound federation security

Peer registration only validates `https://` unless insecure mode is enabled. The federation client then sends bearer tokens to the configured base URL. Unlike webhooks, there is no metadata-host/private-IP/non-routable rejection, so a root/operator mistake or compromised admin path can turn federation into an internal HTTP client with credentials attached.

Recommendation: reuse the webhook SSRF policy for federation, with an explicit allow-private override for lab deployments. Log and require confirmation for private ranges if federation intentionally supports private peers.

### 9. Auth enablement ignores typed/env settings and can fail open

Severity: medium  
Location: `mnemos/core/lifecycle.py:421`, `mnemos/core/lifecycle.py:466`, `mnemos/api/dependencies.py:26`, `mnemos/api/dependencies.py:86`, `mnemos/core/config.py:130`  
Blast-radius: server-wide auth boundary

The typed settings model includes env-backed server fields such as `MNEMOS_API_KEY`, but auth configuration is loaded from raw TOML via `_load_config()` and `config.get("auth", {})`. If a deployment is configured primarily through environment variables and lacks `[auth].enabled`, `get_current_user()` returns the unauthenticated root singleton.

Recommendation: add typed auth settings with env aliases and configure auth from `get_settings()`. For server/profile deployments, fail closed unless auth is explicitly disabled for a personal/local profile.

### 10. SQLite duplicate inserts report success

Severity: low  
Location: `mnemos/persistence/sqlite.py:610`, `mnemos/persistence/sqlite.py:642`  
Blast-radius: backend consistency

SQLite `insert_memory()` uses `INSERT OR IGNORE` and always returns `"INSERT 0 1"`. A duplicate explicit ID or import collision is silently treated as a successful create, diverging from Postgres behavior and from caller expectations around created webhooks and response semantics.

Recommendation: use `RETURNING`, `changes()`, or a follow-up existence check to distinguish insert from conflict. Return a consistent conflict/error path across backends.

## Things That Are Right

- The new persistence abstraction is moving the highest-risk memory CRUD/search paths toward shared repository contracts instead of route-local SQL.
- Memory create/update/delete and bulk create now enqueue webhook delivery rows in the same transaction and schedule delivery after commit.
- Visibility predicates are centralized enough to make cross-backend parity review possible; preserve that direction.
- Consultation persistence correctly commits the consultation row, audit entry, and memory refs in a single transaction.
- Webhook delivery has useful hardening: lease tokens, no redirects, response body caps, chained finalization, and recovery workers.
- The GPU guard and compression contest paths show good cancellation awareness compared with older async fan-out code.
- Version/DAG code has strong same-memory parent checks and per-snapshot visibility concepts; the main remaining issue is aligning the initial DAG access gate with those semantics.

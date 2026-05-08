# Known limitations — v5.0.1 (covers master through the unreleased v5.3.4 line)

This file lists known operational caveats that aren't bugs in
the strict sense but are worth surfacing for self-hosting
operators. Each entry includes the trigger conditions and the
recovery path so a deployment that hits one of these can be
unstuck without filing an issue.

## MCP tool-call audit trail — Phase-D shipped with residual gaps (v5.3.4)

**Where:** `mnemos/db/mcp_audit_repo.py` +
`mnemos/mcp/tools/_security.py` + migration
`migrations_v5_3_4_mcp_audit_log.sql` +
`mnemos/api/routes/mcp_audit.py`.

**What shipped:** durable audit table `mcp_audit_log`. The Python
logger entry remains the always-on surface; every MCP tool call is
also persisted to the table when a Postgres pool is available, via
fire-and-forget `create_task` inside `_mcp_log_tool_audit`. Standalone
MCP bridge processes (mcp-stdio, mcp-http) fall back to httpx POST
against `/v1/internal/mcp_audit` so they too persist via the API
process's pool.

**Outcome labels:** `called`, `success`, `failure`, `error`,
`denied`, `root_bypass`. Root-bypass entries are tagged so
operators can query for elevation events.

**Indexes:** by `created_at DESC`, by `(caller_user_id,
created_at DESC)`, and by `(tool, created_at DESC)`.

**SQLite parity:** schema mirror exists in
`db/migrations_sqlite/migrations_v5_3_4_mcp_audit_log_sqlite.sql`
but the writer is postgres-only (matches the `deletion_log`
pattern). SQLite-only deploys keep the logger-only surface.

### Residual gaps (round-3 deferred to v5.3.5+)

These are real but did not block the v5.3.4 cut — the audit table
is materially better than the logger-only baseline. Two follow-up
slices are tracked:

1. ✅ **Trust boundary on /v1/internal/mcp_audit (closed in v5.3.4
   #148; default-on via #150).** The endpoint now requires
   `X-Mnemos-Audit-Token: <value>` matching the configured
   `MNEMOS_INTERNAL_AUDIT_TOKEN` env var (constant-time compare).
   When that env var is set, normal API token holders cannot POST
   audit rows — only processes that share the env (the API itself
   + bridges configured with the same token) can write. When the
   env var is unset, the endpoint falls back to legacy bearer-token
   mode. Caller attribution remains locked (derived from auth
   context, body fields ignored). Bridges include the header
   automatically when the env var is configured.

   **#150 made this default-on for new installs.** The installer
   now autogenerates `[server].internal_audit_token` (256-bit
   `secrets.token_hex(32)`) on first install and persists it to
   config.toml (mode 0600). Operators no longer need to set the env
   var manually — the lockdown engages automatically. The resolver
   honors `MNEMOS_INTERNAL_AUDIT_TOKEN` env (operator override /
   rotation), then any existing token at the runtime-resolved
   config path (honors `MNEMOS_CONFIG_PATH` to avoid token skew
   between API and bridges), then any token in the in-memory config
   being patched, and finally falls back to fresh-generate. The
   `_set()` regex was hardened in the same slice to be line-anchored
   (rejecting commented `# key = ...` lines and IPv6 URL literals
   like `base = "http://[::1]:5002"`), with post-write `tomllib`
   validation that the parsed `[server].internal_audit_token` is
   non-empty before the file gets replaced.

2. ✅ **Tracked audit task with shutdown drain (closed in v5.3.4
   #149).** `_schedule_audit_persist` now tracks each created
   `asyncio.Task` in `_INFLIGHT_AUDIT_TASKS` and removes via
   `add_done_callback`. A new `drain_pending_audit_tasks(timeout)`
   helper awaits the in-flight set and is wired into:
   - **API process:** `register_lifespan_cleanup_hook("mcp audit
     drain", ...)` runs during FastAPI lifespan teardown before the
     pool closes.
   - **MCP stdio bridge:** `main()` awaits the drain in a `finally:`
     block before `asyncio.run` returns.
   - **MCP HTTP/SSE bridge:** Starlette `on_shutdown=[...]` hook
     awaits the drain.
   The set is bounded at 1024; under audit-DB outage, additional
   schedules log a warning instead of unbounded-growing the set.
   Drain timeouts log a warning naming the still-pending count but
   don't propagate (shutdown must complete).

3. **Forgeable parameter_shape via type allowlist gap (closed in
   round-3 with a fixed allowlist):** parameter_shape[*].type and
   item_types now reject any value not in the closed allowlist
   (str/bool/int/float/list/dict/none/bytes/tuple/set/frozenset/
   NoneType), so raw values like `{type: "sk_live_secret"}` are
   rejected at the validator. ✅ closed.

## MCP direct-DB write quota — local branch guard

**Where:** `mnemos/mcp/tools/dag.py`.

**Trigger:** MCP tools are executed through an in-process MCP server
instead of a REST route protected directly by SlowAPI middleware.

**Symptom:** REST-backed writes are covered by SlowAPI route-layer
limits. The direct `branch_memory` database path keeps a per-user
tool guard, but that MCP bucket is still process-local. A deployment
running multiple MCP processes can multiply that ceiling across
processes.

**Recovery / workaround:** prefer the REST-backed MCP transport for
multi-process deployments, keep edge/API rate limits enabled, and
avoid exposing direct-database MCP workers to untrusted clients.

**Fix scope:** a distributed quota bucket keyed by authenticated
caller and tool name, backed by the same shared rate-limit storage
as the HTTP route limiter.

## GDPR right-to-be-forgotten — final-verify race

**Where:** `mnemos/workers/deletion_request_worker.py`.

**Trigger:** the target user is actively writing memories
during their own deletion-request sweep AND the deployment
runs multiple worker replicas AND timing falls in the
millisecond gap between the worker's zero-row verify
`SELECT` and the `UPDATE deletion_requests SET status =
'soft_deleted'`.

**Symptom:** the new memory committed in that gap stays
`deleted_at = NULL` while the audit row claims the wipe
completed.

**Recovery:** the operator cancels the now-completed
deletion request and creates a new one. The new sweep picks
up the escaped row.

**Fix scope:** the proper fix is a target-scope write
fence (advisory lock keyed on the target user_id, taken by
every memory/kg/session write path while a covering
deletion request is active). That's invasive — every write
path in the codebase would have to consult the fence. The
current verify-pass loop catches everything except this
final-millisecond window, so the trade-off was to ship the
verify-pass and document the residual edge case rather than
delay the feature for an invasive write-fence sweep.

## GDPR right-to-be-forgotten — verify-loop exhaustion

**Where:** same module.

**Trigger:** sustained heavy writes against the deletion
target while the worker is sweeping. The bounded retry
(default N attempts) exhausts; the request stays in
`status = 'sweep_verifying'`.

**Symptom:** the deletion request is stuck — the worker's
dequeue query only picks `status = 'confirmed'`, and the
admin `cancel` / `restore` endpoints reject
`sweep_verifying`. The active-row partial unique index also
keeps blocking new deletion requests for the same target.

**Recovery:** SQL UPDATE to flip the row back to
`'confirmed'` (worker re-runs the sweep, including a fresh
verify pass) or `'cancelled'` (operator chooses to abort
and start over):

```sql
-- Re-run: worker will pick this up on its next dequeue.
UPDATE deletion_requests
   SET status = 'confirmed'
 WHERE id = '<deletion-request-uuid>'
   AND status = 'sweep_verifying';

-- Abort: cancels the request (operator must then create a
-- new one if they still want the wipe).
UPDATE deletion_requests
   SET status = 'cancelled'
 WHERE id = '<deletion-request-uuid>'
   AND status = 'sweep_verifying';
```

**Fix scope:** same write-fence story as the final-verify
race — a proper fix needs target-scope write coordination.
Until then, the bounded-retry + manual-recovery shape is
acceptable for self-hosted MNEMOS where operators have
direct DB access.

If you hit one of these in your own deployment, please open
an issue with the specific scenario — operational
edge-cases benefit from real-world reports, not synthetic
ones.

# Known limitations — v4.2.0a14

This file lists known operational caveats that aren't bugs in
the strict sense but are worth surfacing for self-hosting
operators. Each entry includes the trigger conditions and the
recovery path so a deployment that hits one of these can be
unstuck without filing an issue.

## MCP tool-call audit trail — Phase-D deferred

**Where:** `mnemos/mcp/tools/__init__.py`.

**Trigger:** an operator needs a durable answer to "who called
which MCP tool, with which parameters, and when".

**Symptom:** MNEMOS does not yet have a shared audit-log table for
generic MCP tool calls. The dispatcher has a Phase-D TODO at the
call site, but it deliberately does not invent a parallel MCP-only
audit stream because tool parameters can include memory content and
must be handled by one shared redaction-aware facility.

**Recovery / workaround:** issue per-user MCP tokens/API keys,
avoid the legacy shared `MNEMOS_MCP_TOKEN`, and retain reverse
proxy/API access logs long enough to correlate MCP clients with the
REST writes they trigger. For high-assurance deployments, place the
MCP HTTP edge behind an access gateway that records authenticated
caller, request time, and body hash until the Phase-D shared audit
slice lands.

**Fix scope:** add one durable, redaction-aware audit surface shared
by MCP transports and REST routes. The MCP dispatcher should wire
tool-call records into that shared facility when it exists rather
than inventing a parallel MCP-only table.

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

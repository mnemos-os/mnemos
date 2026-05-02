# Known limitations — v4.2.0a14

This file lists known operational caveats that aren't bugs in
the strict sense but are worth surfacing for self-hosting
operators. Each entry includes the trigger conditions and the
recovery path so a deployment that hits one of these can be
unstuck without filing an issue.

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

## Hard-delete after `restore_by` — not implemented

**Where:** `deletion_requests` lifecycle.

The 30-day grace period is computed and stored as
`restore_by = soft_deleted_at + INTERVAL '30 days'`. After
that timestamp, the spec says the data should be
hard-deleted (`DELETE FROM memories WHERE owner_id = ...
AND deleted_at IS NOT NULL`). The hard-delete worker hasn't
shipped yet — Phase C of the GDPR work is still pending.

**Operator workaround for now:** run the equivalent SQL by
hand against a deployment with deletion requests past their
`restore_by`. A scripted hard-delete worker is on the
roadmap for the next alpha.

---

If you hit one of these in your own deployment, please open
an issue with the specific scenario — operational
edge-cases benefit from real-world reports, not synthetic
ones.

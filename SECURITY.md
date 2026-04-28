# Security Policy

## Supported versions

The most recently maintained release branch is supported. The current
development branch is `v3.5-dev`; it is not a release tag.

## Current security invariants

As of v3.5-dev slice 6:

- Memory read visibility is symmetric across list/get/search/rehydrate,
  OpenAI-compatible gateway context, version history, DAG history, and MCP
  version tools. The live-memory predicate is centralized in
  `read_visibility_predicate` (`api/visibility.py:40-96`).
- Version history is gated per snapshot by `version_visibility_predicate`
  (`api/visibility.py:99-137`), so a later-public memory does not expose
  an earlier private snapshot.
- DAG logs stay within one memory and do not bridge across invisible
  snapshots. `parent_hash` is emitted only when the immediate parent is
  visible to the caller.
- Branch creation is race-safe: HTTP and MCP paths lock the parent memory
  row, resolve the start snapshot inside the transaction, and insert with
  `ON CONFLICT DO NOTHING RETURNING`.
- `db/migrations_v3_5_trigger_same_memory_parent.sql` rejects missing,
  NULL, or cross-memory branch heads with SQLSTATE `MN001`; the API maps
  that condition to HTTP 409 with branch reconciliation guidance.
- `db/migrations_v3_5_rls_group_select_unix_bits.sql` closes task #25:
  the `mnemos_group_select` RLS policy and application
  `read_visibility_predicate` both use the Unix group-read bit expression
  `((permission_mode / 10) % 10) >= 4`.
- Consultation audit metadata is owner-scoped for non-root callers:
  `/v1/consultations/audit` returns only the caller's consultation audit
  rows, and `/v1/consultations/audit/verify` verifies only that caller's
  rows. Root keeps the global operational audit view. This closes the
  v3.4.x cross-tenant audit metadata leak in v3.5-dev.

## Reporting a vulnerability

Please do not open a public GitHub issue for suspected vulnerabilities.

Instead, report security issues privately via GitHub: **@mnemos-dev** or by email to **security@mnemos.dev** (configure this address before public release)

Please include:
- a description of the issue
- impact assessment
- reproduction steps
- any suggested remediation

If a dedicated disclosure channel is added later, this file should be updated.

## Secrets policy

- Never commit `.env` files or live credentials.
- Store provider keys outside the repository.
- Sanitize infrastructure-specific details before public release.

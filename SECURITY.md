# Security Policy

## Supported versions

The most recently maintained release branch is supported. The current
release line is `v5.0.x`. v5.0.1 shipped on 2026-05-06 (on top of the
v5.0.0 GA from 2026-05-02 and v4.0.0 from 2026-04-29).

## Current security invariants

As of v5.0.1 (carried forward from v4.0.0):

- Memory read visibility is symmetric across list/get/search/rehydrate,
  OpenAI-compatible gateway context, version history, DAG history, and MCP
  version tools. The live-memory predicate is centralized in
  `read_visibility_predicate` (`mnemos/core/visibility.py`).
- Version history is gated per snapshot by `version_visibility_predicate`
  (`mnemos/core/visibility.py`), so a later-public memory does not expose
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
  v3.4.x cross-tenant audit metadata leak in v3.5.0.
- Webhook delivery uses persisted leases, retry-chain convergence, terminal
  success guards, and SSRF checks at subscription and delivery time.
- MCP stdio and HTTP/SSE use the same registry under `mnemos/mcp/tools/`, with
  per-user HTTP token mapping available through `MNEMOS_MCP_TOKENS`.
- Multi-worker server deployments use Redis-backed circuit breaker, rate-limit,
  and concurrency state. The in-process fallback remains for single-worker edge
  and dev installs and logs a warning if multiple workers are configured.
- Runtime configuration is centralized in the Pydantic Settings singleton;
  direct `os.environ` reads are limited to `mnemos/core/config.py` and the
  installer path.
- The OpenAI-compatible gateway passes supported generation controls through
  to providers and rejects unsupported tool, response-format, or multimodal
  requests instead of silently ignoring them.

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

# Changelog

All notable changes to MNEMOS are documented here.

## [3.5-dev] — in flight on `v3.5-dev` (unreleased)

v3.5 is being built as a branch sequence after v3.4.1. Do not treat this
as a release tag. The first two slices are merged: `a62a099` for
audit-quick-wins and `d42c475` for memory-read tenancy + DAG integrity;
task #25 is closed in v3.5-dev by the RLS group-select migration.

### Added

- **Shared read-visibility helper** — `api/visibility.py` now owns
  `read_visibility_predicate` (`api/visibility.py:40-96`),
  `version_visibility_predicate` (`api/visibility.py:99-137`),
  `_assert_target_head_visible` (`api/visibility.py:140-168`),
  and `handle_trigger_pgerror` (`api/visibility.py:24-37`).
- **Trigger replacement migration** —
  `db/migrations_v3_5_trigger_same_memory_parent.sql` replaces
  `mnemos_version_snapshot()` so UPDATE/DELETE resolve branch HEADs
  under lock, fail closed on missing/NULL/foreign heads with SQLSTATE
  `MN001`, and keep the DELETE tombstone path live.
- **RLS group-select policy migration** —
  `db/migrations_v3_5_rls_group_select_unix_bits.sql` replaces
  `mnemos_group_select` so RLS uses Unix group-read bit math
  (`((permission_mode / 10) % 10) >= 4`), matching
  `read_visibility_predicate` and closing #25.
- **Docker existing-volume upgrade path** — `docker-compose.yml` and
  `docker-compose.staging.yml` now include a one-shot
  `postgres-upgrade` service that applies v3.5 database patch
  migrations after Postgres is healthy. This is required because
  `/docker-entrypoint-initdb.d` only runs when a volume is first
  initialized.
- **Regression coverage** — new slice-2 tests cover branch visibility,
  cross-memory DAG guards, visibility gaps in logs, trigger concurrency
  locking, `MN001` update/delete conflict mapping, version tenancy, and
  migration-list sync. The merged branch reports 768 passing tests.

### Changed

- **Memory read surfaces use one predicate.** `list_memories`,
  `get_memory`, search, rehydrate, and gateway context now share the
  owner/federated/world/group-readable predicate. The Redis search
  cache key serializes raw inputs with `json.dumps(..., separators=...)`
  and includes group IDs so `None`, empty string, and group variation
  cannot collide.
- **History reads are per-snapshot.** `list_versions`, `get_version`,
  `diff_versions`, HTTP DAG log/get-commit/merge paths, and MCP
  log/checkout/diff paths gate each `memory_versions` row by the
  snapshot's own `owner_id`, `namespace`, and `permission_mode`.
  `memory_versions` lacks `group_id` and `federation_source`, so the
  version predicate intentionally fails closed for those historical
  cases.
- **DAG writers serialize on branch identity.** `merge_branch` and
  feature-branch `revert_memory` share `_branch_advisory_lock_key`
  (`api/handlers/dag.py:21-40`) and use advisory-lock-before-row-lock
  discipline. Main-branch revert still updates the live memory row
  through the trigger under the main GUC; feature-branch revert is a
  pure DAG insert.
- **Branch creation is race-safe.** HTTP and MCP branch creation lock
  the parent memory row with `FOR SHARE`, resolve the starting snapshot
  after the lock, and insert with `ON CONFLICT DO NOTHING RETURNING`.
  MCP implicit-HEAD retries are idempotent; explicit `from_commit`
  retries must match the existing head.
- **Merge writes target tenancy.** Merge commits copy content and
  provenance from the source snapshot but owner/namespace/permission
  from the target branch head; drift guards compare all versioned
  fields including tenancy before mutating live main.
- **Branch logs do not bridge hidden history.** Recursive log walks are
  same-memory only, and `parent_hash` is emitted only when the actual
  immediate parent is also visible.
- **Session history order fixed.** Slice 1 returns the most recent
  history messages instead of the oldest, with deterministic system-row
  pinning.
- **Project URLs moved.** `pyproject.toml` metadata points at
  `mnemos-os/mnemos`.

### Fixed

- **Webhook retry replay state machine** — `api/webhook_dispatcher.py:121-146`
  now recovers due `pending` rows plus `retrying` rows only when no
  successor attempt exists; `_attempt_delivery` treats
  `retry_scheduled` as terminal (`api/webhook_dispatcher.py:198-231`)
  and atomically inserts the successor before terminalizing the failed
  attempt (`api/webhook_dispatcher.py:353-392`). The new migration
  `db/migrations_v3_5_webhook_retry_terminal_state.sql` repairs
  existing superseded `retrying` rows and keeps them out of replay.
  Round 2 holds a `FOR UPDATE SKIP LOCKED` claim through recovery send
  and finalize, and adds an idempotent startup repair sweep for
  upgrade-window retry rows that gained a successor after the migration
  snapshot.

### Conflicts and operator handling

- Trigger-raised `MN001` maps to HTTP 409 with reconciliation guidance:
  the branch row is missing, has `NULL head_version_id`, or points to a
  version from another memory. Operators should reconcile
  `memory_branches` against `memory_versions` for that memory before
  retrying the write.

### Still open on the v3.5 backlog

- #21 federation per-peer ACL + stable cursor.
- #22 audit endpoint scoping + lifespan teardown.
- #23 entity namespace conflict-key migration.
- #19 bulk webhook parity.
- #15 deletion-log refactor.

## [3.4.1] — 2026-04-26

Federation schema-compat preflight + dev↔prod MPF restore drill.
Cross-version federation safety is the headline: peers now exchange
schema fingerprints before opening sync, refusing to pair when their
migration sets diverge unless an operator explicitly opts in via
`compat_mode=permissive`. Eight rounds of Codex adversarial review
on the federation handshake (verdict: SHIP). Restore-drill runbook
validated end-to-end on 10k records (~13s, 770 rec/sec) PYTHIA →
PROTEUS.

### Added

- **`GET /v1/federation/schema`** — preflight endpoint returning
  `mnemos_version`, `schema_signature` (`major.minor`), and
  `migrations_fingerprint` (sha256 over filename + content of
  `db/migrations*.sql`). Peers call this before opening sync and
  refuse to pair on mismatch.
- **`federation_peers` columns** — `compat_mode`
  (`strict|permissive`, default `strict`), `peer_mnemos_version`,
  `last_schema_check_at`. `strict` blocks sync on schema mismatch
  with HTTP 409 + operator-action message; `permissive` allows it
  through with a logged warning.
- **Typed exceptions** — `FederationSchemaIncompatible` /
  `Unverifiable` / `Transient` map to HTTP 409 / 409 / 503 so
  peers can distinguish "your schema is wrong" from "I can't
  reach you right now."
- **Native vs federated memory counts in `/stats`** — top-level
  totals plus a per-peer breakdown so operators can see at a
  glance which peer contributed which slice of the catalog.
- **`docs/RESTORE-DRILL.md`** — step-by-step dev↔prod MPF
  round-trip runbook: 5MB body cap on `/v1/import` means the CLI
  tool is the production path; `--preserve-metadata` is the
  dev↔prod lever; three-step DELETE + orphan sweep cleanup
  pattern documented.

### Changed

- **Worker queue ordering changed to next-due-time.** Previous FIFO
  starved peers with shorter `sync_interval_secs` when a longer-
  interval peer queued a large batch. New ORDER BY balances
  fairness across heterogeneous intervals.
- **`FEDERATION_ALLOW_INSECURE` plumbed through staging compose env.**
  Required for cross-version smoke tests on PROTEUS without
  full TLS termination.
- **MORPHEUS / APOLLO S-IVB naming locked** — no rename in v3.4.x.
  Both names appear in code, docs, and ops procedures by design;
  see `docs/PANTHEON.md`.

### Verified

- PROTEUS staging upgraded to v3.4.0, cross-version tested against
  PYTHIA v3.3.0 — `strict` returns 409 with operator-action message;
  `permissive` flip succeeds with 200. FK rollback applied during
  the v3.4 migration audit (issue #1 mnemos-os/mnemos rescoped to
  v3.5).

## [3.4.0] — 2026-04-26

CHARON v0.2 release: full MPF v0.1 sidecar round-trip, plus
staging-deploy infrastructure for PROTEUS as the cross-version
proving ground. Forty-four rounds of Codex adversarial review on the
sidecar attachment paths (cross-tenant attack surface, DAG
poisoning, version-DAG divergence, timestamp-shift, commit-hash
collision, conflict-row equality semantics, snapshot-consistent
export under REPEATABLE READ READ ONLY).

### Added

- **CHARON v0.2 sidecar round-trip** — full MPF v0.1 import + export
  with `--preserve-metadata` flag as the dev↔prod lever. Sidecar
  surfaces: `kg_triples`, `documents`, `facts`, `events`,
  `compression_manifest`, `memory_versions`. Tenant-scoped record
  IDs prevent cross-tenant sidecar attachment. Memory-versions
  sidecar requires root + `preserve_owner=true` (architectural
  restriction). Per-surface hard cap on sidecar export to bound
  memory consumption on large catalogs.
- **`docker-compose.staging.yml`** — PROTEUS staging compose,
  Postgres bound to :5433 (host-Postgres collision avoidance),
  pre-init `mnemos` role for fresh DB initialization.
- **v3.4 planning charters + ops doc** — `docs/V3_5_CHARTER.md`,
  `docs/V3_6_CHARTER.md`, `docs/V4_PLAN.md`, `docs/OPERATIONS.md`,
  `docs/PANTHEON.md` (extended with charter-bound sidecar
  ownership rules), `ROADMAP.md` cut.

### Changed

- **`OLLAMA_EMBED_*` env vars renamed to `INFERENCE_EMBED_*`.**
  Ollama is one of several inference backends; the variable name
  was misleading. Old names not honored — operators must update
  env files on upgrade.
- **GUC scope tightened on branch context.** Branch-scoped GUC now
  set within transaction only, parameterized to prevent injection.
  Same fix cherry-picked to v3.3.0 release as `8058666` (pre-v3.4
  audit).

### Audit

- Forty-four-round Codex adversarial review on sidecar paths.
  Closed: cross-tenant DAG-edge attack, shadow-parent attack,
  memory-ID oracle attack (records-loop), commit_hash collision,
  timestamp-shift, DAG-divergence integrity check, existing-memory
  DAG poisoning, no-main-branch import bypass, stale-version DAG
  poisoning, stale memory_branches not cleared before restore,
  version verification ON CONFLICT exact-match,
  `kg_triples` / `compression_manifest` ON CONFLICT exact-match,
  IS-NOT-DISTINCT-FROM semantics in conflict checks, in-envelope
  parents required for newly-inserted records, freshly-inserted vs
  conflict-skipped UUID tracking, JSON sidecars warn without
  `--preserve-metadata`, root-path conflict-row equality extension,
  conflict-row check covers all envelope-bound columns, pre-insert
  validation rejections block sidecar attachment, gated sidecar
  timestamp tolerance on freshness, COALESCE-tolerance for sidecar
  timestamp retries, snapshot-consistent export via REPEATABLE
  READ READ ONLY.

## [3.3.0] — 2026-04-26

Compression-stack settlement, CI policy flip to GitLab, MORPHEUS
slice 2 (real cluster + synthesise), and the EVOLUTION.md origin
narrative. Closes the v3.2 compression-stack open question by
retiring ALETHEIA from the default contest.

### Added

- **MORPHEUS slice 2 — real cluster + synthesise phases.** Phase 1
  foundation shipped in v3.3.0-alpha.1; slice 2 adds the cluster
  pass (semantic grouping over the working set) and the synthesise
  pass (LLM-mediated synthesis of cross-memory patterns into
  derived facts). Three audit-log items closed: namespace scope on
  cluster output, cluster introspection endpoint, FastAPI
  deprecation cleanup. 31 tests in `tests/test_knossos.py` cover
  the phase-1 tool surface (0.46s).
- **`recall_count` + `last_recalled_at` on memory search hits.**
  Every search result increments the recall counter and updates
  the timestamp. Useful for downstream "warmest" / "coldest"
  prioritization queries.
- **`docs/EVOLUTION.md`** — five-month development timeline from
  v0.1 design review through v3.2 compression-stack settlement.
  Restructured to put origin story in v1.0 section + ADR block
  for release-gate decisions.
- **GitLab CI** (`.gitlab-ci.yml`) — three stages (`lint`, `test`,
  `build`) running against real Postgres + pgvector service. See
  `~/.claude/rules/gitlab-ci-policy.md` for the full rationale.
- **GitHub `pr-check.yml`** — slim PR-only lint + unit test
  workflow so external contributors get green/red signal on PR
  without maintainer-side GitLab pre-flight.

### Changed

- **`/kg` and `/sessions` routers moved to `/v1/` prefix.** The
  v2 endpoints stayed in place during the v3.0–3.2 transition;
  v3.3 finishes the migration. Old paths return 410 with the new
  path in the response body.
- **ALETHEIA retired from the default compression stack.** The
  going-forward stack is LETHE + ANAMNESIS + APOLLO (APOLLO in
  v3.3+ per ROADMAP.md Apollo Program). ALETHEIA won 0 contests in
  the 2026-04-23 CERBERUS benchmark — its index-list scoring prompt
  doesn't survive instruction-tuned generalist LLMs, and the
  fallback-to-first-N path is strictly inferior to LETHE at lower
  cost. Niche audit found every case where ALETHEIA might
  theoretically win is owned by LETHE (cheaper), ANAMNESIS (better
  fact shape), or APOLLO (schema-typed). `ALETHEIAEngine` now emits
  a DeprecationWarning on construction and is skipped in the
  default contest (`distillation_worker.py` still honors
  `MNEMOS_ALETHEIA_ENABLED=true` for operators who had it opted in,
  but logs a deprecation warning when that gate flips on). The
  engine class stays importable; v4.0 removes it entirely. See
  `docs/benchmarks/compression-2026-04-23.md` for measured rationale
  and the niche audit captured in-session.

### Removed

- **LETHE / ANAMNESIS / ALETHEIA modules and `CompressionManager`
  removed from the active code path.** Engine classes stay
  importable for backward compatibility; the manager is gone. The
  contest harness instantiates engines directly.

### Fixed

- **`install.py` / `installer/db.py` migration loaders include
  v3.2.2 + v3.3 migrations.** Two newer migration files were
  silently absent from the canonical loader.
- **CI pre-creates `mnemos_user` + `mnemos` roles** before applying
  migrations, eliminating a flaky CI failure mode where migrations
  ran before role provisioning completed.

## [3.2.0] — 2026-04-23

Tenancy + observability + ideation-infrastructure release. Rolls in
the v3.1.1 ops-hardening and v3.1.2 Tier-3 tenancy candidates, adds
the full request-correlation/metrics/tracing/logs observability
stack, wires compression artifacts into the hot retrieval paths,
makes the OpenAI-compatible gateway registry-first, lands per-user
namespace tenancy end-to-end (DB column, auth resolution, admin
provisioning API, Tier 3 enforcement on DAG / entities / webhooks),
ships MPF v0.1 export / import, brings the reasoning layer in line
with the public contract (consensus_response / consensus_score /
winning_muse / cost / latency_ms populated from the engine's own
_compute_consensus output), and opens `/v1/consultations` to
operator-driven Custom Query selection across the refreshed
frontier model registry. Queue workers are now self-healing (stale-
running sweep with forward-progress guarantee) and the GPUGuard
circuit breaker handles auto-replacement safely via a probe-identity
handshake. Closes every HIGH finding from the v3.2 memory-OS audit
and the full follow-up Codex re-audit.

### Ops hardening — v3.1.1 candidate

- **Stranded-running queue recovery sweep**
  (`compression/worker_contest.py`, `distillation_worker.py`). The
  v3.1 contest path had a belt-and-suspenders gap: if the
  fresh-connection mark-failed fallback ALSO failed (pool
  exhausted, SIGKILL mid-txn, host reboot), a queue row sat in
  `running` forever because the dequeue only matched `pending`.
  New `_sweep_stale_running()` runs at the top of every batch,
  reclaiming rows stale longer than
  `MNEMOS_CONTEST_STALE_THRESHOLD_SECS` (default 600). Rows below
  retry ceiling go back to `pending`; rows at ceiling go terminal
  `failed` with `stranded_running: ...` marker.

- **Sweep-vs-late-finisher race defense.** `_process_one` opens its
  persist transaction with `SELECT ... FOR UPDATE` against
  `memory_compression_queue` and checks both `status='running'` AND
  `attempts == <dequeue-time value>`. If the sweep reclaimed or
  another worker re-dequeued after reset, the fingerprint mismatches
  and the worker bails cleanly — no duplicate audit rows, no
  overwrite. New `race_abandoned` metric counter.

- **GPUGuard single-probe in HALF_OPEN** (`compression/gpu_guard.py`).
  The circuit breaker's HALF_OPEN state admitted every concurrent
  caller as the probe — thundering herd against a possibly-still-
  broken endpoint. Added `_probe_in_flight` coordination so exactly
  one probe is admitted at a time. Subsequent callers fast-fail
  until the probe resolves via `record_success` / `record_failure`.
  Auto-replacement of a wedged probe is intentionally NOT included
  in v3.1.x — avoiding an identity-tracking handshake that would
  have been needed to prevent late-completion races. Operators
  recover a wedged HALF_OPEN via `reset()`.

- **Richer error metadata in candidate manifests**
  (`compression/contest_store.py`). `persist_contest()` now runs
  every non-winner candidate through `_enriched_manifest()`, which
  preserves engine-authored manifest keys and ADDS a namespaced
  `_audit` block: `reject_reason`, `engine_version`, `error`
  (full exception text, previously lost from the DB), `quality_score`
  on floor rejections, `compression_ratio`, `elapsed_ms`, `gpu_used`.
  Winners are not enriched — their typed columns are authoritative.
  Non-dict engine-authored `_audit` values are preserved under
  `_audit_original` rather than crashing persist.

- **Log-space `speed_factor` to stop multiplicative speed dominance**
  (`compression/contest.py`). Raw linear `fastest_ms/elapsed_ms`
  crushed slow-but-accurate engines — a 10x-slower candidate
  scored 0.1 and multiplied through the composite made
  quality_first weighting unable to recover. Now:
  `factor = clamp(1 + log10(ratio)/2, [SPEED_FACTOR_FLOOR=0.1, 1.0])`.
  10x-slower maps to 0.5, 100x-slower bottoms at the floor. This
  is a scoring-breaking change for the `speed_factor` column;
  existing v3.1.0 rows are on a different scale.

- **Scoring-profile validation** (`compression/contest.py`).
  Custom TOML profiles previously accepted any `float()`-able
  value. Negative weights, 1000x weights, `quality_floor >= 1.0`,
  non-numeric strings, and NaN/Inf all produced surprising
  behavior. New `_validated_profile()` clamps weights to
  `[0.0, 10.0]` and `quality_floor` to `[0.0, 0.99]` with loud
  WARNING logs on every clamp. Explicit NaN/Inf rejection
  (they compare False to any numeric bound and would silently
  poison composite scores for every candidate).

- **`docs/SYSTEM_REQUIREMENTS.md`** — per-tier (Server / Workstation
  / Edge) resource floor reference. CPU / RAM / disk / GPU per
  tier, baseline (Python / Postgres / pgvector), environment
  knobs (`MNEMOS_CONTEST_ENABLED`, `MNEMOS_ALETHEIA_ENABLED`,
  `MNEMOS_CONTEST_STALE_THRESHOLD_SECS`), observed resource
  usage from live deployments as sanity check.

### Tier 3 tenancy rollup — v3.1.2 candidate

- **KG triples carry `owner_id` + `namespace`**
  (`db/migrations_v3_1_2_kg_tenancy.sql`, `api/handlers/kg.py`).
  Previously `kg_triples` had no tenancy columns and the `/kg`
  read/mutate paths had NO owner filter at all — every
  authenticated caller saw every row. Added columns idempotently
  (ADD COLUMN without DEFAULT, backfill from linked memory
  rows via `memories.memory_id` join, residual NULLs → 'default',
  THEN SET DEFAULT + NOT NULL — sequencing matters because
  ADD COLUMN DEFAULT would have made the backfill a no-op).
  Handlers now filter on BOTH owner AND namespace for non-root
  callers; cross-tenant `memory_id` references on create are
  rejected 404 (not 403 — existence is invisible to non-owners).

- **App-layer namespace enforcement on `list_memories` and
  `get_memory`** (`api/handlers/memories.py`). RLS policies from
  v1_multiuser scope `owner_id` + `group_id` but never filter by
  `namespace`. Personal-mode installs with RLS disabled had no
  tenancy filter at all. Non-root callers now get
  `AND namespace = user.namespace` appended to every WHERE
  branch (combined with category/subcategory filters). Root
  bypasses.

- **Owner + namespace pinning on `/memories/search` and
  `/memories/rehydrate`** (`api/handlers/memories.py`,
  `api/lifecycle.py`). `_fts_fetch` and `_vector_search` gained
  an `owner_id` kwarg. Non-root searches force `owner_id =
  user.user_id` and `namespace = user.namespace` regardless of
  the request body. Cross-namespace request from non-root →
  HTTP 403 (explicit rejection rather than silent narrowing).
  Cache key now hashes the EFFECTIVE pinned values, not the
  raw request.

- **Namespace enforcement on mutation precheck paths**
  (`api/handlers/memories.py`). `update_memory`,
  `delete_memory`, and `get_compression_manifests` now check
  BOTH owner AND namespace for non-root callers.

- **Registry-backed `/v1/models`** (`api/handlers/openai_compat.py`).
  Replaces the hardcoded six-entry list with
  `SELECT … FROM model_registry WHERE available AND NOT deprecated
  ORDER BY graeae_weight DESC`. Fallback to a built-in list when
  the registry is empty (fresh install) or the query fails.
  `get_model` resolves aliases first, then registry lookup;
  unregistered IDs still return with `owned_by="Unknown"` since
  operators may route to locally configured models.

### Provider routing + audit fixes — handoff work

- **Provider-unavailability errors now explain the cause**
  (`graeae/engine.py`, `api/handlers/openai_compat.py`).
  `_unavailable()` gained an `error: str` field; `route()` populates
  it for each failure class (provider not registered, missing
  api_key, upstream exception). `/v1/chat/completions` surfaces
  the cause in the 503 detail so operators don't have to tail
  debug logs:
  ```
  HTTP 503 {"detail": "Provider anthropic unavailable: HTTP 401 Unauthorized"}
  ```
  Missing-key case is caught pre-dispatch with a targeted hint
  at the standard env var to set.

- **MNEMOS-native Provider Registry File + env-var fallback**
  (`graeae/api_keys.py`). The key-file loader was too rigid and
  too permissive in the wrong ways: it only accepted the canonical
  `{"llm_providers": {...}}` shape AND logged only a generic
  warning on missing files. Replaced with:
  - Canonical shape only (MNEMOS-owned format, self-contained,
    no symlinks to third-party service key files).
  - Per-provider environment variable fallback using standard
    names every vendor SDK uses — `OPENAI_API_KEY`,
    `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`,
    `GROQ_API_KEY`, `PERPLEXITY_API_KEY`, `TOGETHER_API_KEY`,
    `NVIDIA_API_KEY`. Env vars win when both are set.
  - Search-path order swapped so `~/.config/mnemos/api_keys.json`
    is preferred over the legacy `~/.api_keys_master.json`.
  - `load_api_keys()` → `load_provider_registry()` with a
    backward-compat alias.

- **Refreshed frontier model defaults** (`graeae/engine.py`,
  `api/handlers/openai_compat.py`). v3.1.0 shipped with 2024-era
  model IDs. Updated to current:
  - `openai: gpt-4o → gpt-5.2-chat-latest`
  - `claude: claude-3-5-sonnet-20241022 → claude-opus-4-6`
  - `gemini: gemini-1.5-pro → gemini-3-pro-preview` (URL too)
  - `xai: grok-2-latest → grok-4-1-fast`
  - `perplexity: sonar-pro` (unchanged)
  - `groq: llama-3.3-70b-versatile` (unchanged)

  GPT-5 series requires `temperature=1` (returns 400 on any other
  value). `_query_openai_compatible` now omits the temperature
  field for `gpt-5*` models, matching the existing
  `max_completion_tokens` branching.

- **`graeae_audit_log` schema backfill**
  (`db/migrations_v3_1_2_audit_log_columns.sql`). Databases that
  applied `migrations_v2_versioning.sql` first got the v2 table
  shape; `migrations_v3_graeae_unified.sql` used `CREATE TABLE IF
  NOT EXISTS` so the six new columns (`prompt`, `response_text`,
  `prev_chain_hash`, `model`, `latency_ms`, `cost_usd`) never
  landed. The consultations handler INSERT referenced these by
  name, so `/v1/consultations` returned 503 with "audit trail
  is required" and an `UndefinedColumnError` in the log.
  Added all six via `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.
  All nullable so existing hash-only audit rows aren't
  retroactively invalidated.

- **UUID → str coercion on consultation response**
  (`api/handlers/consultations.py`). asyncpg returns UUID columns
  as `uuid.UUID` objects; `ConsultationResponse.consultation_id`
  is typed `Optional[str]` and pydantic strict mode rejected the
  UUID. One-line coercion at the construction site.

### Tests

Suite: 282 → 295 → 303 → 309 → 317 → 318 → 318 across the series.
All targeted tests green, full suite 0 regressions.

### Deferred to later releases (v3.3+)

- Horizontal scaling past workers=1 — GRAEAE reliability state
  (circuit breaker, rate limiter, semaphores) is process-local, so
  the server is still pinned to single-worker uvicorn. External
  state store (Redis) or session-affinity at the load balancer is
  the path forward.
- Webhook SSRF DNS-rebinding defense — the current allowlist is
  checked once at subscribe time; a malicious DNS TTL could still
  flip to an internal IP between check and delivery. Needs
  per-delivery re-resolution against a pinned IP.
- Federation peer tokens stored plaintext — `federation.py:113`
  still writes tokens in the clear; needs symmetric-encrypt-at-rest
  with operator-supplied key or KMS plugin.
- APOLLO engine (v3.2–v3.4 per ROADMAP.md) — schema-aware dense
  encoding for LLM-to-LLM wire use. S-IC scheduled for v3.3 kickoff.
- Dream state (v3.3 preview / v3.4 real) — divergent-mode ideation
  riding on APOLLO's dense-form substrate. Design scoped in
  `docs/DREAM_STATE_DESIGN.md`.

## [3.1.0] — 2026-04-23

Compression platform release. Adds a plugin `CompressionEngine` ABC open
to operator-registered engines, a competitive per-memory contest across
three built-in engines, and a persisted audit log recording every
winner AND loser per contest with its score and disqualification
reason — not just the chosen output. Extends the v3.0 schema with three
new tables (`memory_compression_queue`, `memory_compression_candidates`,
`memory_compressed_variants`) wired through a GPU circuit breaker that
fast-fails when the inference endpoint is unreachable.

Ships the Tier 1 small-fix unblocks already on master since 2026-04-22
under the v3.1 umbrella; Tier 3 tenancy fixes are explicitly deferred
to v3.1.1; APOLLO (the fourth engine, schema-aware dense encoding for
LLM-to-LLM wire use) is staged across v3.2–v3.4 per `ROADMAP.md`.

### Added

- **Plugin `CompressionEngine` ABC** (`compression/base.py`). Open
  interface for first-party and operator-registered engines. Declares
  `id`, `label`, `version`, `gpu_intent` at class level. One async
  method, `compress(CompressionRequest) -> CompressionResult`. Adapted
  from OpenClaw's `CompactionProvider` pattern (Apache-2.0, credited in
  module docstring).

- **Three engines under the ABC**: LETHEEngine (extractive, CPU),
  ALETHEIAEngine (LLM-assisted token importance, GPU), ANAMNESISEngine
  (LLM fact extraction, GPU). All three compose the existing v3.0
  engines; existing sync callers (manager.py, distillation_engine.py)
  continue to work unchanged.

- **Competitive-selection contest** (`compression/contest.py`). The
  distillation worker runs every eligible engine per memory via
  `asyncio.gather`, scores each candidate via a composite function
  (`quality * ratio_term * speed_factor`, with a quality floor that
  disqualifies damaged output), and picks the highest-scoring survivor.
  Scoring profile configurable via `~/.mnemos/compression_scoring.toml`:
  `balanced` | `quality_first` | `speed_first` | `custom`.

- **Persisted contest audit log** (`compression/contest_store.py`).
  `persist_contest()` writes every candidate (winner AND losers)
  into `memory_compression_candidates` and upserts the winner into
  `memory_compressed_variants` in a single transaction. Operators
  get a full record of what was tried, what scored how, and why each
  engine was or wasn't picked.

- **GPU circuit breaker** (`compression/gpu_guard.py`). Per-endpoint
  three-state breaker (CLOSED → OPEN → HALF_OPEN → CLOSED) tracks
  health of each configured `GPU_PROVIDER_HOST`. `gpu_required`
  engines (ALETHEIA, ANAMNESIS) fast-fail with
  `reject_reason='disabled'` when the circuit is open instead of
  piling doomed requests onto a dead endpoint. Process-local
  registry (v3.2 horizontal-scaling work makes it shared-state).

- **Distillation-worker queue drain** (`compression/worker_contest.py`
  + `distillation_worker.py`). `process_contest_queue()` atomically
  dequeues pending rows via `FOR UPDATE SKIP LOCKED`, runs the
  contest, persists the outcome, transitions the queue row
  `pending → running → done/failed` with an honest rejection-reason
  summary on failure. Runs alongside the existing v3.0 direct-memory
  polling loop; failure-isolated so a contest error doesn't stall
  the legacy path.

- **`GET /v1/memories/{id}/compression-manifests`** endpoint
  (`api/handlers/memories.py`). Returns the current winning variant
  and every historical contest grouped by `contest_id`, with
  scoring fields and reject_reason per engine attempt.
  `?include_content=true` returns full compressed content; default
  is a 200-char preview. RLS-gated via the underlying memories
  table.

- **v3.1 schema** (`db/migrations_v3_1_compression.sql`). Three new
  tables wired idempotently: `memory_compression_queue` (write-time
  task queue), `memory_compression_candidates` (full contest log),
  `memory_compressed_variants` (current winner per memory). Dry-run
  validated against real Postgres.

- **Environment flags**:
  - `MNEMOS_CONTEST_ENABLED` (default `true`) — gates the whole v3.1
    path. Operators who want to run v3.0 behavior exclusively can
    flip to `false`.
  - `MNEMOS_ALETHEIA_ENABLED` (default `false`) — see "Changed"
    below.
  - `MNEMOS_CONTEST_MIN_CONTENT_LENGTH` (default `0` = off) —
    optional threshold below which the worker marks queue rows
    `failed` with `error='too_short'` before running any engine.
    Surfaced by the 2026-04-23 benchmark: ~8% of real production
    memories are short templated blurbs (git commit headers,
    consultation stubs) that cannot be meaningfully compressed under
    any engine at the balanced profile's floor — LETHE returns
    ratio~1.0, ANAMNESIS's rendering inflates past 1.0, contest
    fails "no winner" after burning ANAMNESIS's multi-second GPU
    round-trip. Recommended value `500` for GPU-constrained installs;
    default `0` preserves the full-contest behavior.

- **Admin compression-queue endpoints** (`api/handlers/admin.py`):
  - `POST /admin/compression/enqueue` — enqueue specific memory IDs
    into `memory_compression_queue`. Skips unknown IDs silently
    (reports count in response).
  - `POST /admin/compression/enqueue-all` — bulk enqueue up to
    `limit` (default 500, max 10,000) memories. Default filters to
    memories without an existing variant; `only_uncompressed=false`
    forces re-contest.
  Without these, the v3.1 contest pipeline has no application-layer
  entry point — operators would need manual SQL to exercise it.

- **First real benchmark**:
  `docs/benchmarks/compression-2026-04-23.md`. 464 stratified memories
  from PYTHIA MNEMOS (uncompressed only, small/medium/large buckets)
  drained through the contest on a CERBERUS test deployment with
  gemma-4-E4B-it-Q6_K as the judge model. Winner distribution,
  per-category breakdown, ratio histogram, timing histogram per
  engine, outlier cases, and the one real bug the drain surfaced
  and fixed.

- **`ROADMAP.md`**. Committed scope for v3.1 and the v3.2–v3.4
  "Apollo Program" staged rollout. Explicit deferrals with
  rationale.

### Changed

- **ALETHEIA is disabled by default** (`MNEMOS_ALETHEIA_ENABLED=false`).
  The v3.0 engine's index-list scoring prompt ("output comma-separated
  token indices to keep") doesn't survive instruction-tuned chat
  models — tested against Qwen2.5-Coder-7B and gemma-4-E4B-it, both
  return off-spec text the parser can't interpret. Parser falls
  through to first-N truncation with honest `quality_score=0.60`,
  which the balanced profile's 0.70 quality_floor correctly rejects.
  Engine never wins and burns GPU time. Default engine roster is now
  LETHE + ANAMNESIS. Operators with a tuned prompt/model combination
  opt in via the env var. The prompt redesign is v3.x scope.

- **README.md + ROADMAP.md reality-alignment audit**. Stripped APOLLO
  from v3.1 descriptions (moved to v3.2–v3.4). Switched "four engines"
  → "three engines under a plugin ABC". Normalized stale v3.0.0
  language to v3.0 (release line). Removed "on the roadmap" claims
  for integration adapters not actually in the roadmap. Generalized
  specific production-count numbers that would age.

### Fixed

- **Tier 1 unblocks** (already on master as 2026-04-22 commits, now
  under the v3.1 umbrella):
  - MCP stdio server path prefix (`#M31-01`). The published stdio
    MCP server called `/memories*` but the REST router registers
    `/v1/memories*` — nine of fourteen memory tools returned 404
    against a default install.
  - Installer `api_keys` schema alignment (`#M31-04`). Fresh
    auth-enabled installs failed at seed because `installer/db.py`
    wrote columns the current schema no longer has.
  - Admin `create_user` accepts `role='federation'` (`#M31-03`).
    Federation peer onboarding previously required direct SQL writes
    because the admin validator and the v1_multiuser CHECK
    constraint both rejected the role at creation time.

- **`mnemos_version_snapshot()` trigger bytea crash on backslash
  content** (`db/migrations_v3_1_versioning_fix.sql`). The v2
  versioning trigger computed `commit_hash` via direct `text::bytea`
  cast on concatenated memory content. Postgres interprets
  backslash-escape sequences (`\x47`, `\d+`, `\0`, `\n`, `\x1b[...`)
  as bytea escape syntax and rejects the INSERT outright with
  "invalid input syntax for type bytea". Affected any production
  install ingesting memories that contain code, paths, or regex
  patterns — which is most real content. Latent since v2 shipped;
  surfaced by the v3.1 CERBERUS test deployment running real PYTHIA
  memories. Fix replaces `(text)::bytea` with `convert_to(text,
  'UTF8')` which returns raw UTF-8 bytes without trying to parse
  escape sequences. Idempotent migration; `CREATE OR REPLACE
  FUNCTION` replaces the existing definition in place.

- **Composite-zero winner CHECK-constraint violation**
  (`compression/contest.py`). Short memories where every engine
  scored `composite_score=0` (ratio at or below MIN_CHUNK_RATIO
  or >= 1.0) previously "won" the contest with
  `persist_contest`'s NULL coercion violating
  `mcc_winner_has_output`. Surfaced during the 49-memory CERBERUS
  drain. `run_contest` now requires `composite_score > 0` for
  winner eligibility; zero-composite survivors fall through to
  `reject_reason='inferior'`, and the queue row is marked `failed`
  with an honest "no winner" message rather than silently storing a
  degenerate "winner" variant.

- **ALETHEIA parser returns first-N fallback on unparseable model
  responses** (`compression/aletheia.py`). Pre-existing v3.0 bug
  where the importance-score parser returned empty content when
  zero valid indices survived filtering (as opposed to an actual
  exception). Now explicitly raises on empty-indices → existing
  first-N fallback fires. Compress result reports honest
  `quality_score=0.60` and `method='aletheia_parse_fallback'` when
  fallback is used. Surfaced during live-GPU testing against Qwen
  and gemma; the contest correctly filters the degenerate output
  via the ratio_term floor, but the audit log now accurately shows
  WHAT happened rather than reporting "aletheia" with empty content.

- **`ratio_term` floor below MIN_CHUNK_RATIO** (`compression/contest.py`).
  Scoring function returned `1.0 - ratio` for any ratio, which
  rewarded degenerate empty-output engines (ratio=0) with maximum
  score. Now returns 0 for ratios below `MIN_CHUNK_RATIO` (0.15) or
  at/above 1.0 — empty output and non-compression both score zero.
  Surfaced by live-GPU testing of ALETHEIA.

### Deferred

- **Tier 3 tenancy fixes** — v3.1.1 patch series with migration
  guides and per-fix regression coverage. Covers KG `owner_id`
  column + handler enforcement, namespace enforcement on memory
  paths, application-layer owner filter (defense-in-depth beside
  RLS), and registry-backed `/v1/models` (instead of hardcoded list).
- **APOLLO engine + schema-aware dense encoding** — v3.2–v3.4
  Saturn V-staged rollout per `ROADMAP.md`. Design informed by
  InvestorClaw's consultative-LLM pipeline pattern, not by raw
  Apollo-era telemetry specs.
- **Narration endpoint** (`GET /v1/memories/{id}/narrate`) — v3.2,
  APOLLO's companion read path.
- **Hot-path compression-variant reads** (rehydrate / gateway inject
  / session context serving winner variants instead of raw
  `memories.content`) — v3.2 alongside APOLLO.
- **Judge-LLM quality scoring** replacing engine self-reports —
  v3.2 alongside APOLLO. Today's scoring depends on engines'
  self-reported quality; a real judge would likely shift some
  wins between engines.

## [3.0.1] — 2026-04-22

Patch release fixing three credibility-sensitive defects in the initial
public cut of v3.0.0. No feature changes, no schema changes, safe in-place
upgrade.

### Fixed

- **OpenAI gateway: full conversation history reaches the provider**
  (`api/handlers/openai_compat.py`). The `_route_to_provider` helper used
  by `/v1/chat/completions` and `/sessions/*/messages` previously
  collapsed the request to `messages[-1]["content"]`, silently dropping
  the system prompt, injected memory context, and every prior assistant
  turn before the provider call. A new `_flatten_messages_for_prompt`
  helper serializes the full `messages` array with role boundaries so
  multi-turn chat and session history reach the provider intact. Silent
  regression — no error, just degraded responses — fixed.

- **Docker Compose applies all 11 migrations, not 4**
  (`docker-compose.yml`). The v3.0 Compose file mounted only the first
  four migration files into `docker-entrypoint-initdb.d/`. Fresh Compose
  installs booted without sessions, DAG, consultations audit, webhooks,
  OAuth, federation, or ownership tables — every v3 route 500'd on first
  use. All eleven migration files are now mounted in the canonical
  order (matches `installer/db.py::run_migrations()`).

- **Session compression metrics tightened** (`api/handlers/sessions.py`).
  The session-injection path currently ships raw-slice truncation, not
  real compression; the `compression_ratio` columns on
  `session_messages` and `session_memory_injections` now write `NULL`
  rather than placeholder constants. Real ratios are populated in v3.1
  once compression is wired into the session path.

### Also

- Internal renaming: compression mode aliases in `compression/lethe.py`
  and `compression/distillation_engine.py` updated to accurate
  descriptors. No behavior change; source-tree honesty pass.

## [3.0.0] — 2026-04-22

First public release.

MNEMOS has been in daily production use since December 2025, backing multiple
active agentic systems. This is the first cut shipped as open source — a
single unified FastAPI service covering memory, multi-LLM consensus
reasoning, DAG versioning, provider routing, and an OpenAI-compatible
gateway.

### What's in

**Unified API under `/v1/*`**

- **Consultations** (`/v1/consultations`) — GRAEAE multi-LLM consensus
  reasoning with cited memory artifacts and a tamper-evident SHA-256
  hash-chained audit log. Memory-injection tracking per consultation via
  `consultation_memory_refs`. Atomic persistence: consultation row, audit
  entry, and memory refs commit in a single transaction; audit-write
  failure aborts the consultation.
- **Memories** (`/v1/memories`) — CRUD, semantic + FTS search, DAG
  versioning (git-like: `log`, `branch`, `merge`, `revert`), three-tier
  compression pipeline (LETHE CPU / ALETHEIA GPU / ANAMNESIS archival)
  with a written quality manifest on every transformation.
- **Providers** (`/v1/providers`) — unified catalog, health tracking,
  task-aware model recommendation. Falls back to static config when the
  model-registry table is empty (fresh-install friendly).
- **OpenAI-compatible gateway** (`POST /v1/chat/completions`,
  `GET /v1/models`) — drop-in for OpenAI SDK consumers with automatic
  provider routing and optional memory injection.
- **Sessions** (`/sessions`) — stateful multi-turn chat with memory
  injection at turn boundaries.
- **Webhooks** (`/v1/webhooks`) — HMAC-SHA256-signed outbound event
  delivery. SSRF-hardened URL validation at both subscription and
  dispatch time (loopback, private, link-local, cloud-metadata endpoints
  all rejected). Durable retry log replayed on restart (1m / 5m / 30m /
  2h backoff; `abandoned` after four attempts).
- **OAuth / OIDC** (`/auth/oauth/*`) — browser login via Google, GitHub,
  Azure AD, or any generic OIDC provider (Keycloak, Authentik, Auth0,
  Okta). DB-backed sessions, hourly GC, `email_verified` required for
  cross-provider account linking. Coexists with API-key Bearer auth.
- **Federation** (`/v1/federation/*`) — pull-based cross-instance memory
  sync. Per-memory opt-in via `permission_mode` (others-read bit).
  Admin-only peer management, `federation`-role `/feed` endpoint,
  loop-prevention via `federation_source`.
- **Knowledge graph** (`/kg/triples`, `/kg/timeline/{subject}`) —
  temporal triple store with `valid_from` / `valid_until` windows.
- **Per-owner multi-tenant isolation** on memories, consultations,
  state, journal, entities. Root-only override for cross-owner
  operations.

**Infrastructure and tooling**

- Python 3.11+, PostgreSQL + pgvector, asyncpg.
- Body size limit enforced as streaming ASGI middleware (chunked-upload
  safe, default 5 MB, `MAX_BODY_BYTES` configurable).
- Rate limiter keyed on socket peer by default; honours `X-Forwarded-For`
  when `RATE_LIMIT_TRUST_PROXY=true`.
- Distillation worker supervised with exponential-backoff restart
  (cap 5 min).
- TLS enforced on federation peer URLs (opt-out via
  `FEDERATION_ALLOW_INSECURE`).
- CI runs under `uv` with a reproducible `.venv`. Ruff-clean tree.
- Installer CLI (`mnemos-install`) shipped as a `[project.scripts]`
  entry point so `pip install mnemos-os` gives you a working install
  binary without needing the source tree.
- All eleven SQL migrations ride inside the wheel as `db/*.sql`
  package data — accessible at runtime via
  `importlib.resources.files("db")`.

**Integrations**

- Drop-in hooks, skills, and MCP configs for Claude Code, OpenClaw,
  ZeroClaw, and Hermes. Each framework gets SKILL.md + MCP config +
  enforcement snippet; Claude Code also includes idempotent install /
  uninstall scripts.
- IBM Docling integration for PDF / DOCX / HTML / MD / PPTX / TXT
  import (`tools/docling_import.py`).
- Generic bulk-import helper (`tools/memory_import.py`).
- MCP tools for DAG versioning and the model optimizer (stdio MCP server).

### Security posture

- Tamper-evident SHA-256 hash chain on every consultation.
  `audit/verify` walks the chain from genesis; rate-limited 5/min,
  `audit` list 30/min.
- Consultation row + audit entry + memory refs commit atomically.
  Audit-write failure aborts the consultation with 503.
- Webhook URL validation blocks loopback, RFC1918 private, link-local,
  multicast, reserved, cloud-metadata endpoints (Google / AWS / Azure /
  Alibaba / Tencent / IPv6 variants). Async DNS resolution so a slow
  resolver can't freeze the ASGI worker.
- Webhook payloads HMAC-SHA256 signed per subscription. Delivery log
  retained after soft-delete for audit.
- OAuth cookie `Secure` flag honours `X-Forwarded-Proto` behind a
  trusted proxy (`OAUTH_TRUST_PROXY=true`). Sessions DB-backed,
  revocable.
- OAuth account-linking requires `email_verified=true` from the
  provider (strict — the string `"false"` does not count as verified).
- DAG merge wrapped in a single transaction held under
  `pg_advisory_xact_lock` keyed on `(memory_id, target_branch)` so
  concurrent merges cannot produce orphan commits or duplicate version
  numbers.
- Memory `owner_id` / `namespace` override on create requires
  `role='root'`.
- Explicit `owner_id = $2` filter on memory PATCH / DELETE as
  defense-in-depth beyond RLS.

### License

Apache License 2.0 — see [`LICENSE`](./LICENSE). Contributions accepted
under the Developer Certificate of Origin (DCO), see
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

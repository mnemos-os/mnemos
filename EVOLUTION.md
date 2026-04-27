# Evolution of MNEMOS

The version numbers on releases are tidy. The actual path was not. This
document is the honest version — decisions, refactors, mistakes, and the
reasoning that got us from the original prototype to the current v3.5-dev branch. If you
are considering MNEMOS for your own stack, you should know where it came
from; if you are contributing, you should know which doors have been closed
and why.

Written in April 2026 for the first public release. Maintained going
forward as architecture changes land.

---

## Development timeline

The version sections below tell the story in detail. This is the
five-month at-a-glance — when each piece landed, and what shape the
codebase took along the way.

| Date | Milestone |
|---|---|
| 2025-11 | Pre-history. A shell script (`auto_hydrate.sh`) + ChromaDB on a Mac Studio Ultra serving ~1,808 conversation chunks exported from prior Claude sessions. The thing was called *Rehydrator*. No FastAPI, no Postgres, no graph. |
| 2025-12-03 | Design review (`DESIGN_REVIEW_FOR_GPT_GEMINI.md`) names the failure mode in one line: *"vector search finds similar text, not connected meaning."* The graph-aware design starts here. |
| 2025-12 | **v1.0.0** — single-file FastAPI server. PostgreSQL + pgvector chosen as the backing store on day one. GRAEAE runs as a separate service on port 5001. *Rehydrator* → **MNEMOS** rename (Mnemosyne, Titan of memory). Persistent, inspectable, attributable, operationally reliable — the non-negotiables. |
| 2026-01 | Multi-provider GRAEAE consensus design + first eight provider integrations (Mistral, Qwen, DeepSeek, Gemini, OpenAI, Groq, Perplexity, Together). Per-provider circuit breaker prototype. The single-file server starts hitting its maintainability ceiling around 2,000 lines. |
| 2026-02 | **v2.0.0** — single-file → `api/handlers/` package refactor. `asyncpg` replaces `psycopg2`; connection pooling lands (`min=5, max=20`, never revisited). Real pgvector semantic search replaces grep-in-Python. Per-provider circuit breakers (CLOSED / OPEN / HALF_OPEN, 5-minute cooldown) ship. |
| 2026-03 | Knowledge graph schema design — subject / predicate / object with temporal validity windows. Memory versioning trigger drafted. Cryptographic audit chain (SHA-256) prototyped. Multi-user RLS foundation via PostgreSQL session variables. The first attempt at the column-naming convention that the v2.3 release would later force a five-bug cleanup of. |
| 2026-04-12 | **v2.3.0** — knowledge graph (`/kg/triples`, `/kg/timeline/{subject}`), journal, key-value state, entity tracking, model registry, LETHE compression (Tier 1, CPU), full audit chain, multi-user RLS, memory versioning with diff/revert. The painful release — five `created_at` column bugs, missing `kg_triples` migration on fresh installs, FK type mismatch (`UUID` vs `TEXT`) caught at first insert, port 5000 → 5002 move. |
| 2026-04-19 | **v2.4.0** (consolidation, not public) — OpenAI-compatible gateway (`/v1/chat/completions`, `/v1/models`) with optional memory injection. Stateful session management (`/sessions/*`). DAG versioning — git-like branch / merge / revert on memory history. MOIRAI compression triad (LETHE / ALETHEIA / ANAMNESIS) with quality manifests on every transformation. The shape of v3 is locked. |
| 2026-04-22 | **v3.0.0-beta** — MNEMOS + GRAEAE unify on port 5002. Webhooks (HMAC-SHA256, SSRF defense, durable retry log). OAuth / OIDC (Google / GitHub / Azure AD / generic) with `email_verified` cross-provider linking guard. Cross-instance federation with `permission_mode` per-memory opt-in and `federation_source` loop prevention. Self-maintaining model registry (`provider_sync` daily, Arena.ai Elo quarterly). Per-owner multi-tenant scoping on memories / consultations / state / journal / entities. Atomic consultation persistence under `pg_advisory_xact_lock`. Twenty-two FK edges with explicit `ON DELETE`. Two-pass release-gate audit (self + Codex re-audit) catches five P0 issues including a hardcoded production bearer in the Docling export tool. |
| 2026-04-22 | OpenClaw PR #70224 merged — first upstream contribution from the MNEMOS testing surface. The "we give as well as take" principle starts being load-bearing instead of aspirational. |
| 2026-04-23 | **v3.1.0** — Plugin `CompressionEngine` ABC + competitive contest framework + persisted audit log across three built-in engines. GPU circuit breaker. Admin enqueue endpoints. First real benchmark on 49 PYTHIA memories with `gemma-4-E4B` as judge on CERBERUS reveals ALETHEIA scored 0/49 contest wins — its index-list prompt doesn't survive instruction-tuned generalist LLMs. ALETHEIA retired from default contest the same day. Benchmark write-up: `docs/benchmarks/compression-2026-04-23.md`. |
| 2026-04-23 | Federation → fault-tolerance pivot. The cross-instance feed model from v3.0.0-beta turns out to be the wrong primitive for a single operator running multiple boxes; what was actually needed was HA. Move to `pg_auto_failover` (TYPHON monitor, PYTHIA primary, CERBERUS standby). ARGONAS reconciled against public master via 156-commit Codex triage (6 KEEP / 137 SKIP / 13 DROP). |
| 2026-04-24 | **v3.2.x bug-fix tail** — APOLLO LLM-fallback emits a startup warning when GPU isn't reachable; flag flipped off in PYTHIA prod after the 49-mem run showed 4.4% win-rate didn't justify the GPU cost. Apollo schema false-positive guards across five schemas (commit / code / decision / event / person). Artemis labeled-block assembly + sentence-span tracking rewrite. Scoring math fix in the contest (multiplicative profile-weight constants → exponentiated weights, so winner ordering actually depends on profile). 654/654 tests pass on CERBERUS. |
| 2026-04-24 | **v3.3.0-alpha** — MORPHEUS dream-state subsystem slice 1 lands (begin / finish / rollback runs, replay phase real, cluster + synthesise as stubs). MCP HTTP/SSE bridge (`mcp_http_server.py`) for ChatGPT Pro Developer Mode + any remote MCP client that needs an HTTPS URL. KNOSSOS / CHARON positioning as *gifts to other memory systems* (MemPalace, Mem0, Letta, Graphiti, Cognee) with concrete upstream-PR commitments. The compression stack settles to two engines (APOLLO + ARTEMIS); LETHE / ANAMNESIS / ALETHEIA become evolutionary history. `EVOLUTION.md`, `ROADMAP.md`, and `docs/connectors/` written for the first public release. |
| 2026-04-26 | **v3.3.0 / v3.4.0** — MORPHEUS slice 2 fills in real cluster + synthesise, recall tracking, cluster introspection, and namespace scoping. CHARON v0.2 turns MPF into a sidecar-capable portability surface for KG triples, documents, facts, events, compression manifests, and memory-version DAGs. The v3.4 audit work spends forty-four Codex rounds on sidecar attachment and restore correctness. |
| 2026-04-26 | **v3.4.1** — federation schema-compat preflight lands before peers sync. Strict peers refuse schema mismatch with HTTP 409 unless an operator opts into `compat_mode=permissive`. The dev↔prod MPF restore drill is written and validated on PYTHIA → PROTEUS data. |
| 2026-04-26 | **v3.5-dev slice 1** — audit quick wins: session history returns the newest rows instead of the oldest, system rows are pinned/capped deterministically, and project URLs move to `mnemos-os/mnemos` (`a62a099`). |
| 2026-04-26 | **v3.5-dev slice 2** — memory-read tenancy and DAG-integrity hardening (`d42c475`). Shared read visibility moves into `api/visibility.py`; version history becomes per-snapshot visible; merge/revert/branch writers serialize around branch heads; the v3.5 trigger raises `MN001` rather than accepting missing, NULL, or cross-memory branch heads. |

Roughly five months. One developer with three reviewers (Codex, GRAEAE
multi-LLM consensus, occasional Sonnet / Opus passes for design). Several
rounds of audit-driven rework — every major surface in v3.0.0-beta has had
its seams moved at least once, on evidence rather than vibes. The point of
the timeline is not the version count; it's that nothing here is a
two-week-sprint prototype, and every architectural seam has paid for
itself in a real failure mode at least once.

---

## v1.0.0 — December 2025 — "a file that doesn't disappear"

### The story that started it all

In late November / early December 2025, MNEMOS did not exist yet. What did exist was a shell script called `auto_hydrate.sh`, a Raspberry Pi on my home lab running an HTTP API that served roughly two dozen JSON "memory shards", and a Mac Studio Ultra running ChromaDB over 1,808 conversation chunks exported from prior Claude sessions. The whole thing was called **Rehydrator**. The MNEMOS name came later.

The catalyzing moment was one question typed into a Claude Code session:

> *"Who is Lonnie and what did he promise me?"*

Lonnie is a friend of mine who had promised to send me some equipment. That promise had been discussed across half a dozen prior sessions, was sitting in the ChromaDB vector store fully intact, and still took **more than fifteen semantic searches** to piece back together into a single answer:

- *"friend"* → found fragments.
- *"promised"* → more fragments.
- *"equipment"* → connected the promise.
- *"going to send"* → finally reconstructed the story.

It worked, eventually. It should not have taken fifteen searches to answer a question about a person. The problem was not retrieval quality — the vectors were fine. The problem was that vector search finds similar *text*, and my question was about connected *meaning*: **Lonnie → is friend of → me → promised → equipment**. A flat chunked vector store has no way to traverse a graph like that. You can only hope the right fragments happen to be semantically nearby, and pray the re-assembly happens upstream in the model.

The written design review from December 3, 2025 (`DESIGN_REVIEW_FOR_GPT_GEMINI.md` in my notes) captured it in one line:

> *"Vector search finds similar TEXT, not connected MEANING. Can't traverse relationships like 'Lonnie → is friend of → Jason → promised → equipment'."*

That one observation is the reason almost everything downstream exists. It's why v2.3 shipped with a knowledge-graph API (`/kg/triples`, `/kg/timeline/{subject}`) that stores subject → predicate → object triples with temporal validity windows alongside the vector memories. It's why v3.0 ships per-owner scoping on memories, consultations, state, and entities — because *"who"* and *"what's true of them"* and *"who said what to whom, when"* are not answered by similarity alone. It's why, when I eventually reached for a proper name, the Greek goddess of memory ended up feeling right: what I was building was not a database and not a cache; it was something that was supposed to remember *the way humans remember*, which is always relational.

The name **Rehydrator** described the action: at the start of a new Claude session, a shell script would pull memory shards from the Pi and inject them into the system prompt, "re-hydrating" a process that had come back up empty. The action name was accurate but too mechanical for what the thing actually was becoming. When the second major refactor happened, the project got its proper name — **MNEMOS**, short for Mnemosyne, the Titan of memory — and *Rehydrator* became the name of the subsystem that still does the startup-time context injection. The action got renamed; the system finally got its own name.

What followed from that December 3 session has been the non-negotiable design commitment MNEMOS has carried through every subsequent version: **memory that is persistent, inspectable, attributable, and operationally reliable — not just convenient in a demo.**

### What actually shipped in v1.0

A single-file FastAPI server with a PostgreSQL + pgvector backend.
GRAEAE ran as a separate service on port 5001; memory and reasoning
were two processes that didn't know much about each other. The primary
goal was the simplest thing that could survive a process restart.

**Key design choices that survived into v3:**

- **PostgreSQL as the backing store, not ChromaDB or SQLite.** Made on the
  first day and never revisited. ACID transactions, real foreign keys,
  real operational tooling. Every "toy" vector store we compared against
  has since been surpassed in capability by the pgvector extension inside
  a real RDBMS.
- **Separate reasoning layer (GRAEAE).** The choice to not bolt consensus
  reasoning onto the memory store, but to run it as its own service with
  its own auth and lifecycle, predates the merger on port 5002 — they
  stayed architecturally separate even after they lived in one process.
- **Memory is a first-class resource with an owner, a category, and a
  provenance**, not a blob with metadata. Every later subsystem —
  versioning, federation, compression, multi-tenancy — was possible
  because this was decided at the beginning.

**What v1.0 couldn't do:**

- No versioning — a memory write was destructive.
- No compression — store text, retrieve text.
- No multi-tenant isolation — every caller shared one bucket.
- No audit trail on reasoning.
- No federation, no webhooks, no OAuth.
- No migrations runner — installs were `psql < schema.sql`.

**Operational shape:** single-user, single-machine, bare-metal. Good
enough to prove the thesis; obviously not good enough to ship.

---

## v2.0.0 — February 2026 — "make it a service, not a script"

**The refactor:** single-file `api_server.py` became the `api/handlers/`
package. `asyncpg` replaced `psycopg2`. Connection pooling arrived.
pgvector-based semantic search moved from "grep in Python" to real vector
search. GRAEAE acquired multi-provider consensus (replacing the single
Mistral-7B fallback) with per-provider quality scoring.

**Why this refactor happened at all:** the single-file server became
unmaintainable around 2,000 lines. Testing was impossible; every bug fix
risked a regression in a module-flavored function three pages away.
`api/handlers/` was the smallest refactor that restored the ability to
reason locally about each domain.

**Architectural decisions committed here:**

- **Handler-per-domain file layout** (memories, consultations, providers,
  sessions, etc.) with a shared `api/lifecycle.py` for DB pool / cache /
  worker registration. Still the shape at v3.
- **`asyncpg` connection pooling** with `min=5, max=20` per worker. These
  defaults have never been revisited and have never needed to be.
- **Per-provider circuit breakers** in GRAEAE — CLOSED / OPEN / HALF_OPEN
  with 5-minute cooldown. Built because one provider's bad afternoon
  was repeatedly killing consultation throughput.

**Mistakes made here:**

- `memories.id` was declared `UUID` at v2. We had to migrate it to `TEXT`
  later when the `mem_xxxxxxxxxxxx` prefix convention showed up in v2.3,
  and the migration caused foreign-key ripples across four dependent
  tables. Lesson: if you're going to prefix your ids with a type tag,
  don't also require them to be UUIDs.
- Backward-compat aliases (`/graeae/*`, `/memories/*`, `/model-registry/*`)
  were *claimed* in the v2→v3 migration story but never actually
  implemented as aliases. The CHANGELOG carried the claim for weeks
  before a release-gate audit caught it and removed it. Lesson: don't
  document promises the code hasn't kept.

---

## v2.3.0 — April 12, 2026 — "memory starts to act like a substrate"

The release where MNEMOS stopped looking like a memory API and started
looking like a substrate. This is also the release where the most
migration pain lived.

**New surface:**

- **Knowledge graph** — temporal triples (subject → predicate → object
  with `valid_from` / `valid_until`), `/kg/triples`, `/kg/timeline/{subject}`.
  First concession that pure embedding search wasn't the whole story.
- **Journal** (`/journal`) — date-partitioned operational log.
- **Key-value state** (`/state/{key}`) — persistent session state store.
- **Entity tracking** (`/entities`) — people, projects, concepts with
  bidirectional links.
- **Model registry** (`/model-registry/`) — the first version of the
  self-maintaining provider catalog. This predates the Arena.ai Elo sync;
  at this point the registry was just a typed catalog of what each
  provider had said was available.
- **LETHE compression (Tier 1, CPU)** — unified the two earlier
  experiments (HyCoLL and SAC) into a single module with two modes:
  `token` (the old HyCoLL) and `sentence` (the old SAC). Both names
  survived as `was HyCoLL` / `was SAC` annotations in source for
  traceability. Eventually the public rename to LETHE was the right call
  but it broke every internal doc and integration that referenced the
  old names — budget a release for that kind of rename.
- **Cryptographic audit chain** — SHA-256-hashed consultation log.
  Earliest version of what's now the tamper-evident hash chain.
- **Multi-user RLS** via PostgreSQL session variables. Put the foundation
  in place for what v3 would extend into full per-owner scoping.
- **Memory versioning with diff and revert** — the `memory_versions` table
  with a trigger that auto-snapshots on every mutation. This was the
  foundation the v3 DAG extended.

**Mistakes made here (this was the painful release):**

- Five `created_at` → `created` column reference bugs across migrations
  and views. A minor column-name inconsistency shipped, got replicated
  across dependent views, and each one was found separately over the
  course of three days. Lesson: pick one column-naming convention at
  migration time and enforce it with a lint rule, not with reviewer
  attention.
- `kg_triples` table was missing from migrations on fresh installs. The
  KG API 500'd on anyone who tried it on a clean DB. Nobody caught it
  because every developer's DB had the table from the dev branch.
  Lesson: "fresh install CI" is not optional for a multi-table system.
- `compression_quality_log.memory_id` was UUID but `memories.id` was
  now TEXT — a type mismatch that was declared via FK and then silently
  tolerated by PostgreSQL until the first real insert. Lesson: if you
  rename a column's type across a version boundary, grep every FK that
  targets it.
- `CREATE EXTENSION vector/pgcrypto` was not sequenced before the tables
  that used those types. Fresh installs that weren't already bootstrapped
  failed. Lesson: extensions belong at the top of the very first
  migration, and migration-runner ordering is load-bearing.
- `distillation_worker.py` crashed on reconnect because `self.db` was
  referenced where `self.db_pool` existed. Simple typo, lived for a
  quarter of the v2 lifetime before surfacing at an inconvenient moment.
- Port 5000 → 5002 move. MNEMOS (5000) and GRAEAE (5001) were split
  across two ports since v1. 2.3 moved MNEMOS to 5002 in preparation
  for the v3 unification. Any operator with an `API_URL` env var set
  to `:5000` silently broke. Lesson: ports are contracts; treat moves
  like API breaking changes.

**Architectural decision committed here, in retrospect too late:** the
decision to keep GRAEAE on its own port, even though both services were
now on the same machine. This would be reversed in v3. The v2.3 split
introduced one real failure mode: the two services could disagree about
whether PostgreSQL was reachable, depending on which pool was healthy.

---

## v2.4.0 — April 19, 2026 — "the unification baseline"

Not a release so much as a consolidation point. v2.4 was the development
baseline from which v3.0 was cut; it's numbered in the CHANGELOG for
traceability but did not ship as a public version.

**What landed in the working tree here:**

- **OpenAI-compatible gateway** (`/v1/chat/completions`, `/v1/models`) with
  automatic provider routing and optional memory injection. The decision
  to expose OpenAI-shape endpoints on top of GRAEAE routing was taken
  here. It turned out to be the single biggest lever for adoption — any
  team already using OpenAI SDKs could switch to MNEMOS by changing
  `base_url`.
- **Stateful session management** (`/sessions/*`) — multi-turn state with
  memory injection at turn boundaries.
- **DAG versioning** — full git-like operations (branch, merge, revert)
  on memory history. Extended the v2.3 `memory_versions` table with
  `commit_hash`, `parent_version_id`, `memory_branches`.
- **THE MOIRAI compression triad** — LETHE / ALETHEIA / ANAMNESIS as the
  formal three-tier compression subsystem with a written quality
  manifest on every transformation. The manifest was the decisive change:
  compression-as-data rather than compression-as-side-effect. Every
  compressed memory now has a receipt explaining what was removed, what
  was preserved, and which downstream uses remain safe.
- **Distillation worker lifecycle integration** — the background worker
  became a real lifespan-tracked task rather than a separately-managed
  process.

v2.4 is where the surface of v3 took its final shape. Everything from
v2.4 ships in v3 without modification; the difference is unification and
security hardening on top.

---

## v3.0.0-beta — April 22, 2026 — "one service, no aliases, production-grade"

The release. Everything between v2.4 and v3.0 is one of: unification,
security hardening, multi-tenant scoping, or honesty (removing claims
the code didn't back).

**Unification.** MNEMOS and GRAEAE became one FastAPI service on port
5002. Nine routers included in a single app. All primary routes
namespaced under `/v1/*`. The earlier backward-compat alias claim was
dropped from both docs and code — v3.0 is the first public release, so
there is no pre-v3 surface to be compatible with.

**Webhooks.** Durable outbound event delivery. HMAC-SHA256 signed. Retry
log replayed on restart via a recovery worker. Soft-delete of
subscriptions retains the delivery log for audit. The SSRF defense on
webhook URLs was the largest single security addition between v2.4 and
v3.0 — it was not in v2.4.

**OAuth / OIDC.** Browser login via Google / GitHub / Azure AD / generic
OIDC. DB-backed sessions with hourly GC. Coexists with Bearer API keys.
The `email_verified` guard for cross-provider account linking was added
specifically because a first draft of the OAuth path would have let a
permissive identity provider claim existing accounts by email match.

**Cross-instance federation.** Pull-based sync. Per-memory opt-in via
`permission_mode` (others-read bit). Loop-prevention via
`federation_source`. The `permission_mode` gate was added late — the
first version of the feed endpoint served every non-federation-sourced
memory, which in retrospect was one configuration error away from a
full tenant leak.

**Self-maintaining model registry.** `provider_sync` against each
provider's `/v1/models` daily. `refresh_elo_weights.py` against the
Arena.ai leaderboard quarterly via systemd timer. `graeae_weight`
(0.50–1.00) feeds the consensus scorer. This replaced the previous
hand-maintained provider list, which had gone stale twice within a
single release cycle.

**Per-owner multi-tenant scoping** on memories, consultations, state,
journal, entities. A `migrations_v3_ownership.sql` migration backfills
`owner_id` columns with the string `'default'` for existing rows and
re-keys the affected uniqueness constraints. Pre-v3 these tables were
globally shared across users — fine for single-user installs, silently
unsafe for multi-user ones.

**Atomic consultation persistence.** Consultation row, audit entry, and
memory references commit in a single transaction held under
`pg_advisory_xact_lock`. Pre-v3, these were three separate transactions;
a crash between them could leave the consultation visible without an
audit entry. Not good for a tamper-evident chain.

**Referential integrity pass.** Every cross-table reference is now a
real FK with an explicit `ON DELETE` semantic (CASCADE or SET NULL,
chosen per edge based on whether the target owns the source's lifecycle
or whether audit has to survive deletion). Twenty-two FK edges.

**Docs / packaging / install story.**

- Installer CLI (`mnemos-install`) now lands on `PATH` via
  `[project.scripts]`. You can `pip install mnemos-os` and run it.
- All 11 SQL migrations ship as `db/*.sql` package data.
- `MANIFEST.in` added so the sdist carries `config.toml.example`,
  `mnemos.service`, `Dockerfile`, `docker-compose.yml`, integrations
  bundle, tests, and docs.
- CI moved from `pip install -e .[dev]` to `uv venv + uv pip install`
  so the CI path matches the supported user workflow.
- Twenty-plus internal dev artifacts (test plans, release checklists,
  ops runbooks) removed from the tree entirely. They did not belong in
  a public repo. One of them, `EXECUTION_WITH_SYSTEM_LOCATIONS.md`,
  carried plaintext SSH passwords — caught during the release-gate
  scrub, not during the feature work. Lesson: ops runbooks and OSS
  codebases must not share a git repo.

**Mistakes found during the v3 release-gate audits (multiple passes):**

Two independent audit passes — self + Codex re-audits — caught work the
original v3 PRs had claimed but not delivered. The ones worth remembering:

- The `created AS created_at` SQL alias that shipped in consultation
  handlers matched the real database but did not match the test harness's
  substring-based mock matcher. Tests passed, production failed. Fixed
  by moving the column rename to the Python response-builder layer and
  keeping the SQL honest to the column name.
- The FakePool owner-scoped query branch was ordered after the
  non-scoped substring branch. Substring matching on SQL is not
  composable; the shorter pattern silently won. Tests claimed owner
  scoping worked; the test harness wasn't actually exercising it.
- A production bearer token was hardcoded at module scope in the
  Docling export tool. Not in main code paths, but it was checked in
  and would have shipped publicly. Caught during the P0 credentials
  scrub.
- The webhook SSRF validator was initially added at *create* time only.
  A subscription URL set outside the handler (direct DB write during
  migration) would bypass the validator at dispatch time. Fixed by
  calling the validator a second time in the dispatcher, with a
  docstring admission that a DNS-rebinding window still exists and
  requires host-pinning to fully close.
- The audit chain lock was held only around the audit INSERT, not
  around the surrounding consultation + memory-refs writes. A crash
  mid-sequence could produce a consultation row without an audit entry.
  The fix widens the lock and aborts the consultation if the audit
  write fails — a visible capacity cost under load, in exchange for
  correctness that the tamper-evident story actually requires.

**Naming scrubs:**

- HyCoLL → LETHE (token mode) and SAC → LETHE (sentence mode). The
  internal acronyms had never been good public names; the Greek names
  are what the compression tiers will be called going forward.
- Internal-infrastructure hostnames (PYTHIA, CERBERUS, PROTEUS,
  ARGONAS) and IP literals scrubbed from all public docs and code
  paths. See `GPU_PROVIDER_HOST` for the generic replacement.
- The MemPalace comparison section was initially a takedown (it led
  with a benchmark critique). Rewritten before release to the current
  *"MemPalace and MNEMOS: different problems, not competitors"* framing.
  The original was accurate on the narrow technical points but wrong
  in spirit for a first-public-release document. Competitors deserve
  respect; we would want the same.

---

## v3.2 tail — April 2026 — "the compression stack settles to two"

The MOIRAI triad shipped in v3.1 (LETHE / ALETHEIA / ANAMNESIS) didn't
survive its first real benchmark. That benchmark — 49 memories from
PYTHIA's actual corpus, run through gemma-4-E4B as the judge on
CERBERUS — surfaced concrete problems with each engine that the
unit-test suite hadn't caught:

- **ALETHEIA** scored 0 contest wins. Its index-list prompt
  ("return the indices of the sentences to keep") doesn't survive
  instruction-tuned generalist LLMs; modern Gemma / Llama / Qwen
  flavors paraphrase the input rather than emitting an index list.
  Retired from the default contest 2026-04-23. The benchmark write-up
  is in `docs/benchmarks/compression-2026-04-23.md`.
- **LETHE** worked, but its extractive path missed the structured-
  span treatment (protected identifiers, labeled blocks like
  `**Field**: value`, code fences) that real prose memories carry.
- **ANAMNESIS** worked, but its role — LLM-driven fact extraction —
  was already subsumable by APOLLO's LLM fallback path: when APOLLO
  doesn't recognize a schema, it falls back to "generic fact
  extraction with identifier preservation," which is what ANAMNESIS
  was. Two engines were doing the same job.

The settlement, landed in the v3.2 tail:

- **APOLLO** (gpu_optional) — schema-aware dense encoding for
  LLM-to-LLM wire use. Portfolio, decision, person, event, code, and
  commit schemas as of v3.2.4. LLM-fallback path covers anything that
  doesn't match a schema.
- **ARTEMIS** (cpu_only) — extractive with identifier preservation,
  labeled-block handling, evidence-based self-scoring. Replaces
  LETHE in the going-forward stack.
- **The contest framework itself** stays untouched. Adding APOLLO
  and ARTEMIS, retiring the others, was a registration change in
  `compression/manager.py` plus a benchmark-driven decision recorded
  in the manifest. No platform churn.

The takeaway for contributors: compression engines are not features
operators register against; they are architectural choices we
revisit when benchmark evidence demands. The contest's value is
exactly that we can evaluate, retire, and replace engines without
breaking the platform around them. LETHE / ANAMNESIS / ALETHEIA
remain in the codebase as evolutionary history — see
`compression/{lethe,anamnesis,aletheia}.py` for what each was —
but the going-forward names are APOLLO and ARTEMIS. The pantheon
moved on.

When MORPHEUS (v3.3+) and PERSEPHONE (v3.6+) land, the same
naming convention extends: each subsystem is a Greek name that
maps to its function. Engines that turn out to be wrong don't get
renamed; they get retired and the history gets recorded here.

---

## v3.4.1 and v3.5-dev — April 2026 — "the branch learns to distrust its own history"

v3.4.1 was the federation-safety release. CHARON v0.2 already made
memory portable across systems, but portability created a new problem:
two peers can both speak MPF and still be on different schema shapes.
So v3.4.1 added schema fingerprints before sync. Strict peers now
compare `mnemos_version`, `schema_signature`, and the migrations
fingerprint before opening a pull. A mismatch returns 409 unless an
operator deliberately flips the peer to permissive mode.

That same release wrote down the restore drill instead of trusting the
happy path. The drill matters because `memory_versions` and
`memory_branches` intentionally preserve history across memory deletion;
cleanup still needs explicit branch/version deletes. `docs/RESTORE-DRILL.md`
is the runbook for doing that without polluting a target node after a
test import.

The first v3.5 branch slice was small and worth doing early. Session
history had a classic ordering bug: "give me 10 history rows" returned
the oldest rows, then later code tried to reason as if it had the recent
context. The fix (`f9ea8d9` through `e3c884c`, merged as `a62a099`)
made the order descending, pinned system rows deterministically, and
kept the repo metadata aligned to `mnemos-os/mnemos`.

The second slice was larger because the bugs were about trust
boundaries, not syntax. The important lesson was that a memory's live
row and its historical snapshots are different authorization objects.
If a memory was private at version 1 and public at version 2, a reader
allowed to see version 2 is not automatically allowed to see version 1.
That became `version_visibility_predicate` in `api/visibility.py:99-137`.
The live-memory read contract moved next to it as
`read_visibility_predicate` in `api/visibility.py:40-96`, with owner,
federation, world-readable, and Unix group-readable branches spelled
out in one place.

The same pass tightened the DAG. Logs no longer invent a clean path
through hidden snapshots; `parent_hash` is emitted only when the actual
immediate parent is visible. Recursive walks and parent-hash joins are
scoped by `memory_id`, so a corrupt `parent_version_id` cannot pull a
different memory's version into the graph. Branch creation now locks
the parent memory row, resolves the start commit after that lock, and
uses `INSERT ... ON CONFLICT DO NOTHING RETURNING` for duplicate-race
handling. Merge and feature-branch revert share the same advisory lock
key (`api/handlers/dag.py:21-40`) and the same lock order.

The database had to participate. The schema's FK can prove that
`memory_branches.head_version_id` points at an existing version; it
cannot prove that the version belongs to the same memory. The v3.5
trigger replacement (`db/migrations_v3_5_trigger_same_memory_parent.sql`)
does that check under `FOR UPDATE OF mb`, fails closed on missing or
NULL branch heads, raises SQLSTATE `MN001` for foreign heads, and lets
`handle_trigger_pgerror` translate the condition to HTTP 409 with an
operator reconciliation message.

The final documented mismatch is closed by
`db/migrations_v3_5_rls_group_select_unix_bits.sql`: the
`mnemos_group_select` RLS policy and application predicate now both
test the Unix group-read bit directly with
`((permission_mode / 10) % 10) >= 4`.

---

## Architectural decisions made during the release-gate audit pass

The release-gate pass caught two claims in the README that needed firm
architectural calls, not just wording fixes. Recording them here so the
reasoning survives:

### ADR-01: Response cache stays exact-match, not semantic

- **Context.** The README described the GRAEAE consultation cache as
  "embedding-similarity deduplication". The code (`graeae/_cache.py`)
  is an LRU keyed on `sha256(task_type + normalized_prompt)`, with a
  comment explicitly saying semantic similarity is *not* used because
  the embedding round-trip negates the win for near-duplicate cases.
- **Decision.** Keep the exact-match implementation. Update the docs to
  describe what the code actually does. Do not ship semantic / embedding
  similarity lookup in v3.0.0-beta.
- **Rationale.**
  - Embedding lookup cost (~5–20 ms for nomic-embed-text / BGE) against
    a provider round-trip (~500–2000 ms) is ~1% — cheap, not free.
  - Agent workflows in practice repeat exactly-the-same constructed
    prompts (system prompt + context block + templated question), so
    exact-match on the normalized form plausibly catches most realistic
    hits without added complexity.
  - Aspirational docs that the code doesn't back is the exact class of
    claim the audit pass was designed to catch. Calling it what it is
    keeps the release honest.
- **Upgrade trigger.** If instrumentation shows cache hit rate below
  ~15% under real load, re-evaluate semantic lookup with a live
  comparison of latency-saved-per-call.
- **Reversibility.** The cache sits behind `ResponseCache.get` /
  `.set`. Swapping the key function from `sha256(normalized)` to
  `embedding_lookup(normalized)` is a localized change, not a schema
  change. This is a cheap decision to revisit.

### ADR-02: Distillation worker keeps the supervisor-wrapper pattern

- **Context.** Two files, one feature — `distillation_worker.py` is the
  worker class; `api/lifecycle.py::_run_distillation_worker` is the
  supervisor that wraps it with exponential-backoff restart. An
  auditor grep for "backoff" in the worker class came up empty and
  suggested the feature was missing. It wasn't; it was in the wrapper.
- **Decision.** Keep the two-file separation. Worker knows how to do
  the work. Supervisor knows how to keep the worker alive. Two concerns,
  two files.
- **Rationale.** Conflating work and supervision inside a single class
  is the pattern that produces workers that "never die because we can't
  tell whether they died". Moving supervision out — to something that
  is literally a `while True` loop with a `try/except Exception` and a
  backoff counter — is what lets the worker class stay small and
  testable.
- **Mitigation for the audit confusion.** A docstring cross-reference
  in `distillation_worker.py` pointing at the supervisor, so the next
  person to grep finds both files without having to read this doc.

---

## Architectural decisions that held up

For anyone reading this to understand why MNEMOS is shaped the way it
is, the list of non-obvious calls that proved out:

1. **PostgreSQL over anything else.** Every alternative memory system we
   looked at had eventually regretted its SQLite / ChromaDB / duckdb
   foundation. We have not.
2. **Treat reasoning as its own named subsystem** (GRAEAE) rather than
   "MNEMOS plus an LLM call". This made the audit chain possible, the
   circuit breakers possible, the Arena.ai weighting possible. A
   reasoning-as-a-feature design would have had to bolt all of that
   onto an existing call site.
3. **Compression gets a receipt.** The quality manifest turned out to
   be the single most operator-valued feature for audit-sensitive
   users. No other memory system treats compression as something that
   requires documentation.
4. **Greek names are subsystem tags, not theming.** Every Greek name in
   the source tree maps to a real subsystem. When we rebranded HyCoLL
   → LETHE, the change was load-bearing; when we added ANAMNESIS as
   Tier 3, it had to be its own file, its own code path, its own
   failure mode. The naming does work.
5. **Referential integrity at the database layer, not the app layer.**
   The FK edges and their `ON DELETE` choices are the spine of the
   data model. Applications pass through; the constraint stays.

## Architectural decisions we would revisit

Equally important — the calls that were correct at the time but would
be made differently today:

1. **Single-writer assumption.** The app currently requires `workers=1`
   because the circuit breakers, rate limiters, and semaphores are
   in-process state. A v3.1+ move to a shared state backend (Redis or
   the DB itself) would let us scale horizontally. Today it's a scale
   ceiling we accept.
2. **OAuth state via Starlette `SessionMiddleware`.** Works, but the
   signing key regenerates on restart if `MNEMOS_SESSION_SECRET` is
   unset. We warn about it loudly; we should probably refuse to start
   instead.
3. **SQL migrations as sequential files rather than a migration tool.**
   Worked at v1 when there was one file. At v3 with eleven ordered
   files, we should probably move to Alembic or sqitch. The current
   ordering is documented in `install.py` and `installer/db.py` but
   that documentation is two places that must stay in sync.
4. **FakePool in the test harness.** Substring-match-on-SQL is
   fundamentally the wrong abstraction; it silently matches shorter
   patterns as prefixes of longer ones and we've been bitten by that
   twice. The v3.1+ path is either a real postgres container in CI or
   a proper SQL parser-based mock. Today's harness is pragmatic but
   not sound.

---

## For contributors

If you're here to contribute and want to understand the history:

- Read `CHANGELOG.md` for what shipped.
- Read this file for why, and what almost shipped and didn't.
- The biggest "don't touch this without understanding why" areas are:
  - **The audit chain lock** (`api/handlers/consultations.py::_write_audit_entry_on_conn`) — widening or narrowing the lock window changes both correctness and throughput. Don't adjust casually.
  - **The FK edge ON-DELETE choices** (`db/migrations_*.sql`) — each one is the result of a real design conversation about whether history survives deletion.
  - **The SSRF validator** (`api/handlers/webhooks.py::validate_webhook_url`) — the block list and the async-resolve path have both been tightened in response to real concerns. Loosen carefully.
  - **The Greek names.** They look like whimsy from outside. From inside they are subsystem labels that appear in logs, tables, migrations, and code paths. Renaming one is a distributed refactor.

---

*This is a living document. If a major architectural decision lands or
reverses after publication, it goes here. The point of writing it down
is that nobody should have to re-learn any of this by running into it in
production.*

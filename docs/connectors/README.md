# MNEMOS Connectors — experimental

> **Status: experimental.** This subsystem is published for power users and
> developers who want MNEMOS as a memory layer behind their existing agent
> tooling (Claude Desktop, Claude Code, ChatGPT Pro Developer Mode, Cursor,
> Codex CLI). Defaults are off; configuration is opt-in; surface area is
> intentionally narrow. APIs may change between minor releases without a
> deprecation cycle until the surface is promoted to `stable` in a later
> release. v4.0.0 keeps stdio/HTTP registry parity in the `mnemos.mcp`
> package, but broad remote connector packaging remains experimental.

## Audience

MNEMOS is a power-user / developer memory operating system. The connector
work makes its existing MCP surface usable from agent surfaces beyond
Claude Code (the original deployment target). It does not target
non-technical end users — that's a different problem space (see
`docs/positioning.md`).

If you fit this profile, the connectors are for you:

- You run MNEMOS yourself (homelab, dev box, NAS, cloud VM, or a fleet
  alongside the rest of your infra).
- You use multiple coding agents day-to-day (Claude, ChatGPT Pro,
  Cursor, Codex) and want them to share memory.
- You're comfortable with concepts like reverse tunnels, bearer auth,
  TLS termination, and editing config files.

If you're looking for a one-click consumer memory app: that's not what
MNEMOS is. We recommend [MemPalace](https://github.com/mempalace/mempalace)
for the local-first single-user Claude-Desktop experience; MNEMOS is
deliberately scaled differently. The two compose via the
[KNOSSOS bridge](../KNOSSOS.md) when you want both.

## Canonical MCP tool surface

Every connector below talks to the same MCP tool registry. Use
these EXACT names when building per-tool allow/deny lists in the
host agent's config (Cline ``autoApprove``, ChatGPT custom
connector permissions, etc.) — partial matches don't fire.

Source of truth: ``mnemos/mcp/tools/{memory,kg,dag,models}.py``.

**Read tools (safe to auto-approve on any key) — 10:**

| Tool                    | Surface  | Purpose                                  |
|-------------------------|----------|------------------------------------------|
| ``search_memories``     | memory   | Full-text + vector search                |
| ``list_memories``       | memory   | Paginated list, optionally scoped        |
| ``get_memory``          | memory   | Fetch by id                              |
| ``get_stats``           | memory   | Operator stats (counts, namespaces)      |
| ``kg_search``           | kg       | Subject/predicate/object KG search       |
| ``kg_timeline``         | kg       | Temporal KG query                        |
| ``log_memory``          | dag      | Per-memory version history               |
| ``diff_memory_commits`` | dag      | Diff between two commits                 |
| ``checkout_memory``     | dag      | Fetch a specific commit (read-only view) |
| ``recommend_model``     | models   | Provider/model catalog query             |

**Write tools (require approval; see per-connector guidance) — 8:**

| Tool                    | Surface  | Mutation shape                          |
|-------------------------|----------|------------------------------------------|
| ``create_memory``       | memory   | INSERT new row                           |
| ``update_memory``       | memory   | UPDATE existing row by id                |
| ``delete_memory``       | memory   | DELETE existing row by id (DAG tombstone)|
| ``bulk_create_memories``| memory   | INSERT N new rows                        |
| ``kg_create_triple``    | kg       | INSERT new KG triple                     |
| ``update_triple``       | kg       | UPDATE existing KG triple. **No ``kg_`` prefix in MCP registry.** |
| ``delete_triple``       | kg       | DELETE existing KG triple. **No ``kg_`` prefix in MCP registry.** |
| ``branch_memory``       | dag      | INSERT new branch on a memory's DAG      |

The ``kg_``-prefix asymmetry on ``update_triple`` / ``delete_triple``
vs the rest of the KG tools is a registry quirk — autoApprove /
deny-list configs match exact names, so listing ``kg_delete_triple``
does NOTHING while leaving ``delete_triple`` available.

The ``branch_memory`` DAG tool is a write — it creates a new
branch. ``checkout_memory`` and ``diff_memory_commits`` are
read-only despite the "DAG" naming.

The MCP server may add tools across releases; check
``/v1/mcp/discovery`` on a running instance OR
``mnemos serve mcp-stdio --print-schema`` for the live count.

**Important: the ``mnemos_`` UI prefix some agents add (e.g.,
Cursor's tool drawer shows ``mnemos_search_memories``) is
display-only.** The autoApprove / deny-list configs all match
EXACT registry names — strip the prefix when configuring.

## Surfaces supported

| Agent surface | Transport | Status | Notes |
|---|---|---|---|
| Claude Code | stdio MCP | ✅ stable | The original target; works out of the box |
| Claude Desktop | stdio MCP or HTTP/SSE | ✅ stable (stdio) / 🧪 experimental (HTTP) | Stdio for local; HTTP/SSE if you want the same MNEMOS to back multiple machines |
| Cursor | stdio MCP or HTTP/SSE | ✅ stable | Cursor's MCP support is mature |
| Codex CLI (OpenAI) | stdio MCP or HTTP/SSE | 🧪 experimental | Codex 0.125.0+ has MCP; we test against 0.126.0-alpha.1 |
| Continue.dev | stdio MCP or HTTP/SSE | 🧪 experimental | Continue v0.9+ has MCP support |
| Cline (formerly Claude Dev) | stdio MCP or HTTP/SSE | 🧪 experimental | VS Code extension, autonomous-edit loop. Cline v3.x |
| ChatGPT Pro Developer Mode (web) | HTTP/SSE | 🧪 experimental | Requires the Pro / Team / Enterprise / Edu tier with Developer Mode enabled, plus a public HTTPS URL pointing at your MNEMOS |
| ChatGPT consumer (free / Plus) | none | ❌ not supported | OpenAI hasn't broadened MCP to those tiers; no plan to ship a non-MCP shim for them |

## Quick start

### If you already have MNEMOS running locally and just want stdio MCP

For Claude Code, Claude Desktop, Cursor, or Codex CLI on the same
machine as MNEMOS — no tunnel needed, the agent spawns MNEMOS's MCP
server as a child process. See the per-surface guides:

- [Claude Code](./claude-code.md)
- [Claude Desktop](./claude-desktop.md)
- [Cursor](./cursor.md)
- [Codex CLI](./codex-cli.md)
- [Continue.dev](./continue-dev.md)
- [Cline (VS Code)](./cline.md)

### If you want ChatGPT Pro / Team to talk to your MNEMOS

ChatGPT's web app needs a public HTTPS URL — it can't spawn local
processes. You expose MNEMOS's MCP HTTP/SSE endpoint via a tunnel,
register it as a Custom Connector, paste the bearer token. See:

- [ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md) — full
  walkthrough including ngrok setup and the experimental
  `mnemos-tunnel-setup` helper script.

### Mobile / laptop tether to a home or SOHO MNEMOS

The v4 `edge` profile runs a single-tenant SQLite-backed MNEMOS locally and
tethers to your authoritative
MNEMOS on a server via federation. Same MCP surface, offline-tolerant,
conflict resolution via the existing version DAG. Power users can also use SSH
port-forwarding or Tailscale to point a local agent at a remote MNEMOS; the MCP
server does not care which transport delivers the bytes.

## Why we publish these as experimental

Three reasons:

1. **The remote-MCP story is new** in the broader ecosystem. ChatGPT
   Pro Developer Mode landed recently; Codex CLI's MCP shipped in
   0.125; Claude Desktop's HTTP transport is in flux. Anything we
   publish here may need changes when upstream surfaces stabilize.
2. **The audience is narrow on purpose**. We're not going to spend
   2026 building an installer-app for the consumer market — that's a
   different product with a different operations footprint. The
   connectors targeting that market (a hosted SaaS, a Tauri desktop
   app) are v5.0+ framing, not v4.0 scope. See `ROADMAP.md`.
3. **We are not trying to displace MemPalace, OpenWebUI, Mem0, Letta,
   Graphiti, or Cognee**. Each of those serves a real audience well.
   MNEMOS exists for users who outgrew them or whose workload —
   multi-tenant, production-data-rate, schema-extensible, audit-and-
   rollback grade — was never their target. Connector publication is
   about making MNEMOS easy to wire into the agent surfaces that
   people in our audience already use, not about market displacement.

## The pantheon gives gifts

The subsystem names are Greek on purpose. MNEMOS (memory itself, the
mother of the Muses), GRAEAE (the three grey sisters who shared one
eye — multi-LLM consensus across providers), APOLLO and ARTEMIS
(twin deities — the two active compression engines; APOLLO does
schema-aware dense encoding for LLM-to-LLM wire use, ARTEMIS does
CPU-only extractive compression with identifier preservation), CHARON (the
ferryman between worlds — memory portability across schemas),
KNOSSOS (the palace at Crete where Linear A/B tablets first
institutionalised writing-as-memory — the MemPalace-compatible MCP
shim), MORPHEUS (the god of dreams who shapes — the dream-state
synthesiser). Each name maps to a function. The convention isn't
decoration; it's how we keep the architecture's intent legible
across releases.

In the mythology, the gods give gifts. Prometheus brought fire.
Demeter brought grain. Athena brought olive cultivation and weaving.
Each was specific, each was useful, each strengthened the mortal
world rather than diminishing the giver. KNOSSOS and CHARON sit in
that lineage:

- **KNOSSOS** is a phase-1 portal into MNEMOS's storage substrate that
  speaks MemPalace's tool vocabulary (wings, rooms, drawers,
  and KG basics today; tunnels and diaries remain deferred) byte-for-byte
  where implemented. Existing MemPalace-targeting
  agents — every Claude Code prompt, every harness — keep working
  when their owner's workload outgrows what file-backed local-first
  storage can handle. No migration, no code changes in the agent.
- **CHARON** is the ferryman between memory systems with different
  schemas. The adapters in `tools/adapters/` carry data across from
  MemPalace, Mem0, Letta, Graphiti, and Cognee without losing
  provenance. MPF (Memory Portability Format) is the envelope; each
  adapter is a translator between a foreign schema and the envelope.

Both are interop gifts, not weapons. The goal is composability —
operators who run MemPalace alongside MNEMOS get to use both. CHARON
adapters move data without forcing rewrites. The two-way bridges
matter more than any single system winning.

This isn't just framing. The pantheon brings tools, AND it picks up
its hammer when the upstream needs work. We contribute back to
projects we touch, file issues for bugs we encounter through
KNOSSOS/CHARON testing, and propose fixes where maintainers are
open to them. Public evidence:

- **OpenClaw** (`openclaw/openclaw`):
  [PR #70224](https://github.com/openclaw/openclaw/pull/70224) —
  critical gateway fix, merged 2026-04-22. Contributor status as
  @perlowja.
- **Zeroclaw**: provider-config + backend work in
  [perlowja/zeroclaw](https://github.com/perlowja/zeroclaw); ongoing.
- **Hermes Agent**: design-inspiration credit on the zterm-family
  side; PRs scoped where the runtime intersects MNEMOS's MCP surface.
- **MemPalace, Mem0, Letta, Graphiti, Cognee**: bug reports and
  goodwill PRs as we encounter issues testing the CHARON adapters
  against real instances. The first wave of upstream MemPalace
  contributions and KNOSSOS bridge RFC re-engagement remain staged work. See
  `ROADMAP.md`.

We'll grow this list as PRs land. The principle is simple: when
KNOSSOS or CHARON adapters surface bugs in upstream memory systems,
we file them, we propose fixes, and where the maintainers are open
to it, we ship the fix as a PR. That's the contract.

## Stability commitments

While `experimental`:

- Endpoints under `/admin/tunnels/*` may be renamed, restructured, or
  withdrawn in any minor release.
- Default ports (5004 for the MCP HTTP/SSE bridge) may change.
- Bearer auth is the current baseline. Per-user token mapping exists on the
  HTTP/SSE bridge; OAuth on the MCP edge remains later work.
- The `mnemos-tunnel-setup` script's argument shape and config-file
  location (`~/.mnemos/tunnel.toml`) may change.

When the connector subsystem promotes to `stable` in a later release,
those guarantees flip — semver applies, deprecation cycles apply.

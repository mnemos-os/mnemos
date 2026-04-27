# KNOSSOS — team-grade MemPalace compatibility for MNEMOS

Outgrew MemPalace? KNOSSOS is an MCP server that speaks MemPalace's
tool vocabulary (wings, rooms, drawers, tunnels, diaries) but routes
every call to a MNEMOS backend. Your existing workflow keeps working.
Your team gets shared namespaces, ownership, federation, an HTTP API,
a version DAG, audit logs, compression, and the rest of MNEMOS —
without relearning any tool names.

Named after the Bronze Age palace on Crete where Linear A/B tablets
first institutionalized writing-for-memory. An actual palace, not
a Latinate abstract noun.

---

## Why it exists

MemPalace is local-first by design. That's a feature when the palace
is yours alone; it becomes a wall when a team needs:

- **Shared memory** across developers (not "sync my palace over
  Dropbox and hope")
- **Ownership and permissions** — who wrote this, who can delete it,
  what room is private
- **Version history** — diff, revert, merge; not "whoever wrote last
  wins"
- **An HTTP API** — for agents, dashboards, ETL jobs, non-Claude
  tooling
- **Federation** — pull memories across MNEMOS instances with
  provenance intact
- **Audit + compliance** — signed audit chain on every memory
  mutation

MNEMOS has all of these in its core. KNOSSOS is the glue that lets a
team adopt them without changing how their agents talk to memory.

---

## How it works

KNOSSOS is a stdio MCP server (same shape as MemPalace's) that
registers the MemPalace tool names and translates each call to the
corresponding MNEMOS REST endpoint under the hood.

```
┌──────────────┐       MCP stdio       ┌──────────────────┐       HTTP       ┌────────────┐
│  Agent       │◀─────────────────────▶│  KNOSSOS         │◀────────────────▶│  MNEMOS    │
│  (Claude,    │   mempalace_search,   │  MCP shim        │   /v1/memories/  │  server    │
│   Cursor,    │   mempalace_add_      │  (tool-name      │   /v1/kg/        │  (team     │
│   ...)       │   drawer, ...)        │   translator)    │   /v1/federation │   backend) │
└──────────────┘                       └──────────────────┘                  └────────────┘
```

The agent's MCP client still calls `mempalace_search`, `mempalace_add_drawer`,
etc. KNOSSOS re-maps those to MNEMOS endpoints and responses back to
the MemPalace tool-response shape.

---

## Terminology map

| MemPalace concept | MNEMOS concept | KNOSSOS default |
|---|---|---|
| **wing** (person/project space) | `owner_id` OR `namespace` | `namespace` (configurable via `KNOSSOS_WING_AXIS=owner_id`) |
| **room** (topic within a wing) | `category` | 1:1 mapping |
| **drawer** (verbatim content unit) | memory record | 1:1 — a memory is a drawer |
| **tunnel** (cross-wing link) | `kg_triple` with `predicate="tunnel:<label>"` | Round-trips via `/v1/kg/triples` |
| **diary** (per-agent log) | memory with `source_agent=<name>` + `category="diary"` | 1:1 mapping |
| **AAAK compressed card** | compression_manifest entry | Ride through under `metadata.aaak` |
| **Palace config** (`mempalace.yaml`) | (implicit) | KNOSSOS emits a compatibility file on first connect |

---

## What you gain by moving

| Capability | MemPalace 3.3.x | MNEMOS 3.2.x via KNOSSOS |
|---|---|---|
| Multi-user memory | ❌ | ✅ — `owner_id`, `group_id`, `permission_mode` |
| HTTP API | ❌ | ✅ — `/v1/memories/*`, `/v1/kg/*`, `/v1/export`, `/v1/import` |
| Bulk ingest speed | ~2 memories/sec (local, one-at-a-time) | ~200 memories/sec via `/v1/import` (MPF envelope, batched) |
| Cross-instance portability | ❌ (no native export) | ✅ — Memory Portability Format (MPF) round-trip |
| Version DAG (diff/revert/branch) | ❌ | ✅ — memory_versions table + commit_hash |
| Audit chain | ❌ | ✅ — every mutation hashed, verifiable |
| Compression (APOLLO/ARTEMIS contest) | AAAK only (~30× on a subset) | Full contest — APOLLO schema + ARTEMIS extractive, judge-scored |
| Federation across instances | ❌ | ✅ — `/v1/federation/*`, pull-based |
| Knowledge graph (temporal) | ✅ — SQLite-backed | ✅ — Postgres-backed, same temporal semantics |
| MCP surface | 29 native tools | 29 via KNOSSOS + MNEMOS-native MCP tools |
| Retrieval benchmark R@5 (LongMemEval) | 96.6% raw / 98.4% hybrid | (see `benchmarks/compression_corpus_v3_3.jsonl`) |

MemPalace is not wrong for its use case. KNOSSOS exists for the
moment your use case grew out of it.

---

## Setup

### 1. Run a MNEMOS backend

Point MNEMOS at your team's datastore (Postgres + optional phi-server
for embeddings). Canonical quickstart: `docker-compose up` against
the MNEMOS repo root. Full deployment notes in [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md).

### 2. Point KNOSSOS at it

```bash
export MNEMOS_BASE=http://mnemos.internal:5002
export MNEMOS_API_KEY=$TEAM_API_KEY           # bearer token, issued per user
export KNOSSOS_WING_AXIS=namespace             # default; 'owner_id' is also accepted
                                               # (MNEMOS /v1/memories/search only scopes by namespace today)

python -m tools.knossos_mcp                   # stdio MCP server
```

Or register with Claude Code:

```bash
claude mcp add knossos -- python -m tools.knossos_mcp
```

The existing `mempalace_*` tool names keep working in your agent's
prompts and harnesses; no code change in the agent.

### 3. (Optional) Migrate an existing palace

```bash
python -m tools.knossos_mcp migrate \
    --from-palace ~/.mempalace/palace \
    --endpoint $MNEMOS_BASE \
    --api-key $MNEMOS_API_KEY \
    --as-wing you@team.com              # your user_id in the team instance
```

This reads MemPalace's ChromaDB directly (no dependency on MemPalace's
Python runtime), wraps each drawer as an MPF `kind: memory` record,
and `POST`s to `/v1/import` with `preserve_owner=true`. Original
drawer IDs and metadata are kept.

---

## Tool coverage (v0.1)

Implemented:

- `mempalace_status`
- `mempalace_list_wings`
- `mempalace_list_rooms`
- `mempalace_get_taxonomy`
- `mempalace_search`
- `mempalace_check_duplicate`
- `mempalace_list_drawers`
- `mempalace_get_drawer`
- `mempalace_add_drawer`
- `mempalace_update_drawer`
- `mempalace_delete_drawer`
- `mempalace_kg_add`
- `mempalace_kg_query`
- `mempalace_kg_invalidate`
- `mempalace_kg_timeline`
- `mempalace_kg_stats`

Phase 2 (issue-tracked):

- `mempalace_create_tunnel` / `mempalace_list_tunnels` /
  `mempalace_delete_tunnel` / `mempalace_find_tunnels` /
  `mempalace_follow_tunnels` / `mempalace_traverse` — tunnels map to
  `kg_triples` with a reserved `tunnel:*` predicate; scaffold exists,
  needs edge semantics lined up.
- `mempalace_diary_read` / `mempalace_diary_write` — agent-scoped
  memory; needs `source_agent` propagation audit.
- `mempalace_get_aaak_spec` — serves MemPalace's AAAK dialect
  verbatim for round-trip with MemPalace-compressed drawers.
- `mempalace_reconnect` / `mempalace_memories_filed_away` /
  `mempalace_hook_settings` / `mempalace_graph_stats` /
  `mempalace_mcp` — maintenance surface, mostly constant responses.

---

## Design invariants

1. **No vocabulary drift.** Tool names, argument names, and
   response-key names match MemPalace's wire shape byte-for-byte
   where possible. An agent prompt that works against MemPalace
   works against KNOSSOS without a single token change.
2. **No silent field loss.** When MemPalace-native fields don't
   have a MNEMOS equivalent (spatial palace coordinates, AAAK
   compressed form), they ride through under `metadata.mempalace.*`
   so round-trip is preserved.
3. **Team features are additive.** A solo user who points KNOSSOS
   at their local MNEMOS sees MemPalace behavior. The team features
   (multi-user, group_id, federation) activate only when the backing
   MNEMOS is configured for them.

---

## Relation to CHARON and MPF

KNOSSOS is one spoke; CHARON is the hub. KNOSSOS translates the
MemPalace MCP tool surface. CHARON handles bulk import/export via
MPF envelopes (`tools/memory_import.py`, `tools/memory_export.py`,
`docs/mpf_v0.1.json`). A full MemPalace → MNEMOS migration uses both:

1. CHARON one-time bulk import of the existing palace (via
   `tools.knossos_mcp migrate --from-palace`).
2. KNOSSOS ongoing MCP traffic going forward.

Other memory systems get the same pattern — CHARON for bulk
migration via MPF, plus a per-system MCP shim on top if their tool
vocabulary is worth preserving. Mem0 / Letta / Graphiti / Cognee
shims are scoped but not yet written.

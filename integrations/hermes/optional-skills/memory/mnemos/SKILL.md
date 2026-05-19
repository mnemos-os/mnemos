---
name: mnemos
description: Configure and use MNEMOS for persistent cross-session memory with Hermes — shared memory pool across profiles, full-text and semantic search, compression with quality manifests, knowledge-graph triples, and session transcript ingestion. Use when setting up MNEMOS, troubleshooting memory retrieval, storing decisions/patterns/infrastructure facts, or tuning category-scoped queries.
version: 1.0.0
author: MNEMOS Project
license: Apache-2.0
metadata:
  hermes:
    tags: [MNEMOS, Memory, Persistence, Knowledge-Graph, Cross-Session, Semantic-Search]
    homepage: https://github.com/ncz-os/mnemos
    related_skills: [hermes-agent]
prerequisites:
  services: [mnemos-api]
---

# MNEMOS Memory for Hermes

MNEMOS is persistent memory infrastructure running as a separate network service. You point Hermes at it via `MNEMOS_BASE` — there is no default. Unlike Honcho (per-peer cross-session user modeling), MNEMOS is a **shared memory pool** across all Hermes profiles — any agent connected to the same MNEMOS instance reads and writes the same memory pool, subject to namespace and permission controls.

## When to Use

- Storing architectural decisions, infrastructure facts, solved problems, patterns, team standards
- Retrieving prior decisions before starting a task
- Loading compressed context into a Hermes prompt under a token budget
- Building knowledge-graph triples with temporal validity (`X depends on Y until <date>`)
- Ingesting Hermes session transcripts for later retrieval

## Setup

### Register MNEMOS as an MCP server

```bash
hermes mcp add mnemos \
  --command /path/to/mnemos/venv/bin/mnemos \
  --args "serve mcp-stdio" \
  --env MNEMOS_BASE=http://mnemos.internal:5002
```

Or edit `~/.hermes/config.yaml` directly (replace placeholder with your MNEMOS host):

```yaml
mcp_servers:
  mnemos:
    command: /path/to/mnemos/venv/bin/mnemos
    args: ["serve", "mcp-stdio"]
    env:
      MNEMOS_BASE: http://<your-mnemos-host>:5002
```

### Verify

```bash
hermes mcp list          # should show mnemos
hermes mcp test mnemos   # should round-trip
```

## Memory categories

MNEMOS uses six canonical categories. Pick deliberately — wrong category makes retrieval harder:

| Category | For |
|----------|-----|
| `infrastructure` | Ports, hostnames, service locations, credential storage |
| `solutions` | Workarounds, resolved bugs, fixes applied elsewhere |
| `patterns` | Reusable approaches, validated designs |
| `decisions` | Architecture choices, rationale, tradeoffs |
| `projects` | Current project state, owners, milestones |
| `standards` | Conventions, quality gates, review criteria |

## Read pattern

```python
# Search before starting a non-trivial task
results = mcp.call("mnemos", "search_memories", {
    "query": "auth flow token rotation",
    "category": "decisions",
    "limit": 5
})

# Large context load under a token budget — REST endpoint (not an MCP tool
# in the current `mnemos serve mcp-stdio` transport). Use via httpx against MNEMOS directly, or
# chain several search_memories calls with a running token tally.
#
# POST /v1/memories/rehydrate
#   { "query": "current project state", "budget_tokens": 8000 }
```

## Write pattern

Store after solving something or making a decision:

```python
mcp.call("mnemos", "create_memory", {
    "content": "Chose OAuth device-code flow for CLI clients — refresh tokens kept out of ~/.config. Why: user requested no file-based secrets. Tradeoff: re-auth required every 90 days.",
    "category": "decisions",
    "subcategory": "auth",
    "metadata": {
        "source_agent": "hermes",
        "profile": "<profile-id>"
    }
})
```

Write rules:

- Lead with the **claim or fact**; put the why immediately after
- Under 500 words per memory
- Include provenance in metadata so future Hermes profiles can trace origin

## Knowledge graph

For relational facts with temporal meaning, use triples:

```python
mcp.call("mnemos", "kg_create_triple", {
    "subject": "hermes-agent",
    "predicate": "depends_on",
    "object": "mnemos-api",
    "valid_from": "2026-04-21"
})
```

Query a subject's timeline to see how relationships evolved.

## Profiles and namespaces

If MNEMOS is running in team or enterprise profile, memories carry `owner_id`, `namespace`, and `permission_mode`. Each Hermes profile can use a distinct namespace (e.g. `namespace: "hermes/<profile-id>"`) to keep writes separated while still permitting cross-profile reads at `permission_mode=644`.

## Avoid memory poisoning

Before acting on a retrieved memory:

- If it names a file or endpoint, verify it's still reachable
- If current reality contradicts the memory, **trust current reality** and update the memory — don't act on stale data
- MNEMOS has a DAG versioning model (see `ANTI_MEMORY_POISONING.md` in the MNEMOS repo). For drift investigation use the REST endpoints: `GET /v1/memories/{id}/log` for commit history, `GET /v1/memories/{id}/versions/{n}` for specific snapshots, `GET /v1/memories/{id}/diff` for content comparison. Plus v3.1's `GET /v1/memories/{id}/compression-manifests` for the compression audit trail

## Troubleshooting

**`hermes mcp test mnemos` fails with connection refused**
MNEMOS API is not running, or `MNEMOS_BASE` points to the wrong host/port. Check with `curl $MNEMOS_BASE/health`.

**Authentication errors on team/enterprise profiles**
The v4 MCP transport forwards Bearer tokens when `MNEMOS_API_KEY` is set in its environment. Ensure the env block in your Hermes MCP config includes `MNEMOS_API_KEY` alongside `MNEMOS_BASE`. If you can't put the token in the MCP config, run the MCP server on the same host as MNEMOS with MNEMOS bound to loopback, or use SSH transport with `mnemos serve mcp-stdio`.

**Memories returned but low relevance**
Try `semantic: true` for concept-level search, or narrow with `category` filter.

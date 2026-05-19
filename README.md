<p align="center">
  <img src="docs/images/logo.png" alt="MNEMOS" width="220" />
</p>

# MNEMOS + GRAEAE

**MNEMOS v5.0.1 is the memory operating system for serious agentic work: a
packaged FastAPI runtime, multi-backend persistence layer, GRAEAE reasoning bus,
operator-audited compression stack, divergent dream-state pipeline (REPLAY ->
CLUSTER -> CONSOLIDATE -> SYNTHESISE -> EXTRACT), GDPR right-to-be-forgotten
worker, PERSEPHONE archival subsystem, PANTHEON unified LLM facade, KRONOS
recall observability, and CLI-first deployment surface.**

MNEMOS is not just a place to put bytes. It is a runtime of named subsystems that
manage the full lifecycle of agent memory across providers, agents, and time
horizons: **write, embed, search, compress, version, reason-over, audit,
federate, export, import, and operate**.


## Quick Start

Memory and reasoning runtime for AI agents: persistent search, versioned storage, webhook fanout, and a unified LLM routing bus - all behind a single MCP interface.

---

### 1. Agent-driven install

Paste into Claude Code, Cursor, or Codex. The agent runs the install; you confirm.

```
Install MNEMOS on this machine.

Steps:
1. pip install 'mnemos-os[server]==5.0.1'
2. mnemos init                         # scaffold config + token
3. mnemos serve                        # start API on :5002
4. mnemos doctor                       # verify subsystems
5. Set MNEMOS_BASE=http://localhost:5002 and MNEMOS_API_KEY=<token from step 2>
   in shell env and any agent config that needs to reach it.

Edge device (SQLite, no Postgres): pip install 'mnemos-os[edge]==5.0.1' instead.
Full install with all subsystems: pip install 'mnemos-os[full]==5.0.1'
```

---

### 2. Connect an agent via MCP

Add to `~/.claude/mcp_servers.json` (Claude Code) or equivalent:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://<host>:5002",
        "MNEMOS_API_KEY": "<token>"
      }
    }
  }
}
```

For HTTP/SSE transport (ChatGPT, remote agents): `mnemos serve mcp-http` on `:5004`.

Key MCP tools the agent gets:

| Tool | What it does |
|---|---|
| `search_memories` | Semantic + filtered search across the memory store |
| `create_memory` | Write a new memory with category, tags, and content |
| `get_memory` | Fetch a memory by ID |
| `kg_search` | Query the knowledge-graph triple store |
| `kronos_anomalies` | Surface recall anomalies and memory health signals |

---

### 3. Webhooks + integrations

| Integration | What connects | How |
|---|---|---|
| **Claude Code** | Hooks fire on session-start, prompt-submit, stop - auto-log to MNEMOS | `integrations/claude-code/` - copy hooks + set `MNEMOS_BASE` |
| **ZeroClaw** | Zeroclaw agent reads/writes memories via MCP | `integrations/zeroclaw/` + `mnemos serve mcp-stdio` in zeroclaw config |
| **OpenClaw** | OpenClaw gateway routes memory ops through MCP | `integrations/openclaw/` + MCP server entry in `openclaw.json` |
| **Hermes** | Optional memory skill mounts MNEMOS as a tool provider | `integrations/hermes/optional-skills/memory/mnemos/` |
| **Webhooks (any)** | Push `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed` events to any HTTPS endpoint | `POST /api/webhooks/register` with `{"url": "...", "events": [...]}` |
| **Cursor / Cline / Continue.dev / Zed / Aider** | Any MCP-capable IDE connects via stdio or HTTP transport | See `docs/connectors/` |

---

Full documentation: [docs/](docs/)

## Architecture

MNEMOS is a packaged FastAPI service with a single `mnemos` CLI for installation, serving, MCP transport, and operational checks. Agents connect through MCP stdio, MCP HTTP/SSE, REST, or OpenAI-compatible SDKs, while the runtime routes memory, reasoning, session, webhook, federation, portability, and observability work through the `mnemos/` package. Persistence is selected by profile: SQLite plus sqlite-vec for edge and development installs, or PostgreSQL plus pgvector for server deployments. GRAEAE handles multi-provider reasoning and model routing; MOIRAI handles operator-audited compression through APOLLO and ARTEMIS.

## Documentation

| Topic | File |
|---|---|
| Installation | [docs/INSTALL.md](docs/INSTALL.md) |
| Specification | [docs/SPECIFICATION.md](docs/SPECIFICATION.md) |
| System requirements | [docs/SYSTEM_REQUIREMENTS.md](docs/SYSTEM_REQUIREMENTS.md) |
| Memory architecture | [docs/MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md) |
| Compression | [docs/COMPRESSION.md](docs/COMPRESSION.md) |
| GRAEAE reasoning | [docs/GRAEAE_FEATURES.md](docs/GRAEAE_FEATURES.md) |
| PANTHEON provider facade | [docs/PANTHEON.md](docs/PANTHEON.md) |
| KRONOS observability | [docs/KRONOS.md](docs/KRONOS.md) |
| Portability format | [docs/MEMORY_EXPORT_FORMAT.md](docs/MEMORY_EXPORT_FORMAT.md) |
| Scaling | [docs/SCALING.md](docs/SCALING.md) |
| Single-binary builds | [docs/SINGLE_BINARY.md](docs/SINGLE_BINARY.md) |
| Operations | [docs/OPERATIONS.md](docs/OPERATIONS.md) |

## License

MNEMOS is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.

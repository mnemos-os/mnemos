# Claude Code → MNEMOS

> **Status: stable.** Claude Code was the original target for the
> MNEMOS MCP layer; the integration has shipped and run continuously
> since v3.x. The recipe below is the recommended path; legacy
> SSH-spawn shapes from earlier releases still work but the local
> `mnemos mcp serve --stdio` path has fewer moving parts.

## What this gets you

Claude Code's tool surface gains 13 MNEMOS tools (search, create,
update, delete, list, get_stats, KG triple CRUD + search +
timeline, bulk_create_memories) usable directly from any prompt.
The same memory is shared with every other MCP-aware client you
have configured against the same MNEMOS instance.

## Prerequisites

- Claude Code (`@anthropic-ai/claude-code`) installed.
- A running MNEMOS instance — local-stdio if Claude Code is on the
  same host, HTTP/SSE if Claude Code is remote.
- Bearer token for MNEMOS auth (`MNEMOS_API_KEY` from your install).

## Setup — local stdio (recommended for desktop / dev box)

### 1. Stage the MCP entrypoint

Make sure `mnemos` is on your PATH:

```bash
which mnemos                # should print the binary path
mnemos --version            # should print 4.2.0a11 or later
```

If it's not on PATH, point at the absolute path of the install
directory binary in step 2.

### 2. Register with Claude Code

Edit `~/.claude.json` (or invoke `claude mcp add`). The MCP server
block looks like:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["mcp", "serve", "--stdio"],
      "env": {
        "MNEMOS_BASE": "http://localhost:5002",
        "MNEMOS_API_KEY": "<your bearer token>"
      }
    }
  }
}
```

If your MNEMOS runs on a different host, change `MNEMOS_BASE` to
the LAN URL (e.g., `http://192.168.207.67:5002`). The MCP server
opens an outbound HTTPS connection to that base URL on every
tool call — Claude Code never talks to MNEMOS directly.

### 3. Restart Claude Code

```bash
claude /restart   # or simply close + reopen
```

The MCP tools should now appear in `claude /mcp` list output.

## Setup — remote MNEMOS via SSH (legacy / multi-host fleets)

If your dev box runs Claude Code but MNEMOS lives on a fleet host,
SSH-spawn the MCP server on the remote host:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "ssh",
      "args": [
        "user@mnemos-host",
        "/opt/mnemos/venv/bin/python",
        "/opt/mnemos/mcp_server.py"
      ],
      "env": {
        "MNEMOS_BASE": "http://localhost:5002",
        "MNEMOS_API_KEY": "<your bearer token>"
      }
    }
  }
}
```

The MNEMOS_BASE here is `localhost` because the MCP server runs
ON the remote host; the env vars travel through SSH's environment.
You'll need pubkey auth set up so SSH doesn't prompt for a
password each Claude session start (it would block forever).

## Setup — remote MNEMOS via HTTP/SSE (multi-machine, no SSH)

When you can't or don't want to SSH-spawn, point Claude Code at
MNEMOS's MCP HTTP/SSE bridge:

```json
{
  "mcpServers": {
    "mnemos": {
      "url": "https://mnemos.example.com/v1/mcp/sse",
      "headers": {
        "Authorization": "Bearer <your bearer token>"
      }
    }
  }
}
```

This requires the MCP HTTP/SSE bridge to be running on the
target — see [README.md](./README.md) §"Quick start". Plus a
public HTTPS endpoint (most setups use Caddy/nginx + Let's
Encrypt or a Tailscale Funnel).

## Smoke test

Open Claude Code, run:

```
/mcp
```

You should see `mnemos` listed with status `connected`. Then:

```
Search MNEMOS for "test memory" and show me the first three results.
```

Claude Code will call the `search_memories` tool and return the
results. If MNEMOS has nothing yet, ask it to create one:

```
Create a MNEMOS memory in category "test" with content "First connector smoke test".
```

Then re-search. The new memory should appear.

## Troubleshooting

| Symptom                                          | Likely cause                                   | Fix                                                    |
|--------------------------------------------------|------------------------------------------------|--------------------------------------------------------|
| `mnemos` not found in `/mcp`                     | MCP server failed to start                     | Run `mnemos mcp serve --stdio` directly; read errors   |
| All tool calls return `MNEMOS UNREACHABLE`       | `MNEMOS_BASE` URL wrong or MNEMOS not running  | `curl <MNEMOS_BASE>/health` from the same shell        |
| Tool calls return 401 / 403                      | Bearer token wrong or expired                  | Verify with `curl -H "Authorization: Bearer $TOKEN" <base>/v1/memories` |
| `ssh:` shape: hangs at MCP connect               | SSH would have prompted for password           | Set up pubkey auth: `ssh-copy-id user@host`            |
| HTTP/SSE shape: 502 errors                       | Reverse proxy buffering SSE                    | Disable buffering for `/v1/mcp/sse` in nginx/Caddy     |
| Random tool call timeouts                        | Slow downstream (LLM consultation, federation) | Check MNEMOS `/metrics` for p99 latency on the route   |

## Memory-namespace isolation per-project

If you want your work-related Claude Code memories isolated from
your personal ones, use distinct MNEMOS namespaces:

```json
{
  "mcpServers": {
    "mnemos-work": {
      "command": "mnemos",
      "args": ["mcp", "serve", "--stdio", "--namespace", "work"],
      "env": {"MNEMOS_BASE": "...", "MNEMOS_API_KEY": "..."}
    },
    "mnemos-personal": {
      "command": "mnemos",
      "args": ["mcp", "serve", "--stdio", "--namespace", "personal"],
      "env": {"MNEMOS_BASE": "...", "MNEMOS_API_KEY": "..."}
    }
  }
}
```

Search and create in `work` only see `work` rows; the same for
`personal`. The MNEMOS server enforces the scope at the SQL
level — there's no path for an agent to escape its namespace
short of explicit operator-tier auth.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- `MEMORY_ARCHITECTURE.md` §3 — two-axis tenancy details (owner +
  namespace).
- `OPERATIONS.md` — running the MCP server as a systemd service
  (alternative to per-Claude-Code spawn).

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a11.*

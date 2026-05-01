# Claude Code → MNEMOS

> **Status: stable.** Claude Code was the original target for the
> MNEMOS MCP layer; the integration has shipped and run continuously
> since v3.x. The recipe below is the recommended path; legacy
> SSH-spawn shapes from earlier releases still work but the local
> `mnemos serve mcp-stdio` path has fewer moving parts.

## What this gets you

Claude Code's tool surface gains the MNEMOS MCP tools — see the
canonical exact-name table in [README.md](./README.md#canonical-mcp-tool-surface).
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
      "args": ["serve", "mcp-stdio"],
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
SSH-spawn the MCP server on the remote host. **OpenSSH does NOT
forward arbitrary client env vars by default** — to be safe, pass
them on the remote command line so the MCP server starts with the
right config:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "ssh",
      "args": [
        "user@mnemos-host",
        "env",
        "MNEMOS_BASE=http://localhost:5002",
        "MNEMOS_API_KEY=<your bearer token>",
        "/opt/mnemos/venv/bin/mnemos",
        "serve",
        "mcp-stdio"
      ]
    }
  }
}
```

The ``env`` prefix is what actually plumbs the variables into the
remote process. MNEMOS_BASE here is ``localhost`` because the MCP
server runs ON the remote host. You'll need pubkey auth set up so
SSH doesn't prompt for a password each Claude session start (it
would block forever).

If the remote host has a system-wide ``mnemos`` on PATH you can
omit the ``/opt/mnemos/venv/bin/`` prefix.

**Inline-env caveat.** OpenSSH does NOT preserve a remote argv
boundary across the ssh-client → ssh-server hop; the joined
command string is parsed by the remote login shell. If your
``MNEMOS_API_KEY`` or ``MNEMOS_BASE`` value contains whitespace
or shell metacharacters (``$`` ``"`` ``'`` ``\`` ``;``
``|`` ``&``), the inline form above can break startup or
execute unintended shell syntax. Two safer paths:

1. **Bake the env on the remote.** Write a shell wrapper at
   ``/opt/mnemos/bin/mcp-stdio-wrapper`` that exports the vars
   from a chmod 0600 file and execs ``mnemos serve mcp-stdio``;
   point the SSH command at the wrapper. The secret never
   crosses the wire as part of the argv.
2. **Configure SendEnv/AcceptEnv on both sides.** ``ssh_config``
   ``SendEnv MNEMOS_*`` on the client + ``sshd_config``
   ``AcceptEnv MNEMOS_*`` on the server, then the local ``env``
   from your Claude Code config block reaches the remote process
   without going through argv parsing.

The inline form is safe for MNEMOS-generated tokens (``mnemos_``
prefix + 64 hex chars — no shell metacharacters), but custom
operator-set tokens should go through path 1 or 2.

## Setup — remote MNEMOS via HTTP/SSE (multi-machine, no SSH)

When you can't or don't want to SSH-spawn, point Claude Code at
MNEMOS's MCP HTTP/SSE bridge:

```json
{
  "mcpServers": {
    "mnemos": {
      "url": "https://mnemos.example.com/sse",
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
| `mnemos` not found in `/mcp`                     | MCP server failed to start                     | Run `mnemos serve mcp-stdio` directly; read errors   |
| All tool calls return `MNEMOS UNREACHABLE`       | `MNEMOS_BASE` URL wrong or MNEMOS not running  | `curl <MNEMOS_BASE>/health` from the same shell        |
| Tool calls return 401 / 403                      | Bearer token wrong or expired                  | Verify with `curl -H "Authorization: Bearer $TOKEN" <base>/v1/memories` |
| `ssh:` shape: hangs at MCP connect               | SSH would have prompted for password           | Set up pubkey auth: `ssh-copy-id user@host`            |
| HTTP/SSE shape: 502 errors                       | Reverse proxy buffering SSE                    | Disable buffering for `/sse` in nginx/Caddy     |
| Random tool call timeouts                        | Slow downstream (LLM consultation, federation) | Check MNEMOS `/metrics` for p99 latency on the route   |

## Memory-namespace isolation per-project

If you want your work-related Claude Code memories isolated from
your personal ones, use distinct MNEMOS namespaces by setting
``MNEMOS_DEFAULT_NAMESPACE`` per MCP server entry. The server
treats this as the default namespace stamp on every memory
created through that stdio bridge:

```json
{
  "mcpServers": {
    "mnemos-work": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "...",
        "MNEMOS_API_KEY": "...",
        "MNEMOS_DEFAULT_NAMESPACE": "work"
      }
    },
    "mnemos-personal": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "...",
        "MNEMOS_API_KEY": "...",
        "MNEMOS_DEFAULT_NAMESPACE": "personal"
      }
    }
  }
}
```

``MNEMOS_DEFAULT_NAMESPACE`` is a **WRITE STAMP**, not an
enforced scope. The MCP bridge stamps it on every
create/search/list/bulk request so memories created via the
``mnemos-work`` connector default to ``namespace=work``. By-id
operations (get/update/delete) are NOT scoped by the env var.
Bulk per-row ``namespace`` is OVERWRITTEN by the env stamp when
set (env wins) — cross-namespace bulk requires unsetting
``MNEMOS_DEFAULT_NAMESPACE`` or hitting the REST API directly.

For ENFORCED per-connector isolation:
* Provision two distinct non-root **users** on MNEMOS, each with
  its own ``namespace`` value on the ``users`` row (the
  ``users.namespace`` column added in v3.2). Issue an API key
  for each user. The MNEMOS auth path resolves a request's
  effective namespace from the API key's user, so distinct users
  produce distinct effective scopes.
* Pair each MCP connector entry with the matching key.

Note: distinct API keys under the SAME user share that user's
namespace — the ``api_keys`` table itself has no per-key
namespace column. Isolation is per-user, not per-key.

A root API key with the env stamp will write into the configured
namespace by default but can still read/update/delete any memory
ID across namespaces — that's the cost of running a root key.
Use the env stamp on a root key as a CONVENIENCE for default-
write-to-this-namespace ergonomics, not as a security boundary.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- `MEMORY_ARCHITECTURE.md` §3 — two-axis tenancy details (owner +
  namespace).
- `OPERATIONS.md` — running the MCP server as a systemd service
  (alternative to per-Claude-Code spawn).

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a11.*

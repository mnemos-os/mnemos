# Claude Code -> MNEMOS

Claude Code can use MNEMOS as a shared memory layer by SSH-spawning the MNEMOS stdio MCP server from `.claude.json`.

## What you need — token, host (192.168.207.67), relevant port(s)

- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- SSH access from the Claude Code machine to `192.168.207.67`.
- `mnemos` installed on the remote host and visible on its PATH.
- Claude Code installed and able to read `~/.claude.json`.
- Optional HTTP/SSE MCP bridge reachable at `http://192.168.207.67:5003/sse`.
- Use stdio over SSH when Claude Code runs off-host.
- Use HTTP/SSE only when the host agent supports remote MCP transport.
- Keep the bearer token scoped to the user or namespace Claude Code should use.
- Do not paste the live token into docs, tickets, or shared screenshots.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Merge this `mcpServers` entry into `~/.claude.json`. The MCP process runs
on `192.168.207.67`, so `MNEMOS_BASE` points at loopback from the remote
host's point of view.

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "ssh",
      "args": [
        "mnemos@192.168.207.67",
        "env",
        "MNEMOS_BASE=http://127.0.0.1:5002",
        "MNEMOS_API_KEY=$MNEMOS_TOKEN",
        "mnemos",
        "serve",
        "mcp-stdio"
      ]
    }
  }
}
```

If your remote install lives in a virtualenv, replace `mnemos` with the
absolute path, for example `/opt/mnemos/venv/bin/mnemos`.

The SSH account must authenticate non-interactively. Claude Code cannot
answer a password or MFA prompt while it is starting an MCP server.

For same-host development, use a local stdio entry instead of SSH:

```json
{
  "mcpServers": {
    "mnemos-local": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://192.168.207.67:5002",
        "MNEMOS_API_KEY": "$MNEMOS_TOKEN"
      }
    }
  }
}
```

For remote HTTP/SSE, run the bridge on the MNEMOS host and point a
remote-MCP-capable Claude Code build at `http://192.168.207.67:5003/sse`.
Keep the same bearer token in the `Authorization` header:

```json
{
  "mcpServers": {
    "mnemos-sse": {
      "url": "http://192.168.207.67:5003/sse",
      "headers": {
        "Authorization": "Bearer $MNEMOS_TOKEN"
      }
    }
  }
}
```

Restart Claude Code after editing `.claude.json`; registrations are loaded
at process start.

## Verification — one curl or one tool-list call that proves registration worked

```bash
claude mcp list | grep -i mnemos
```

The `mnemos` server should show as connected or ready. If the CLI build
does not expose `claude mcp list`, open Claude Code and run `/mcp`.

## Common gotchas — 2-4 bullets of real failure modes

- SSH password prompts make the MCP server appear to hang; use key auth.
- `MNEMOS_BASE=http://127.0.0.1:5002` is correct only for the SSH-spawned
  remote process; local stdio should use `http://192.168.207.67:5002`.
- Literal `$MNEMOS_TOKEN` only works if Claude Code expands environment
  variables in config; otherwise write a local wrapper that reads the env.
- HTTP/SSE on `:5003` is plain HTTP unless you put TLS in front of it.

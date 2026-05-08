# Codex CLI -> MNEMOS

Codex CLI 0.125.0 and newer can register MNEMOS as an MCP server with `codex mcp add` or a `~/.codex/config.toml` block.

## What you need — token, host (192.168.207.67), relevant port(s)

- Codex CLI `0.125.0` or newer.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- The `mnemos` CLI installed on the same machine as Codex CLI.
- Config path: `~/.codex/config.toml`.
- Optional HTTP/SSE MCP bridge reachable at `http://192.168.207.67:5003/sse`.
- A new Codex process after config changes.
- Shell access to run `codex mcp add`.
- `jq` available for the verification command.
- A non-root MNEMOS token if you want namespace enforcement.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Register with the CLI first. This writes the MCP server entry for you on
Codex CLI builds that support `codex mcp add`.

```bash
export MNEMOS_TOKEN="${MNEMOS_TOKEN:?set MNEMOS_TOKEN first}"
codex mcp add mnemos \
  --env MNEMOS_BASE=http://192.168.207.67:5002 \
  --env MNEMOS_API_KEY="$MNEMOS_TOKEN" \
  -- mnemos serve mcp-stdio
```

The equivalent `~/.codex/config.toml` block for Codex `0.125.0+` is:

```toml
[mcp_servers.mnemos]
command = "mnemos"
args = ["serve", "mcp-stdio"]

[mcp_servers.mnemos.env]
MNEMOS_BASE = "http://192.168.207.67:5002"
MNEMOS_API_KEY = "$MNEMOS_TOKEN"
```

Some pre-release Codex builds used the older table name
`[mcp.servers.mnemos]`. If your local `codex --version` is older than
`0.125.0`, upgrade before debugging the table spelling.

For HTTP/SSE, use a URL registration when the Codex build supports remote
MCP transport:

```toml
[mcp_servers.mnemos-sse]
url = "http://192.168.207.67:5003/sse"

[mcp_servers.mnemos-sse.headers]
Authorization = "Bearer $MNEMOS_TOKEN"
```

Restart Codex after editing the TOML file. `codex exec` inherits the same
MCP registration as interactive sessions.

## Verification — one curl or one tool-list call that proves registration worked

```bash
codex mcp list | grep -i mnemos
```

If your Codex build lacks `codex mcp list`, start an interactive session
and use its MCP or tool-list command to confirm the `mnemos` server loaded.

## Common gotchas — 2-4 bullets of real failure modes

- Codex versions before `0.125.0` do not understand MCP server config.
- TOML table spelling changed across early builds; prefer `codex mcp add`
  when available.
- Shell variables are not expanded inside TOML by every launcher; use the
  CLI registration path or write the resolved token into a private config.
- Long-running MNEMOS operations can hit Codex tool timeouts; keep write
  approvals manual for bulk operations.

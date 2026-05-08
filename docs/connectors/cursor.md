# Cursor -> MNEMOS

Cursor can load MNEMOS through `~/.cursor/mcp.json` so its chat and agent flows share memory with the rest of your MCP clients.

## What you need — token, host (192.168.207.67), relevant port(s)

- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- Cursor with MCP support enabled.
- Config path: `~/.cursor/mcp.json`.
- The `mnemos` CLI installed on the same machine as Cursor.
- Optional HTTP/SSE MCP bridge reachable at `http://192.168.207.67:5003/sse`.
- A Cursor restart after changing MCP server config.
- A model profile allowed to call tools.
- Network access from Cursor's machine to `192.168.207.67`.
- `jq` available for the verification command below.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Create `~/.cursor/mcp.json` or merge this object with your existing MCP
servers. Cursor reads this file only during startup.

```json
{
  "mcpServers": {
    "mnemos": {
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

If Cursor runs on Windows and MNEMOS tooling is installed inside WSL, prefer
running a local HTTP/SSE bridge and using a URL entry. Cross-boundary stdio
with `wsl.exe` is fragile because environment and quoting rules differ.

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

Cursor may show tool names with a display prefix such as
`mnemos_search_memories`. Approval lists and MNEMOS logs still use the
canonical registry name, for example `search_memories`.

Restart Cursor after saving `~/.cursor/mcp.json`; a window reload is often
insufficient for MCP registration changes.

## Verification — one curl or one tool-list call that proves registration worked

```bash
# Server up?
curl -fsS http://192.168.207.67:5002/health | jq -r '.status'    # → "healthy"

# Confirm the canonical MCP tool registry includes search_memories:
python3 -c 'from mnemos.mcp.tools import TOOL_REGISTRY; print("search_memories" in TOOL_REGISTRY)'  # → True
```

(MCP discovery is the protocol's `tools/list` JSON-RPC method over
SSE/stdio, not a REST endpoint.) Then open Cursor's tool panel and
confirm `mnemos` appears with memory and KG tools.

## Common gotchas — 2-4 bullets of real failure modes

- Cursor requires a restart to reload MCP servers after editing
  `~/.cursor/mcp.json`.
- A literal `$MNEMOS_TOKEN` may not expand in all launch contexts; use an
  absolute wrapper script if Cursor starts outside your shell.
- Tool approval rules match exact names such as `delete_triple`, not display
  names like `mnemos_delete_triple`.
- Remote SSE on `:5003` must keep the event stream unbuffered.

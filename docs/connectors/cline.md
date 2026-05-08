# Cline -> MNEMOS

Cline can use MNEMOS from VS Code by adding a `mnemos` MCP server to the extension's MCP settings JSON.

## What you need — token, host (192.168.207.67), relevant port(s)

- Cline for VS Code with MCP support.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- The `mnemos` CLI installed where VS Code launches extensions.
- macOS Cline path: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`.
- Linux Cline path: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`.
- Windows Cline path: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`.
- Optional HTTP/SSE bridge reachable at `http://192.168.207.67:5003/sse`.
- VS Code window reload after editing the file.
- A model selected in Cline that can call tools.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Open Cline settings, then merge this into the `mcpServers` object.
The `autoApprove` list includes read-only tools only; write tools should
still prompt for approval.

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://192.168.207.67:5002",
        "MNEMOS_API_KEY": "$MNEMOS_TOKEN"
      },
      "disabled": false,
      "autoApprove": [
        "search_memories",
        "list_memories",
        "get_memory",
        "get_stats",
        "kg_search",
        "kg_timeline",
        "log_memory",
        "diff_memory_commits",
        "checkout_memory",
        "recommend_model"
      ]
    }
  }
}
```

For HTTP/SSE, use this shape only if your Cline build supports remote MCP:

```json
{
  "mcpServers": {
    "mnemos-sse": {
      "url": "http://192.168.207.67:5003/sse",
      "headers": {
        "Authorization": "Bearer $MNEMOS_TOKEN"
      },
      "disabled": false,
      "autoApprove": ["search_memories", "list_memories", "get_memory"]
    }
  }
}
```

Reload the VS Code window from the command palette after saving. A Cline
task that was already running will not pick up the new server.

## Verification — one curl or one tool-list call that proves registration worked

```bash
# Server up?
curl -fsS http://192.168.207.67:5002/health | jq -r '.status'    # → "healthy"

# Confirm the canonical MCP tool registry includes kg_search:
python3 -c 'from mnemos.mcp.tools import TOOL_REGISTRY; print("kg_search" in TOOL_REGISTRY)'  # → True
```

(MCP discovery is the protocol's `tools/list` JSON-RPC method over
SSE/stdio, not a REST endpoint.)

In Cline, start a new task and ask it to search MNEMOS for a harmless
phrase. It should request or use the `search_memories` tool.

## Common gotchas — 2-4 bullets of real failure modes

- Cline's `autoApprove` matches exact tool names; `kg_delete_triple` is not
  the same as `delete_triple`.
- VS Code launched from the dock may not inherit shell env variables.
- Do not auto-approve write tools unless the MNEMOS token is namespace
  scoped and non-root.
- The extension path still contains `claude-dev` on many installs.

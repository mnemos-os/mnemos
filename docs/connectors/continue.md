# Continue.dev -> MNEMOS

Continue.dev can attach MNEMOS through its `config.json` MCP server section so chat and edit sessions can search shared memory.

## What you need — token, host (192.168.207.67), relevant port(s)

- Continue.dev with MCP support enabled.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- The `mnemos` CLI installed on the same machine as the Continue host IDE.
- Config path: `~/.continue/config.json`.
- Optional HTTP/SSE MCP bridge reachable at `http://192.168.207.67:5003/sse`.
- A full host-IDE restart after config edits.
- A model profile in Continue that allows tool use.
- Access to Continue's output logs for startup failures.
- `jq` available for the verification command.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Merge this `mcp_servers` section into `~/.continue/config.json`. Keep your
existing `models`, `tabAutocompleteModel`, and context provider entries.

```json
{
  "mcp_servers": {
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

If your Continue build expects camelCase, use the same server object under
`mcpServers` instead of `mcp_servers`; the MNEMOS command shape is unchanged.

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

For HTTP/SSE, use a URL-backed server entry if the Continue build supports
remote MCP:

```json
{
  "mcp_servers": {
    "mnemos-sse": {
      "url": "http://192.168.207.67:5003/sse",
      "headers": {
        "Authorization": "Bearer $MNEMOS_TOKEN"
      }
    }
  }
}
```

Restart VS Code, JetBrains, or the Continue desktop host after saving the
file. Continue usually reads MCP config during extension activation.

## Verification — one curl or one tool-list call that proves registration worked

```bash
# Server up?
curl -fsS http://192.168.207.67:5002/health | jq -r '.status'    # → "healthy"

# Confirm the canonical MCP tool registry includes search_memories:
python3 -c 'from mnemos.mcp.tools import TOOL_REGISTRY; print("search_memories" in TOOL_REGISTRY)'  # → True
```

(MCP discovery is the protocol's `tools/list` JSON-RPC method over
SSE/stdio, not a REST endpoint.) Then open Continue chat and confirm
the `mnemos` server appears in the MCP or tools panel.

## Common gotchas — 2-4 bullets of real failure modes

- Continue has used both `mcp_servers` and `mcpServers` across builds; check
  the extension logs if the server does not appear.
- Host IDE restarts matter; editing `config.json` while Continue is active
  may not reload MCP registrations.
- Tool mentions in chat can fail if the selected model has tool use off.
- If `mnemos` is installed in a venv, use its absolute path as `command`.

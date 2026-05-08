# Claude Desktop -> MNEMOS

Claude Desktop can spawn MNEMOS's stdio MCP server and expose the same memory tools used by Claude Code, Cursor, and Codex CLI.

## What you need — token, host (192.168.207.67), relevant port(s)

- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- The `mnemos` CLI installed on the same machine as Claude Desktop.
- macOS config path: `~/Library/Application Support/Claude/claude_desktop_config.json`.
- Windows config path: `%APPDATA%\Claude\claude_desktop_config.json`.
- Linux config path: `~/.config/Claude/claude_desktop_config.json`.
- Optional remote MCP bridge at `http://192.168.207.67:5003/sse`.
- A Claude Desktop build with MCP server support enabled.
- File permissions that prevent other users from reading the token.
- A full Claude Desktop restart after config edits.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Create the config file if it does not exist, then merge the `mnemos`
entry into the top-level `mcpServers` object.

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

On macOS, the final file is:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

On Windows, the final file is:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

On Linux, the final file is:

```text
~/.config/Claude/claude_desktop_config.json
```

If `mnemos` is not on PATH, replace the `command` value with the absolute
binary path, for example `/opt/mnemos/venv/bin/mnemos`.

For an HTTP/SSE setup, run `mnemos serve mcp-http --host 0.0.0.0 --port 5003`
on the MNEMOS host and use a remote MCP entry if your Claude Desktop build
supports it:

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

Restart Claude Desktop completely after editing the file. Closing only the
chat window is not enough on some builds.

## Verification — one curl or one tool-list call that proves registration worked

```bash
# Server up?
curl -fsS http://192.168.207.67:5002/health | jq -r '.status'    # → "healthy"

# Print the canonical MCP tool names from the live registry:
python3 -c 'from mnemos.mcp.tools import TOOL_REGISTRY; print("\n".join(sorted(TOOL_REGISTRY)))' | head
```

(MCP discovery is the protocol's `tools/list` JSON-RPC method over
SSE/stdio, not a REST endpoint.) After restart, Claude Desktop
should list `mnemos` in its MCP server view and expose tools such
as `search_memories`.

## Common gotchas — 2-4 bullets of real failure modes

- Invalid JSON prevents Claude Desktop from loading all MCP servers.
- Desktop apps launched from Finder may not inherit shell variables; use a
  wrapper or paste the resolved token into your private local config.
- Windows paths need double escaping only inside JSON string values.
- The HTTP/SSE alternative on `:5003` needs reverse-proxy SSE buffering off.

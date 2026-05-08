# Zed -> MNEMOS

Zed has native MCP support since late 2025 and can use MNEMOS by registering the MNEMOS HTTP/SSE endpoint in `~/.config/zed/settings.json`.

## What you need — token, host (192.168.207.67), relevant port(s)

- Zed `0.169` or newer; older builds silently ignore `mcp_servers`.
- MNEMOS MCP HTTP/SSE reachable at `http://192.168.207.67:5003/sse`.
- The connector bearer token for the Zed principal.
- Config path: `~/.config/zed/settings.json`.
- Network access from the Zed machine to `192.168.207.67`.
- A full Zed restart after changing MCP server config.
- A model profile in Zed that is allowed to use tools.
- A private config file; do not paste bearer-token settings into shared screenshots.

## Configuration snippet — full settings.json mcp_servers block

Merge this `mcp_servers` object into `~/.config/zed/settings.json`. If the
file already has settings, keep the outer object and add only the
`mcp_servers.mnemos` entry.

```json
{
  "mcp_servers": {
    "mnemos": {
      "url": "http://192.168.207.67:5003/sse",
      "headers": {
        "Authorization": "Bearer d3a3bc609583005f4a077b6ffd00154b4f03f70104d0cdbfbb019fceb28daca9"
      }
    }
  }
}
```

Zed loads MCP server registrations from user settings. Keep the MNEMOS REST
port (`:5002`) out of this entry; Zed talks to the MCP HTTP/SSE bridge on
`:5003`.

## Verification — one curl or one tool-list call that proves registration worked

Restart Zed after saving `~/.config/zed/settings.json`. Open the assistant or
agent tool view and confirm the `mnemos` server appears with memory and KG
tools.

You can verify the bridge directly before debugging Zed:

```bash
curl -fsS -H "Authorization: Bearer d3a3bc609583005f4a077b6ffd00154b4f03f70104d0cdbfbb019fceb28daca9" http://192.168.207.67:5003/sse
```

The authenticated request should open a `text/event-stream`.

## Common gotchas — 2-4 bullets of real failure modes

- Zed MCP support requires Zed `0.169` or newer; older versions silently
  ignore `mcp_servers`.
- A Zed window reload is not always enough; quit and restart Zed after
  changing `settings.json`.
- The HTTP/SSE bridge on `:5003` is plain HTTP unless TLS is provided by a
  reverse proxy or tunnel.
- Tool approval and deny lists should use exact MNEMOS registry names such
  as `search_memories`, not display-prefixed names.

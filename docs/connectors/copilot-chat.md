# GitHub Copilot Chat -> MNEMOS

GitHub Copilot Chat can partially reach MNEMOS through VS Code's chat participant and MCP configuration path, using the MNEMOS HTTP/SSE endpoint as the backing tool server.

## What you need — token, host (192.168.207.67), relevant port(s)

- VS Code with GitHub Copilot Chat installed and enabled.
- A Copilot Chat build that exposes chat participant MCP configuration.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS MCP HTTP/SSE reachable at `http://192.168.207.67:5003/sse`.
- Config path: user or workspace `settings.json`.
- Network access from VS Code to `192.168.207.67`.
- A VS Code window reload after changing settings.
- A model and policy profile that permits tool use.
- `curl` for bridge verification.

## Configuration snippet — VS Code settings.json participant config

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

What works today is a VS Code `settings.json` chat participant config that
registers MNEMOS via the HTTP/SSE endpoint. Use the user settings file for a
single developer machine, or a workspace settings file only when the token is
resolved from a private environment and is never committed.

```json
{
  "github.copilot.chat.participants": {
    "mnemos": {
      "type": "mcp",
      "transport": "sse",
      "url": "http://192.168.207.67:5003/sse",
      "headers": {
        "Authorization": "Bearer ${env:MNEMOS_TOKEN}"
      }
    }
  }
}
```

If your VS Code build uses the generic MCP settings surface instead of the
Copilot participant key, register the same endpoint and header under the
active `mcp.servers` key for that build.

### Limitations

Copilot Chat's tool-call protocol differs from canonical MCP. Some tool
shapes may not render in the chat UI. Attachment-style tools are fully
supported, but structured memory-search returns may be truncated.

For richer OpenAI-compatible tool calling, use the `mnemos-bridge-openai`
adapter once Phase 2 of the bridge consolidation lands. That path will provide
the runtime glue for structured Python integration while this guide remains
the VS Code configuration reference.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS -H "Authorization: Bearer $MNEMOS_TOKEN" http://192.168.207.67:5003/sse
```

The authenticated request should open a `text/event-stream`. Then reload the
VS Code window and ask Copilot Chat to use the `mnemos` participant to search
for a benign phrase such as `copilot connector smoke`.

## Common gotchas — 2-4 bullets of real failure modes

- Copilot Chat is not a canonical MCP client; unsupported tool shapes may be
  hidden or rendered as plain text.
- Workspace `settings.json` can leak tokens if committed; prefer user
  settings and `${env:MNEMOS_TOKEN}`.
- Some VS Code builds use a generic `mcp.servers` setting instead of the
  Copilot participant key.
- Remote SSE on `:5003` must stay unbuffered through proxies.

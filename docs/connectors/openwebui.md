# OpenWebUI -> MNEMOS

OpenWebUI has no native MCP client path for MNEMOS, so wire MNEMOS REST on `:5002` as a tool endpoint or a small OpenWebUI Function.

## What you need — token, host (192.168.207.67), relevant port(s)

- OpenWebUI running as an administrator-managed instance.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- No MCP server for OpenWebUI; do not use `:5003` for this surface.
- A model in OpenWebUI that supports tool/function calling.
- Access to OpenWebUI Admin Settings, Tools, or Functions.
- Optional access to `http://192.168.207.67:5002/openapi.json`.
- Python `requests` available inside the OpenWebUI container for Functions.
- A non-root MNEMOS token for shared team OpenWebUI deployments.
- Browser access to enable the tool per chat after registration.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

For an OpenAI-compatible tool-call shape, register a function schema that
maps model tool calls to MNEMOS REST. This minimal schema covers search:

```json
{
  "type": "function",
  "function": {
    "name": "search_memories",
    "description": "Search MNEMOS memories through the REST API.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Search text to send to MNEMOS."
        },
        "limit": {
          "type": "integer",
          "description": "Maximum number of memories to return.",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        }
      },
      "required": ["query"]
    }
  }
}
```

Point the implementation at:

```text
POST http://192.168.207.67:5002/v1/memories/search
Authorization: Bearer $MNEMOS_TOKEN
Content-Type: application/json
```

If using OpenWebUI Functions, create a Tool or Function with this minimal
Python body and set its valve token from your private environment:

```python
import os
import requests
from pydantic import BaseModel, Field

class Tools:
    class Valves(BaseModel):
        MNEMOS_BASE: str = Field(default="http://192.168.207.67:5002")
        MNEMOS_TOKEN: str = Field(default="$MNEMOS_TOKEN")

    def __init__(self):
        self.valves = self.Valves()

    def search_memories(self, query: str, limit: int = 5) -> str:
        """Search MNEMOS memories."""
        response = requests.post(
            f"{self.valves.MNEMOS_BASE.rstrip('/')}/v1/memories/search",
            headers={"Authorization": f"Bearer {self.valves.MNEMOS_TOKEN}"},
            json={"query": query, "limit": limit},
            timeout=20,
        )
        response.raise_for_status()
        return response.text
```

An OpenAPI-server style setup can also point OpenWebUI at
`http://192.168.207.67:5002/openapi.json`, then restrict exposed operations
to read tools such as memory search before allowing writes.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS -X POST http://192.168.207.67:5002/v1/memories/search -H "Authorization: Bearer $MNEMOS_TOKEN" -H "Content-Type: application/json" -d '{"query":"openwebui connector smoke","limit":3}'
```

In OpenWebUI, enable the registered tool in the chat's tool picker and ask
the selected model to search MNEMOS for the same phrase.

## Common gotchas — 2-4 bullets of real failure modes

- OpenWebUI does not speak MCP for this path; `mnemos serve mcp-http` on
  `:5003` will not register as an OpenWebUI tool.
- Functions run server-side Python; review code and restrict admin access.
- Tool schemas expose capability, but the Python or OpenAPI implementation
  still has to add the bearer token.
- Local models with weak function-calling support may ignore the tool schema.

## See also — `mnemos-bridge-openai`

The OpenAI-compatible tool-call path described above works at the
configuration level, but for proper MCP→OpenAI schema translation
(stripping incompatible JSON Schema keywords, wrapping each MNEMOS
tool as a function-call definition with the correct shape), use the
[`mnemos-bridge-openai`](https://gitlab.com/mnemos-os/mnemos-bridge-openai) package. It returns
ready-to-pass `tools=[...]` lists from `adapter.openai_tools()` and
handles the round-trip back through MCP via `adapter.handle_tool_call()`.
The bridge layers on top of `mnemos-bridge-core` and pairs naturally
with any OpenAI-SDK-compat client.

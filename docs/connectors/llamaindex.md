# LlamaIndex -> MNEMOS

LlamaIndex can load MNEMOS tools through `llama-index-tools-mcp` and expose them to an agent or query workflow as regular LlamaIndex tools.

## What you need — token, host (192.168.207.67), relevant port(s)

- Python 3.11+.
- `llama-index-core >= 0.11`.
- `llama-index-tools-mcp`.
- A LlamaIndex LLM integration for the agent runtime.
- MNEMOS MCP HTTP/SSE reachable at `http://192.168.207.67:5003/sse`.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- Network access from the LlamaIndex runtime to `192.168.207.67`.
- A policy decision about which write tools the agent may call.

## Configuration snippet — MCPToolSpec to a LlamaIndex agent

```bash
python -m pip install "llama-index-core>=0.11" llama-index-tools-mcp
```

```python
import asyncio
import os
from llama_index.core.agent import ReActAgent
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

async def main() -> None:
    client = BasicMCPClient(
        "http://192.168.207.67:5003/sse",
        headers={"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"},
    )
    tool_spec = McpToolSpec(client=client)
    tools = await tool_spec.to_tool_list_async()
    agent = ReActAgent.from_tools(tools, verbose=True)
    response = await agent.achat("Search MNEMOS for LlamaIndex notes.")
    print(response)

asyncio.run(main())
```

If your installed `llama-index-tools-mcp` release exports `MCPToolSpec`
instead of `McpToolSpec`, use that class name with the same client
configuration.

## Verification — one curl or one tool-list call that proves registration worked

```bash
python - <<'PY'
import asyncio, os
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

async def main():
    client = BasicMCPClient("http://192.168.207.67:5003/sse", headers={"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"})
    spec = McpToolSpec(client=client)
    print([tool.metadata.name for tool in await spec.to_tool_list_async()])

asyncio.run(main())
PY
```

The output should include `search_memories`.

## Common gotchas — 2-4 bullets of real failure modes

- `llama-index-tools-mcp` requires `llama-index-core >= 0.11`.
- Some package versions export `McpToolSpec` while docs may refer to
  `MCPToolSpec`; keep the client configuration the same.
- SSE proxies must not buffer the event stream.
- Agent loops can call read tools repeatedly; cap iterations and keep write
  approvals manual.

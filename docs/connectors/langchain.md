# LangChain -> MNEMOS

LangChain can load MNEMOS tools through `langchain-mcp-adapters` and pass them into an agent as standard LangChain tools.

## What you need — token, host (192.168.207.67), relevant port(s)

- Python 3.11+.
- `langchain >= 0.3` and `langchain-community >= 0.3`.
- `langchain-mcp-adapters` from
  `https://github.com/langchain-ai/langchain-mcp-adapters`.
- A chat model integration for the agent runtime.
- MNEMOS MCP HTTP/SSE reachable at `http://192.168.207.67:5003/sse`.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- Network access from the LangChain runtime to `192.168.207.67`.
- A policy decision about which write tools the agent may call.

## Configuration snippet — MultiServerMCPClient to a LangChain agent

```bash
python -m pip install "langchain>=0.3" "langchain-community>=0.3" langchain-mcp-adapters langchain-openai langgraph
```

```python
import asyncio
import os
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

async def main() -> None:
    client = MultiServerMCPClient({
        "mnemos": {
            "transport": "sse",
            "url": "http://192.168.207.67:5003/sse",
            "headers": {"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"},
        }
    })
    tools = await client.get_tools()
    agent = create_react_agent(ChatOpenAI(model="gpt-4.1-mini"), tools)
    result = await agent.ainvoke({"messages": [("user", "Search MNEMOS for LangChain notes.")]})
    print(result["messages"][-1].content)

asyncio.run(main())
```

Keep write tools behind explicit approval if this agent is allowed to create,
update, delete, or branch memories.

## Verification — one curl or one tool-list call that proves registration worked

```bash
python - <<'PY'
import asyncio, os
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    client = MultiServerMCPClient({"mnemos": {"transport": "sse", "url": "http://192.168.207.67:5003/sse", "headers": {"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"}}})
    print([tool.name for tool in await client.get_tools()])

asyncio.run(main())
PY
```

The output should include `search_memories`.

## Common gotchas — 2-4 bullets of real failure modes

- `langchain-mcp-adapters` requires `langchain >= 0.3` and
  `langchain-community >= 0.3`.
- LangChain tool names should use MNEMOS registry names exactly when applying
  allow or deny lists.
- SSE proxies must not buffer the event stream.
- Agent loops can call read tools repeatedly; cap iterations and keep write
  approvals manual.

# Ollama -> MNEMOS

Ollama has no native MCP client, so use its OpenAI-compatible API with tool-aware local models and the `mnemos-bridge-openai` adapter once that Phase 2 bridge package is available.

## What you need — token, host (192.168.207.67), relevant port(s)

- Ollama installed and running locally.
- A tool-aware Ollama model such as Llama 3.x, Qwen 2.5+, or Mistral Nemo+.
- Avoid base models without instruct fine-tuning; they generally do not emit
  reliable tool calls.
- Python 3.11+ with the OpenAI SDK.
- MNEMOS MCP HTTP/SSE reachable at `http://192.168.207.67:5003/sse`.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- The upcoming `mnemos-bridge-openai` adapter package from Phase 2 of bridge
  consolidation.
- Ollama's OpenAI-compatible endpoint reachable at `http://localhost:11434/v1`.
- A private shell environment for the token.

## Configuration snippet — OpenAI SDK against Ollama

Install Ollama, pull a tool-aware instruct model, then point a Python client at
Ollama's OpenAI-compatible API. The bridge adapter lists MNEMOS tools from the
HTTP/SSE endpoint and normalises tool calls for the client.

```bash
ollama pull qwen2.5:7b-instruct
python -m pip install openai mnemos-bridge-openai
```

```python
import os
from openai import OpenAI
from mnemos_bridge_openai import MnemosOpenAITools

mnemos = MnemosOpenAITools.from_sse(
    url="http://192.168.207.67:5003/sse",
    headers={"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"},
)

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
response = client.chat.completions.create(
    model="qwen2.5:7b-instruct",
    messages=[{"role": "user", "content": "Search MNEMOS for Ollama notes."}],
    tools=mnemos.tools(),
    tool_choice="auto",
)
print(mnemos.dispatch_tool_calls(response))
```

The required flow is three steps: install Ollama, point the OpenAI SDK at
`base_url="http://localhost:11434/v1"`, and list MNEMOS tools through the
`mnemos-bridge-openai` adapter.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS http://localhost:11434/v1/models
curl -fsS -H "Authorization: Bearer $MNEMOS_TOKEN" http://192.168.207.67:5003/sse
```

The first command proves the local OpenAI-compatible server is up. The second
authenticated request should open a `text/event-stream` from MNEMOS.

## Common gotchas — 2-4 bullets of real failure modes

- Ollama does not speak MCP directly; use the OpenAI-compatible tool-call path.
- Non-tool base models may ignore `tools` even when the HTTP API accepts the
  request.
- Ollama's tool-call stream format differs from OpenAI's; the
  `mnemos-bridge-openai` adapter normalises this automatically.
- Keep write tools behind explicit approval in any local automation loop.

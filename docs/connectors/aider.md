# Aider -> MNEMOS

Aider does not currently expose a native MCP-client configuration, so use MNEMOS REST directly and paste the retrieved context into Aider when needed.

## What you need — token, host (192.168.207.67), relevant port(s)

- Aider installed for your repo workflow.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://192.168.207.67:5002`.
- `curl` for quick memory search and write calls.
- Python 3 with `urllib.request` for a no-dependency helper.
- A shell where Aider and the helper commands share environment.
- A repo-local convention for pasting retrieved memory into Aider prompts.
- No MCP bridge is required for this path.
- No OpenAI-compatible proxy is required for this path.
- `jq` is optional but useful for readable curl output.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Use REST calls before or during an Aider session. The curl example searches
MNEMOS; the Python example creates a small memory from the command line.

```bash
export MNEMOS_BASE="http://192.168.207.67:5002"
export MNEMOS_TOKEN="${MNEMOS_TOKEN:?set MNEMOS_TOKEN first}"

curl -fsS -X POST "$MNEMOS_BASE/v1/memories/search" \
  -H "Authorization: Bearer $MNEMOS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"aider project context","limit":5}' | jq .

python3 - <<'PY'
import json
import os
import urllib.request

base = os.environ["MNEMOS_BASE"].rstrip("/")
token = os.environ["MNEMOS_TOKEN"]
payload = {
    "content": "Aider is using MNEMOS REST direct for this repository.",
    "category": "connector-notes",
    "metadata": {"surface": "aider", "transport": "rest-direct"},
}
request = urllib.request.Request(
    f"{base}/v1/memories",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.status)
    print(response.read().decode("utf-8"))
PY
```

Typical workflow:

```bash
aider
```

Then paste the search result into Aider with a prompt such as:

```text
Use this MNEMOS context while planning the change: <paste JSON summary here>
```

For repeat use, put the curl search into a private shell alias or script that
prints only the fields you want in Aider's prompt.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS -H "Authorization: Bearer $MNEMOS_TOKEN" http://192.168.207.67:5002/health
```

REST direct has no MCP registration to list; a healthy authenticated REST
call is the proof point for Aider.

## Common gotchas — 2-4 bullets of real failure modes

- Aider will not auto-call MNEMOS tools; you must fetch context yourself and
  paste or summarize it into the chat.
- `/web` is for web-page scraping, not authenticated bearer-token REST calls.
- Large raw JSON blobs waste context; summarize search hits before pasting.
- Keep write calls manual so Aider does not save noisy intermediate thoughts.

## See also — `mnemos-bridge-aider`

The path described above is "paste-into-Aider" — manual context fetching.
For automated tool dispatch (Aider's LLM autonomously calling MNEMOS),
install the [`mnemos-bridge-aider`](https://gitlab.com/mnemos-os/mnemos-bridge-aider)
package which provides both a CLI sidecar (`mnemos-aider search ...`) and
a Path B integration that hooks into Aider's command/tool surface.

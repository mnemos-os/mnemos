# Continue.dev → MNEMOS

> **Status: experimental.** Continue's MCP support landed in
> v0.9+; we test against v0.9.x and v1.0 release candidates.
> The stdio path is stable; HTTP/SSE inherits whatever stability
> the upstream MCP HTTP transport carries.

## What this gets you

Continue.dev's autocomplete + chat surface gains the 13 MNEMOS
MCP tools — search, create, update, delete, list, get_stats,
KG triples, bulk_create. Useful for capturing decisions during
inline-edit sessions ("Save this rationale as MNEMOS category=
architecture") and querying prior context across the same
memory pool every other agent uses.

## Prerequisites

- Continue.dev v0.9.0 or later. Earlier releases don't speak MCP.
- A running MNEMOS instance.
- Bearer token (`MNEMOS_API_KEY`).

## Setup — local stdio (recommended for desktop)

Continue's MCP config lives in `~/.continue/config.json` (the
same file that holds model + completion settings). Add a
``mcpServers`` block:

```json
{
  "models": [...existing...],
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://localhost:5002",
        "MNEMOS_API_KEY": "<your bearer token>"
      }
    }
  }
}
```

If your MNEMOS runs on a different host, change `MNEMOS_BASE`
to the LAN URL. The MCP server makes outbound HTTPS to that
base on every tool call.

## Setup — HTTP/SSE (remote MNEMOS, no SSH)

```json
{
  "mcpServers": {
    "mnemos": {
      "url": "https://mnemos.example.com/sse",
      "headers": {
        "Authorization": "Bearer <your bearer token>"
      }
    }
  }
}
```

## Restart Continue

After editing the config, do a full restart of the host editor
(VS Code, JetBrains IDE, etc.) — Continue caches MCP server
registrations at extension load time, not on subsequent file
edits.

## Smoke test

In Continue's chat panel:

```
@MNEMOS search for "smoke test"
```

Continue should auto-complete the `@MNEMOS` mention once the
MCP server is connected. The available tools (`search_memories`,
`create_memory`, etc.) appear in the tool drawer.

If the mention doesn't auto-complete, check Continue's MCP
panel (Settings → Continue Settings → MCP servers) — the
`mnemos` entry should show as `connected`.

## Troubleshooting

| Symptom                                  | Likely cause                                   | Fix                                                    |
|------------------------------------------|------------------------------------------------|--------------------------------------------------------|
| `@MNEMOS` mention doesn't auto-complete  | Config edited but Continue not restarted      | Quit the host IDE entirely, relaunch                   |
| MCP panel shows `failed to start`        | `mnemos` binary not on PATH                    | Use absolute path in `command` field                   |
| All tool calls return `MNEMOS UNREACHABLE` | Wrong `MNEMOS_BASE` URL                       | `curl <MNEMOS_BASE>/health` from a separate shell      |
| Tool calls return 401                    | Bearer token wrong / not in env                | Verify with `curl -H "Authorization: Bearer ..."`      |
| Continue freezes during a tool call      | MCP server hung on slow downstream             | Check MNEMOS `/metrics` for p99 latency on the route   |
| WSL2 + Continue-on-Windows: PATH drift   | Continue resolving `mnemos` via Windows PATH  | Use HTTP/SSE shape OR run Continue inside WSL2         |

## Memory namespace per-workspace

Wire ``MNEMOS_DEFAULT_NAMESPACE`` into the per-server env:

```json
{
  "mcpServers": {
    "mnemos-this-project": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "...",
        "MNEMOS_API_KEY": "...",
        "MNEMOS_DEFAULT_NAMESPACE": "<project-name>"
      }
    }
  }
}
```

Drop a ``.continuerc.json`` (or ``.continue/config.json``) at
the project root to override the global config per-workspace.

The env var is a **write stamp**, not an enforced scope (see
[claude-code.md](./claude-code.md) for details). For enforced
project isolation, provision per-project non-root **users**
(each with its own ``users.namespace``) and issue API keys for
those users — distinct keys under the same user share that
user's namespace.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- [claude-code.md](./claude-code.md) — same MCP shape, different
  config file location.
- [cursor.md](./cursor.md) — closest peer in IDE-integration
  shape.

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a12.*

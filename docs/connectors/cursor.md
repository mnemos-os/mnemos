# Cursor → MNEMOS

> **Status: stable.** Cursor's MCP support is mature and the
> setup mirrors the Claude Code stdio shape closely. Tested
> against Cursor v0.45+.

## What this gets you

Cursor's chat panel and inline-edit flows can read and write
the same MNEMOS memory other agents see. Useful for
cross-IDE memory continuity ("Search MNEMOS for the API key
the OpenAI helper sets up") and for capturing decisions
during a coding session ("Save this rationale to MNEMOS as
category=architecture").

## Prerequisites

- Cursor v0.45 or later (MCP support landed there).
- A running MNEMOS instance.
- Bearer token (`MNEMOS_API_KEY`).

## Setup — local stdio (recommended)

Cursor's MCP config lives at `~/.cursor/mcp.json` (macOS / Linux)
or `%APPDATA%\Cursor\User\globalStorage\anysphere.cursor\mcp.json`
(Windows / WSL2 — edit the WSL2 file from inside WSL).

```json
{
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

For a remote MNEMOS, change `MNEMOS_BASE` to the LAN URL.

## Setup — HTTP/SSE (remote, no SSH)

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

Cursor follows the standard MCP HTTP/SSE wire format; no Cursor-
specific quirks here.

## Restart Cursor

After editing the config, do a full restart (not just a window
reload) — Cursor caches MCP server registrations at startup.

## Smoke test

Open Cursor's chat panel, type:

```
@mnemos search "smoke test"
```

The `@mnemos` mention should auto-complete to the MCP server
once Cursor sees it. Tools `mnemos_search_memories`,
`mnemos_create_memory`, etc. should appear in the tool drawer.

If you don't see `@mnemos`, check Cursor's MCP server panel
(Settings → Cursor Settings → MCP) — it should show the
`mnemos` entry as `connected`.

## Troubleshooting

| Symptom                                  | Likely cause                                | Fix                                                    |
|------------------------------------------|---------------------------------------------|--------------------------------------------------------|
| `@mnemos` autocomplete doesn't appear    | Config edited but Cursor not fully restarted | Quit Cursor entirely, relaunch                         |
| MCP panel shows `failed to start`        | `mnemos` binary not on PATH                 | Use absolute path in `command` field                   |
| All tool calls return `MNEMOS UNREACHABLE` | Wrong `MNEMOS_BASE` URL                   | `curl <MNEMOS_BASE>/health` from a separate shell      |
| Tool calls return 401                    | Bearer token wrong / not in env             | Verify token via `curl -H "Authorization: Bearer ..."` |
| Cursor freezes on tool call              | MCP server hung waiting on slow downstream  | Check MNEMOS `/metrics` for p99 latency                |
| WSL2: tool calls fail with "no such file" | Cursor running on Windows but `mnemos` is in WSL2 | Use HTTP/SSE shape; OR Cursor-in-WSL2 with stdio    |

## Cursor-on-Windows + MNEMOS-in-WSL2

If Cursor runs on the Windows side but MNEMOS runs inside WSL2,
the stdio path doesn't work cleanly (Cursor would need to
`wsl.exe mnemos serve mcp-stdio` which has quoting hazards).
Two cleaner options:

1. **Run Cursor inside WSL2** — open Cursor from inside the WSL
   distro shell. The stdio shape works normally.
2. **Use HTTP/SSE** — bring up the MCP HTTP/SSE bridge inside
   WSL2 (it auto-forwards to `localhost:5004` on the Windows
   side). Point Cursor's config at `http://localhost:5004/sse`.

The HTTP/SSE path is preferred for most Windows users because it
doesn't require any WSL2 plumbing on Cursor's side.

## Memory namespace per-workspace

If you want different Cursor projects to have isolated memory
scopes, set ``MNEMOS_DEFAULT_NAMESPACE`` per server entry:

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

Drop a `.cursor/mcp.json` at the project root with the per-project
config; Cursor merges it with the global one. Cross-namespace
search is operator-controlled via the underlying REST API filters.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- [claude-code.md](./claude-code.md) — same stdio shape, different
  config file location.
- `MEMORY_ARCHITECTURE.md` §3 — namespace semantics.

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a11.*

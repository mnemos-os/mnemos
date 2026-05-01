# Cline → MNEMOS

> **Status: experimental.** Cline (formerly Claude Dev) is a
> VS Code extension that drives autonomous code-edit sessions.
> Tested against Cline v3.x. The stdio path is stable; HTTP/SSE
> inherits whatever stability the upstream MCP HTTP transport
> carries.

## What this gets you

Cline's autonomous-edit loop gains the 13 MNEMOS MCP tools.
Practically: Cline can search MNEMOS for prior architecture
decisions before suggesting an approach, save its own
decisions as memories during the session, and pull KG triples
for entity-aware reasoning.

## Prerequisites

- Cline v3.0 or later (MCP support landed in 3.x).
- A running MNEMOS instance.
- Bearer token (`MNEMOS_API_KEY`).

## Setup — local stdio (recommended)

Cline's MCP config is at one of:

- macOS: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- Linux: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- Windows / WSL2: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`

The simpler path: open Cline's settings panel from the VS Code
extension UI and edit the `mcpServers` block visually.

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://localhost:5002",
        "MNEMOS_API_KEY": "<your bearer token>"
      },
      "disabled": false,
      "autoApprove": [
        "search_memories",
        "list_memories",
        "get_memory",
        "get_stats"
      ]
    }
  }
}
```

The ``autoApprove`` field is a Cline-specific feature: tools
listed there don't trigger the per-call approval prompt that
Cline normally puts in front of every tool invocation. The list
above auto-approves READ-only tools while keeping write tools
(`create_memory`, `update_memory`, `delete_memory`,
`kg_create_triple`, `bulk_create_memories`) gated on operator
confirmation. Adjust to taste.

## Setup — HTTP/SSE (remote MNEMOS)

```json
{
  "mcpServers": {
    "mnemos": {
      "url": "https://mnemos.example.com/sse",
      "headers": {
        "Authorization": "Bearer <your bearer token>"
      },
      "disabled": false,
      "autoApprove": ["search_memories", "list_memories", "get_memory", "get_stats"]
    }
  }
}
```

## Restart VS Code

Cline's MCP server registrations are loaded once per VS Code
window. Reload the window (Ctrl/Cmd+Shift+P → "Developer:
Reload Window") after editing the config; full IDE restart is
not required.

## Smoke test

Open the Cline panel, start a new task:

```
> Search MNEMOS for "test memory" and tell me the first three results.
```

Cline should request approval for the `search_memories` tool
(unless you auto-approved it), then execute. If MNEMOS is
empty:

```
> Create a MNEMOS memory in category "test" with content "First Cline smoke test".
```

Cline will request approval for `create_memory`. After
approval, re-search to confirm.

## Troubleshooting

| Symptom                                  | Likely cause                                | Fix                                                    |
|------------------------------------------|---------------------------------------------|--------------------------------------------------------|
| MCP panel shows server as `disabled`     | `disabled: true` in config                  | Set `disabled: false` and reload window                |
| Tool calls return `MNEMOS UNREACHABLE`   | Wrong `MNEMOS_BASE` URL                     | `curl <MNEMOS_BASE>/health` from a separate shell      |
| Tool calls return 401                    | Bearer token wrong                          | Verify with curl                                       |
| Cline asks for approval on every read    | `autoApprove` not set                       | Add read-only tools to the array                       |
| `mnemos` binary not found                | Cline's PATH doesn't include the venv       | Use absolute path in `command`                         |
| WSL2 + Cline-on-Windows: spawn fails     | Same WSL2 path issue as Cursor              | Use HTTP/SSE shape OR run VS Code in WSL2 session      |

## Cline-specific: keep destructive tools manually-approved

Even when you trust Cline to autonomously edit code,
auto-approving `delete_memory` or `kg_delete_triple` is a
footgun — a single hallucinated tool call can wipe useful
context. The recommendation in the example config near the top
of this page keeps write/delete tools manually-approved.

### Auto-approving write tools? Provision a non-root user first

If you want to expand auto-approve to include write tools
(``create_memory`` / ``update_memory``), the prerequisite is a
**non-root user with its own ``users.namespace``** so the
permission boundary is enforced server-side. Setup:

1. **Server side (one-time)**, create the user + key:
   ```sql
   INSERT INTO users (id, role, namespace, display_name)
   VALUES ('cline-sandbox-user', 'user', 'cline-sandbox', 'Cline Sandbox');
   -- then issue an API key for that user via the admin path or
   -- direct INSERT into api_keys with user_id='cline-sandbox-user'
   ```
2. **Client side**, bind the new key + namespace stamp to the
   MCP server:
   ```json
   {
     "mcpServers": {
       "mnemos-cline-sandbox": {
         "command": "mnemos",
         "args": ["serve", "mcp-stdio"],
         "env": {
           "MNEMOS_BASE": "...",
           "MNEMOS_API_KEY": "<the new non-root key>",
           "MNEMOS_DEFAULT_NAMESPACE": "cline-sandbox"
         },
         "autoApprove": [
           "search_memories", "create_memory", "update_memory",
           "list_memories", "get_memory"
         ]
       }
     }
   }
   ```

With the non-root key + ``users.namespace``, the server enforces
the scope: even a hallucinated `update_memory` call against a
stale memory ID outside the namespace is rejected at the auth
seam, not just stamped at write time.

### What about just setting ``MNEMOS_DEFAULT_NAMESPACE`` with a root key?

Don't. The env var is a **WRITE STAMP** only — see the security
caveat in [claude-code.md](./claude-code.md). A root API key
with the env stamp will write new memories into the configured
namespace by default, but can still ``update_memory`` /
``delete_memory`` against ANY memory ID across all namespaces.
Auto-approving `update_memory` on a root key with only the env
stamp is the dangerous shape this section exists to prevent.

If you must run a root key (one-tenant install, no per-user
auth), do NOT extend ``autoApprove`` to ``update_memory`` or
the kg-write tools. The minimal example at the top of this page
covers the supported auto-approve set for the root-key shape.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- [continue-dev.md](./continue-dev.md) — the closest VS Code-
  extension peer.
- [claude-code.md](./claude-code.md) — same MCP shape, different
  IDE.

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a12.*

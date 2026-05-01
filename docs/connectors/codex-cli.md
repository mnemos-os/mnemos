# Codex CLI → MNEMOS

> **Status: experimental.** Codex CLI's MCP support landed in
> v0.125.0; we test against v0.126.0-alpha.1 + v0.127.x. The
> stdio path is stable; the HTTP/SSE path inherits whatever
> stability the upstream MCP HTTP transport carries.

## What this gets you

Codex CLI sessions can search and create MNEMOS memories
inline. Especially useful for the codex-companion review
workflow: codex's review output can be saved to MNEMOS as
provenance for the review gate, and prior reviews are
queryable when starting a new review.

## Prerequisites

- Codex CLI v0.125.0 or later. Older versions don't speak MCP.
  ```bash
  codex --version
  npx @openai/codex --version
  ```
- A running MNEMOS instance.
- Bearer token (`MNEMOS_API_KEY`).

## Setup — local stdio

Codex CLI's MCP config goes in `~/.codex/config.toml` under
`[mcp.servers.<name>]`:

```toml
[mcp.servers.mnemos]
command = "mnemos"
args = ["serve", "mcp-stdio"]
env = {
  MNEMOS_BASE = "http://localhost:5002",
  MNEMOS_API_KEY = "<your bearer token>",
}
```

If `mnemos` is not on PATH, use the absolute path in the
`command` field.

## Setup — HTTP/SSE (remote MNEMOS)

```toml
[mcp.servers.mnemos]
url = "https://mnemos.example.com/sse"
headers = { Authorization = "Bearer <your bearer token>" }
```

The HTTP shape is the right pick if MNEMOS runs on a different
host than the Codex CLI invocation, especially in headless / CI
contexts where SSH-spawning a Python subprocess is fragile.

## Restart Codex

```bash
codex /quit
codex
```

Codex caches MCP server registrations at process start.
Verify via:

```bash
codex /mcp list
```

The `mnemos` server should appear with status `ready`.

## Smoke test

```bash
codex
```

Then in the interactive session:

```
> Search MNEMOS for "test memory" and show me the first three results.
```

Codex will dispatch the `search_memories` tool through the MCP
bridge. If MNEMOS has nothing yet:

```
> Create a MNEMOS memory in category "test" with content "First codex smoke test".
```

Re-search to verify.

## Codex-companion adversarial-review integration

If you use the codex-companion workflow (`codex-companion.mjs
adversarial-review`) for review gates, you can wire MNEMOS as a
review-provenance backend:

```toml
[mcp.servers.mnemos]
command = "mnemos"
args = ["serve", "mcp-stdio"]
env = {
  MNEMOS_BASE = "http://localhost:5002",
  MNEMOS_API_KEY = "<token>",
}

[review.backends.mnemos]
mcp_server = "mnemos"
auto_save_findings = true
category = "review-findings"
```

After each review, the findings are persisted to MNEMOS as
category=`review-findings`. Future reviews can grep for
"have we seen this finding before" via search before the
LLM call fires.

## Troubleshooting

| Symptom                                | Likely cause                                | Fix                                                    |
|----------------------------------------|---------------------------------------------|--------------------------------------------------------|
| `codex /mcp list` shows no servers     | Config path wrong / not parsed              | `codex --debug-config-path` to see the active path     |
| `mnemos` shows status `failed`         | Stdio command not found                     | Run `mnemos serve mcp-stdio` directly; read the error |
| Tool calls return `MNEMOS UNREACHABLE` | Wrong `MNEMOS_BASE`                         | `curl <MNEMOS_BASE>/health` from the same shell        |
| Tool calls return 401                  | Bearer token wrong                          | Verify with curl                                       |
| Codex hangs on long tool calls         | MCP transport timeout                       | Set `[mcp] timeout = 120` in `~/.codex/config.toml`    |
| TOML parse error on launch             | Mismatched quotes / table syntax            | `codex --validate-config` (pre-v0.127) or paste TOML through `taplo lint` |

## Background-mode (codex exec)

`codex exec` and `codex exec --background` invocations inherit
the MCP server config. That means:

- Background tasks can call MNEMOS tools for context.
- The same bearer token is used across foreground and background
  modes — there's no per-mode key separation.

If you need per-task scoping, set ``MNEMOS_DEFAULT_NAMESPACE``
differently per env. The MCP bridge stamps this on every write
through the connector (see "Memory namespace per-workspace" in
[claude-code.md](./claude-code.md) for the underlying mechanism):

```bash
MNEMOS_DEFAULT_NAMESPACE=task-foo codex exec 'do work'
MNEMOS_DEFAULT_NAMESPACE=task-bar codex exec 'do other work'
```

Note: this stamps the namespace on writes but does NOT enforce
isolation against a root-scope API key — a root key can still
read/update/delete by-id across namespaces. For ENFORCED
isolation, provision distinct non-root **users** with
``users.namespace`` set on each user row, then issue an API key
per user and pair it with the right MCP entry. Distinct keys
under the same user share that user's namespace — isolation is
per-user, not per-key.

## Cross-references

- [README.md](./README.md) — connector subsystem framing.
- [claude-code.md](./claude-code.md) — same MCP shape, different
  config file location and surface.
- `MEMORY_ARCHITECTURE.md` §3 — namespace semantics.

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a11.*

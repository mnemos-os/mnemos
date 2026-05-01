# Claude Desktop → MNEMOS

> **Status: stable (stdio) / experimental (HTTP).** Claude Desktop is
> Anthropic's standalone app for macOS and Windows — separate from
> Claude Code (the CLI). The desktop app supports MCP servers via
> the JSON config at:
>
>   * macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
>   * Windows: `%APPDATA%\Claude\claude_desktop_config.json`
>
> The stdio path mirrors Claude Code; the HTTP/SSE path lets the
> same MNEMOS back multiple desktops.

## What this gets you

Claude Desktop conversations gain the MNEMOS MCP tools — see the
canonical exact-name table in [README.md](./README.md#canonical-mcp-tool-surface).
Memory is shared with every other MCP-aware client (Claude Code,
Cursor, Codex CLI, ChatGPT Pro Developer Mode) configured against
the same MNEMOS instance.

## Prerequisites

- Claude Desktop installed.
- A running MNEMOS instance — local for stdio, network-reachable
  for HTTP/SSE.
- Bearer token (`MNEMOS_API_KEY` from your install).

## Setup — local stdio (recommended for personal desktop)

### 1. Stage the MCP entrypoint

`mnemos` must be on the PATH that Claude Desktop sees. Confirm:

```bash
which mnemos
mnemos --version          # 4.2.0a14 or later
```

On macOS, Claude Desktop launches with a minimal PATH. If `which
mnemos` works in your shell but Claude Desktop reports
"command not found mnemos", point at the absolute path in step 2.

### 2. Edit `claude_desktop_config.json`

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

For a homelab MNEMOS, change `MNEMOS_BASE` to the LAN URL
(e.g., `http://192.168.1.50:5002`). The MCP server opens an
outbound HTTPS connection to that base URL on every tool call —
Claude Desktop never talks to MNEMOS directly.

If `mnemos` isn't on the desktop-launched PATH:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "/Users/<you>/.local/bin/mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://localhost:5002",
        "MNEMOS_API_KEY": "<your bearer token>"
      }
    }
  }
}
```

### 3. Restart Claude Desktop

Quit the app fully (⌘Q on macOS) and reopen. The MNEMOS tools
should appear in the conversation's MCP-tools panel. Confirm by
asking: "What MNEMOS tools are available?"

## Setup — HTTP/SSE (multi-machine deployments — experimental)

If you want one MNEMOS to back several Claude Desktops on
different machines (laptop + desktop + tablet), use the HTTP/SSE
transport. Same as the [ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md)
path — your MNEMOS needs a public HTTPS URL (Cloudflare Tunnel,
ngrok, reverse proxy with cert).

```json
{
  "mcpServers": {
    "mnemos": {
      "transport": "sse",
      "url": "https://mnemos.example.com/sse",
      "headers": {
        "Authorization": "Bearer <your bearer token>"
      }
    }
  }
}
```

> ⚠ Claude Desktop's HTTP/SSE config shape has been in flux across
> versions; if the example above fails, run `claude --version` and
> compare against the
> [Claude Desktop release notes](https://claude.com/download).
> Stdio remains the path with the fewest moving parts.

## Auto-approve recommended tool list

Claude Desktop's auto-approve doesn't currently expose a per-tool
allowlist (unlike Cline / Continue). It approves at the
server-name level: `mnemos` either runs without prompts or every
tool call asks. For a personal home install where you trust
yourself, enable the server. For a multi-user / shared install
keep prompts on.

## Verify

In a new conversation, ask:

> Search MNEMOS for any memories about my homelab.

Claude Desktop will call `search_memories`, fold results into
its answer, and (if auto-approve is off) ask before each call.

## Known caveats

- **No tool drawer prefix.** Unlike Cursor (which renames tools to
  `mnemos_search_memories` etc.), Claude Desktop shows the bare
  registry name. autoApprove / deny-list configs in other agents
  must use the bare names regardless.
- **Stdio environment is minimal.** macOS launches Claude Desktop
  with a stripped PATH; if `mnemos` lives in a Homebrew or pyenv
  prefix that the GUI launcher doesn't see, use the absolute
  command path in `claude_desktop_config.json`.
- **HTTP/SSE config drift.** Anthropic has been iterating the
  remote-transport shape. If the example above doesn't work,
  check the current docs and report back so we can update this
  guide.

## Cross-references

- [Claude Code](./claude-code.md) — the CLI variant; same MCP
  shape but lives in `~/.claude.json`.
- [Connector gallery README](./README.md) — landing page.
- [ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md) —
  HTTPS/SSE / Custom Connector path on a different agent surface.

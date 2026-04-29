# MNEMOS integrations

Drop-in bundles wiring MNEMOS memory into external agent frameworks.

Each bundle contains:

- A **skill file** teaching the agent when and how to use MNEMOS.
- A **config snippet** for the framework's MCP / hook system.
- A **README** with install steps specific to that framework.

All bundles talk to MNEMOS through the stdio MCP transport (`mnemos serve
mcp-stdio`, implemented by `mnemos.mcp.stdio`). **You must set `MNEMOS_BASE`**
to your MNEMOS deployment's URL — there is no default. MNEMOS is network
infrastructure; localhost is not a meaningful fallback.

| Bundle | Discovery | Access | Enforcement |
|--------|-----------|--------|-------------|
| `claude-code/` | skill via `.mcp.json` | MCP | **Hooks** — SessionStart/UserPromptSubmit inject memories before the model speaks (strongest: pre-inference) |
| `openclaw/` | `.agents/skills/mnemos-memory/SKILL.md` | MCP | `AGENTS.md.snippet` appended to repo root AGENTS.md (progressive-disclosure directive) |
| `zeroclaw/` | `.claude/skills/mnemos-memory/SKILL.md` | `.mcp.json` | `AGENTS.md.snippet` appended to repo root AGENTS.md |
| `hermes/` | `optional-skills/memory/mnemos/SKILL.md` | `~/.hermes/config.yaml` (`hermes mcp add`) | `hermes skills enable mnemos --global` (no required-skill tier exists) |

**Enforcement matters.** MCP + skill gives an agent *awareness* of MNEMOS. It does not compel use. Each framework's enforcement layer is what turns "the agent could query memory" into "the agent must query memory before committing to an approach." Install all three artifacts per framework.

## Deployment model

MNEMOS is designed to run as a shared network service on a dedicated host (a "memory server" the way a database server is dedicated). Every agent — Claude Code, OpenClaw, ZeroClaw, Hermes — connects to the same MNEMOS over HTTP.

Pick your deployment URL:

- **Unified MNEMOS+GRAEAE:** `http://mnemos.internal:5002`
- **Loopback-only personal dev:** `http://127.0.0.1:5002`

Use the same `MNEMOS_BASE` value everywhere. If you run the `server` profile
with auth enabled, set `MNEMOS_API_KEY` too.

## Authentication

Set `MNEMOS_API_KEY` in the MCP server's environment; the v4 MCP transport
attaches `Authorization: Bearer $MNEMOS_API_KEY` on every outbound HTTP request
when the var is set, and omits the header when it isn't. For unauthenticated
edge/dev installs, leave it unset.

The bundled MCP config examples may still omit `MNEMOS_API_KEY` — add it to the `env` block of each framework's MCP server entry if you run team or enterprise profile:

```jsonc
{
  "mcpServers": {
    "mnemos": {
      "command": "/opt/mnemos/venv/bin/mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://mnemos.internal:5002",
        "MNEMOS_API_KEY": "mnemos_..."   // only if MNEMOS has auth enabled
      }
    }
  }
}
```

The Claude Code hooks (bundle in `claude-code/hooks/`) already handle `MNEMOS_API_KEY` correctly — they attach the Bearer header when the key is set and omit it when unset.

Alternative transports for operators who don't want the token in a local env file:

1. Run the MCP server on the same host as MNEMOS with MNEMOS bound to loopback (`listen_host = "127.0.0.1"`). Local MCP connects without auth; external network can't reach MNEMOS directly.
2. Use SSH transport: `command: ssh` and `args: ["user@host", "/opt/mnemos/venv/bin/mnemos", "serve", "mcp-stdio"]`. Credentials ride the SSH channel, not the MCP config.

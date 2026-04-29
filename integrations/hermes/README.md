# MNEMOS + Hermes

## Install

1. Copy the skill into Hermes' optional-skills tree:
   ```bash
   mkdir -p optional-skills/memory/mnemos
   cp integrations/hermes/optional-skills/memory/mnemos/SKILL.md \
      optional-skills/memory/mnemos/SKILL.md
   ```

2. Register MNEMOS as an MCP server via the Hermes CLI:
   ```bash
   hermes mcp add mnemos \
     --command /path/to/mnemos/venv/bin/mnemos \
     --args "serve mcp-stdio" \
     --env MNEMOS_BASE=http://mnemos.internal:5002
   ```

   Or merge `mcp_servers.example.yaml` into `~/.hermes/config.yaml` under the `mcp_servers` key. Replace `<your-mnemos-host>` with your MNEMOS deployment URL — there is no default.

3. Verify the connection:
   ```bash
   hermes mcp list
   hermes mcp test mnemos
   ```

## What the skill does

Full Hermes skill frontmatter (version, author, license, tags, related_skills, prerequisites). Teaches Hermes agents when to search MNEMOS, when to store, and how to avoid acting on stale data.

## Enforce globally (important)

Installing the skill makes it *available*; it does not make the agent *use* it. Hermes skills ship under `optional-skills/` and default to opt-in. To make MNEMOS memory part of every Hermes session across all profiles:

```bash
# Enable the skill as a global default
hermes skills enable mnemos --global

# Verify
hermes skills list --enabled
```

Hermes does not have a "required skill" tier — agents still choose when to invoke the skill's tools. But enabling it globally guarantees the skill description is in every agent's discovery pool, and the skill's **When to Use** section gives the agent strong guidance on when memory operations are expected.

For teams standardizing on MNEMOS, also consider adding an entry to your Hermes profile's system prompt instructing agents to consult the `mnemos` skill before architectural decisions and after resolving non-trivial problems.

## Prerequisites

MNEMOS reachable at whatever URL you set in `MNEMOS_BASE`. There is no default. See the MNEMOS setup guide.

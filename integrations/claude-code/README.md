# MNEMOS + Claude Code

Two integration modes. You can use either or both.

## Prerequisite

Set `MNEMOS_BASE` to your MNEMOS deployment's URL. **There is no default** — MNEMOS is network infrastructure, not an embedded library. Typical values:

- `http://mnemos.internal:5002` — unified MNEMOS+GRAEAE
- `http://127.0.0.1:5002` — loopback-only personal dev

## 1. MCP server (recommended)

Gives Claude Code direct tool access to MNEMOS: `search_memories`, `create_memory`, `update_memory`, plus knowledge-graph and rehydration tools.

### Per-project install

```bash
cp integrations/claude-code/mcp.example.json ./.mcp.json
# edit .mcp.json — set MNEMOS_BASE and the absolute path to the mnemos CLI
```

Claude Code auto-detects `.mcp.json` on session start.

### Per-user install

Merge the `mcpServers.mnemos` entry from `mcp.example.json` into `~/.claude/settings.json`.

## 2. Session hooks (optional)

Three hook scripts:

| Script | Event | What it does |
|--------|-------|--------------|
| `hooks/mnemos-session-start.sh` | SessionStart | Queries MNEMOS for recent memories and injects a compact context block |
| `hooks/mnemos-user-prompt-submit.sh` | UserPromptSubmit | Semantic retrieval on the prompt; injects top-3 matches |
| `hooks/mnemos-stop.sh` | Stop | Posts the session transcript to `/ingest/session` |

All three **noop silently** when `MNEMOS_BASE` is unset, and attach the `Authorization: Bearer $MNEMOS_API_KEY` header only when that var is set. Safe to install before MNEMOS is reachable.

### Install (idempotent)

```bash
./install.sh
```

This script:

- Copies the three hook scripts to `~/.claude/mnemos-hooks/`
- Copies `hooks.config.example` → `~/.claude/mnemos-hooks.config` **only if absent** (never overwrites)
- Merges hook entries into `~/.claude/settings.json` via jq, deduplicating by command path
- Backs up `settings.json` before modifying

Re-running is safe — already-present hooks are not duplicated.

### Configure

After `install.sh`, edit `~/.claude/mnemos-hooks.config`:

```bash
export MNEMOS_BASE="http://mnemos.internal:5002"     # required
export MNEMOS_API_KEY=""                             # optional (team/enterprise)
```

Then source it in your shell init so Claude Code picks it up:

```bash
echo '[ -f "$HOME/.claude/mnemos-hooks.config" ] && source "$HOME/.claude/mnemos-hooks.config"' >> ~/.zshrc
```

### Verify

```bash
# Dry-run a hook (should exit 0 and either emit context or print {})
MNEMOS_BASE=http://mnemos.internal:5002 ~/.claude/mnemos-hooks/mnemos-session-start.sh <<< '{}'

# After Claude Code restart, check hook activity
tail -n 20 /tmp/mnemos-hooks.log
```

### Uninstall

```bash
./uninstall.sh
```

Removes hook entries from `settings.json` but leaves `~/.claude/mnemos-hooks/` scripts and the config file in place.

## Environment variables

| Var | Required? | Purpose |
|-----|-----------|---------|
| `MNEMOS_BASE` | **yes** — hooks noop without it | MNEMOS REST endpoint |
| `MNEMOS_API_KEY` | optional | Bearer key for team/enterprise profiles |
| `MNEMOS_CONTEXT_LIMIT` | default 5 | Max memories injected on SessionStart |
| `MNEMOS_SEARCH_LIMIT` | default 3 | Max results injected on UserPromptSubmit |
| `MNEMOS_SEARCH_MIN_CHARS` | default 20 | Min prompt length for retrieval |
| `MNEMOS_SESSION_CATEGORY` | default `session_activity` | Category tag for transcripts |
| `MNEMOS_HOOK_LOG` | default `/tmp/mnemos-hooks.log` | Hook activity log |

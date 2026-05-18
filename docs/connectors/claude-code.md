# Claude Code → MNEMOS

Claude Code can use MNEMOS as a shared memory layer by SSH-spawning the MNEMOS stdio MCP server from `~/.claude.json`.

## Requirements

- MNEMOS bearer token (see `~/.api_keys_master.json` or your shell env)
- MNEMOS REST reachable at `http://192.168.207.67:5002` (v5.x unified port)
- SSH access from Claude Code machine to `192.168.207.67`
- mnemos package installed at `/opt/mnemos` with virtualenv at `/opt/mnemos/venv`

## Configuration

Merge into `~/.claude.json`. The MCP process runs on the remote host; `MNEMOS_BASE` defaults to `localhost:5002` which is correct from there.

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "ssh",
      "args": [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "jasonperlow@192.168.207.67",
        "/usr/bin/env",
        "PYTHONPATH=/opt/mnemos",
        "MNEMOS_API_KEY=<your-token-here>",
        "/opt/mnemos/venv/bin/python",
        "-m", "mnemos.mcp.stdio"
      ]
    }
  }
}
```

## Notes

- Port 5002 is the unified API port (MNEMOS + GRAEAE) as of v5.x. Port 5001 is retired.
- `PYTHONPATH=/opt/mnemos` is required — the package is editable, not installed in venv site-packages.
- `MNEMOS_API_KEY` must be in the SSH args via `/usr/bin/env`; Claude Code's `env` block is local-only and does not cross the SSH boundary.
- GRAEAE MCP tool not yet registered — use `POST http://192.168.207.67:5002/graeae/consult` with Bearer token as fallback.

## Idempotent fix script (for pre-v5.x configs)

```bash
python3 ~/.claude/scripts/fix-mnemos-mcp-auth.py
```

Safe to run multiple times.

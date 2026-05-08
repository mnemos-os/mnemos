#!/usr/bin/env python3
"""
MNEMOS MCP Server — Model Context Protocol interface to MNEMOS memory system.

Transport: stdio (Claude Code spawns this process directly)
Backend:   MNEMOS REST API (default http://localhost:5002, override via MNEMOS_BASE env var)

For remote MNEMOS (e.g. from macOS connecting to api-host):
  Set MNEMOS_BASE=http://<host>:5002 in the MCP server config,
  or use SSH transport: command=ssh, args=[user@host,
  /path/to/mnemos/venv/bin/python, /path/to/mnemos/mcp_server.py]

IMPORTANT: All logging must go to stderr. Any stdout output corrupts MCP JSON-RPC framing.
"""
import asyncio
import json
import logging
import sys
from typing import Any

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from mnemos.mcp.tools import TOOL_REGISTRY, execute_tool, tool_input_schema, user_from_context

# Stderr-only logging — stdout is reserved for JSON-RPC frames
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("mnemos-mcp")

app = Server("mnemos")


# ── Tool registry ─────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=tool["description"], inputSchema=tool_input_schema(tool))
        for name, tool in TOOL_REGISTRY.items()
    ]


# ── Tool dispatch ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await execute_tool(name, arguments or {}, user=user_from_context())
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except httpx.HTTPStatusError as e:
        detail = {}
        try:
            detail = e.response.json()
        except Exception:
            detail = {"raw": e.response.text[:500]}
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e), "detail": detail}, indent=2),
        )]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e)}, indent=2),
        )]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        # Round-3 residual #2 of #146 (#149): drain in-flight MCP
        # audit persist tasks before the loop closes. Without this,
        # stdio bridges can deliver the tool result and exit while
        # the audit HTTP POST is still in-flight — asyncio.run would
        # cancel the task and silently lose the row.
        from mnemos.mcp.tools._security import drain_pending_audit_tasks

        try:
            # #163: log the drain count for parity with the http bridge —
            # stdio operators get the same shutdown observability
            # ("drained N pending mcp_audit_log persist tasks").
            #
            # round-2: stdio's basicConfig sets level=WARNING (line 29),
            # so logger.info would be suppressed. Use logger.warning
            # because a non-zero drain count IS noteworthy: it means
            # audit writes were still lagging when the process exited.
            # The http bridge runs under uvicorn (INFO by default) so
            # logger.info works there; here, warning is the right
            # level to surface under stdio's tighter WARNING floor.
            drained = await drain_pending_audit_tasks(timeout=5.0)
            if drained:
                logger.warning(
                    "drained %d pending mcp_audit_log persist task(s) on shutdown",
                    drained,
                )
        except Exception:
            # Drain failures must NOT propagate; the underlying
            # logger entry is the always-on surface.
            logger.exception("mcp_audit drain on stdio shutdown failed")


if __name__ == "__main__":
    asyncio.run(main())

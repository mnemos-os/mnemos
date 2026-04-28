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

from api.mcp_tools import TOOL_REGISTRY, execute_tool, tool_input_schema

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
        result = await execute_tool(name, arguments or {})
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
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())

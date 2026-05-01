#!/usr/bin/env python3
"""
knossos_mcp.py — MemPalace-compatible MCP server backed by MNEMOS.

KNOSSOS is the shim MemPalace users install when their memory
workflow outgrows local-first single-user. Tool names, argument
names, and response shapes match MemPalace's wire contract; every
call is routed to a MNEMOS /v1/* REST backend with bearer auth.

Transport: stdio (same as mempalace.mcp_server). Registers with
Claude Code as:

    claude mcp add knossos -- python -m mnemos.tools.knossos_mcp

Environment:
    MNEMOS_BASE         — MNEMOS API base URL (default: http://localhost:5002)
    MNEMOS_API_KEY      — Bearer token (required when MNEMOS auth enabled)
    KNOSSOS_WING_AXIS   — 'owner_id' (default) or 'namespace'; controls which
                          MNEMOS tenancy axis maps to the MemPalace 'wing' field
    KNOSSOS_DEFAULT_WING — value used as 'default' when a tool call omits wing

See docs/KNOSSOS.md for the terminology map and team-feature call-outs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

# stdout must carry ONLY MCP JSON-RPC frames (same constraint as
# MemPalace's server — some transitive imports print banners to
# stdout and break the protocol). Redirect before heavy imports.
_REAL_STDOUT = sys.stdout
try:
    _REAL_STDOUT_FD = os.dup(1)
    os.dup2(2, 1)
except (OSError, AttributeError):
    _REAL_STDOUT_FD = None
sys.stdout = sys.stderr

import httpx  # noqa: E402
import mcp.types as types  # noqa: E402
from mnemos.core.config import get_settings  # noqa: E402
from mnemos.mcp.tools._runtime import _safe_path_segment, _safe_path_value  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("knossos-mcp")

_SETTINGS = get_settings()
MNEMOS_BASE = _SETTINGS.server.base.rstrip("/")
MNEMOS_API_KEY = _SETTINGS.server.api_key
# Default wing_axis is 'namespace': that's the only MNEMOS tenancy
# axis the /v1/memories/search endpoint accepts as a filter today.
# 'owner_id' remains configurable for tooling that talks to root
# (which can pass owner_id on mutations), but search/list scoping
# requires namespace. Codex caught the prior owner_id default silently
# dropping wing scope on every search/list call.
WING_AXIS = _SETTINGS.tools.knossos_wing_axis
DEFAULT_WING = _SETTINGS.tools.knossos_default_wing

if WING_AXIS not in ("owner_id", "namespace"):
    raise SystemExit(
        f"KNOSSOS_WING_AXIS must be 'owner_id' or 'namespace' (got {WING_AXIS!r})"
    )

# Reserved predicate prefix for MemPalace tunnels projected onto
# the MNEMOS KG.
TUNNEL_PREDICATE_PREFIX = "tunnel:"

server = Server("knossos")


# ─── HTTP helpers ───────────────────────────────────────────────────────────


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if MNEMOS_API_KEY:
        h["Authorization"] = f"Bearer {MNEMOS_API_KEY}"
    return h


async def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{MNEMOS_BASE}{path}", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: Any) -> Any:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{MNEMOS_BASE}{path}", json=body, headers=_headers())
        r.raise_for_status()
        if not r.content:
            return {}
        return r.json()


async def _patch(path: str, body: Any) -> Any:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(f"{MNEMOS_BASE}{path}", json=body, headers=_headers())
        r.raise_for_status()
        return r.json()


async def _delete(path: str) -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{MNEMOS_BASE}{path}", headers=_headers())
        r.raise_for_status()
        return r.status_code


# ─── Wing/room mapping helpers ──────────────────────────────────────────────


def _wing_filter(wing: Optional[str]) -> Dict[str, str]:
    """Translate a 'wing' value into MNEMOS tenancy filter kwargs."""
    if not wing:
        return {}
    return {WING_AXIS: wing}


def _mem_to_drawer(mem: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a MNEMOS memory row into MemPalace's 'drawer' response shape."""
    return {
        "id": mem.get("id"),
        "wing": mem.get(WING_AXIS) or DEFAULT_WING,
        "room": mem.get("category"),
        "content": mem.get("content"),
        "tags": (mem.get("metadata") or {}).get("tags", []) or [],
        "created": mem.get("created"),
        "updated": mem.get("updated"),
        "metadata": mem.get("metadata") or {},
    }


# ─── Tool implementations ───────────────────────────────────────────────────


async def t_status(args: Dict[str, Any]) -> Any:
    stats = await _get("/stats")
    total = stats.get("total_memories", 0)
    by_category = stats.get("memories_by_category") or {}
    return {
        "total_drawers": total,
        "wings": {},     # filled in below
        "rooms": by_category,
        "source": "knossos (MNEMOS backend)",
    }


async def t_list_wings(_args: Dict[str, Any]) -> Any:
    # MNEMOS doesn't expose a wings-axis aggregate today, so fan out
    # via /v1/memories and group by the axis column. Capped at 10_000
    # records via MNEMOS's export limit.
    env = await _get("/v1/export", params={"limit": 10_000})
    counts: Dict[str, int] = {}
    for rec in env.get("records") or []:
        p = rec.get("payload") or {}
        w = p.get(WING_AXIS) or DEFAULT_WING
        counts[w] = counts.get(w, 0) + 1
    return {"wings": [{"name": w, "drawer_count": n} for w, n in sorted(counts.items())]}


async def t_list_rooms(args: Dict[str, Any]) -> Any:
    wing = args.get("wing")
    env = await _get("/v1/export", params={"limit": 10_000})
    rooms: Dict[str, int] = {}
    for rec in env.get("records") or []:
        p = rec.get("payload") or {}
        if wing and p.get(WING_AXIS) != wing:
            continue
        room = p.get("category") or "uncategorized"
        rooms[room] = rooms.get(room, 0) + 1
    return {
        "wing": wing,
        "rooms": [{"name": r, "drawer_count": n} for r, n in sorted(rooms.items())],
    }


async def t_get_taxonomy(_args: Dict[str, Any]) -> Any:
    env = await _get("/v1/export", params={"limit": 10_000})
    tree: Dict[str, Dict[str, int]] = {}
    for rec in env.get("records") or []:
        p = rec.get("payload") or {}
        w = p.get(WING_AXIS) or DEFAULT_WING
        r = p.get("category") or "uncategorized"
        tree.setdefault(w, {}).setdefault(r, 0)
        tree[w][r] += 1
    return {
        "taxonomy": [
            {
                "wing": w,
                "rooms": [{"name": r, "drawer_count": n}
                          for r, n in sorted(rooms.items())],
            }
            for w, rooms in sorted(tree.items())
        ]
    }


async def t_search(args: Dict[str, Any]) -> Any:
    query = args.get("query", "")
    k = int(args.get("k", args.get("max_results", 5)))
    wing = args.get("wing")
    room = args.get("room")
    max_distance = float(args.get("max_distance", 1.5))
    body: Dict[str, Any] = {"query": query, "limit": k, "semantic": True}
    if room:
        body["category"] = room
    if wing:
        body.update(_wing_filter(wing))
    resp = await _post("/v1/memories/search", body)
    hits = resp.get("memories") or []
    drawers = []
    for m in hits:
        d = _mem_to_drawer(m)
        score = m.get("score")
        if score is not None and score > max_distance:
            continue
        d["score"] = score
        drawers.append(d)
    return {"query": query, "count": len(drawers), "drawers": drawers}


async def t_check_duplicate(args: Dict[str, Any]) -> Any:
    content = args.get("content", "")
    threshold = float(args.get("threshold", 0.9))
    # Translate a cosine-similarity threshold to a cosine-distance
    # threshold for MNEMOS's search (which reports distance).
    max_distance = max(0.0, 1.0 - threshold)
    resp = await _post(
        "/v1/memories/search",
        {"query": content[:500], "limit": 3, "semantic": True},
    )
    hits = resp.get("memories") or []
    best = None
    for m in hits:
        score = m.get("score")
        if score is not None and score <= max_distance:
            best = m
            break
    return {
        "is_duplicate": best is not None,
        "match": _mem_to_drawer(best) if best else None,
    }


async def t_list_drawers(args: Dict[str, Any]) -> Any:
    wing = args.get("wing")
    room = args.get("room")
    limit = int(args.get("limit", 100))
    params: Dict[str, Any] = {"limit": limit}
    if room:
        params["category"] = room
    if wing:
        params.update(_wing_filter(wing))
    resp = await _get("/v1/memories", params=params)
    mems = resp if isinstance(resp, list) else resp.get("memories") or []
    return {"count": len(mems), "drawers": [_mem_to_drawer(m) for m in mems]}


async def t_get_drawer(args: Dict[str, Any]) -> Any:
    drawer_id = args.get("drawer_id") or args.get("id")
    if not drawer_id:
        return {"error": "drawer_id is required"}
    try:
        safe_id = _safe_path_segment(drawer_id, label="drawer_id")
    except ValueError as exc:
        return {"error": str(exc)}
    mem = await _get(f"/v1/memories/{safe_id}")
    return _mem_to_drawer(mem)


async def t_add_drawer(args: Dict[str, Any]) -> Any:
    wing = args.get("wing") or DEFAULT_WING
    room = args.get("room") or "imported"
    content = args.get("content")
    if not content:
        return {"error": "content is required"}
    metadata = dict(args.get("metadata") or {})
    if args.get("tags"):
        metadata["tags"] = args["tags"]
    body = {
        "content": content,
        "category": room,
        "metadata": metadata,
        **_wing_filter(wing),
    }
    resp = await _post("/v1/memories", body)
    return _mem_to_drawer(resp)


async def t_update_drawer(args: Dict[str, Any]) -> Any:
    drawer_id = args.get("drawer_id") or args.get("id")
    if not drawer_id:
        return {"error": "drawer_id is required"}
    try:
        safe_id = _safe_path_segment(drawer_id, label="drawer_id")
    except ValueError as exc:
        return {"error": str(exc)}
    body: Dict[str, Any] = {}
    if "content" in args:
        body["content"] = args["content"]
    if "room" in args:
        body["category"] = args["room"]

    # MemPalace callers pass tags as a top-level field; MNEMOS has no
    # tags column on MemoryUpdateRequest — tags live in metadata.tags.
    #
    # The server's PATCH path REPLACES the full metadata object
    # (not a JSON merge patch), so naively sending
    # `metadata={"tags": [...]}` would erase every other key the
    # memory already carries (distillation_success, source, ...).
    # Round-trip read: GET the memory first, merge caller's metadata
    # + tags into the existing object, PATCH the merged result.
    # Skip the merge round-trip when the caller isn't touching
    # metadata OR tags at all — avoids a wasted fetch on
    # content/room-only updates.
    caller_meta = args.get("metadata")
    caller_tags = args.get("tags")
    if caller_meta is not None or caller_tags is not None:
        try:
            existing = await _get(f"/v1/memories/{safe_id}")
            merged = dict((existing or {}).get("metadata") or {})
        except Exception:
            # If the read fails, fall back to sending just what the
            # caller passed. Losing merge is better than blocking the
            # update; the warning is visible in server logs.
            merged = {}
        if isinstance(caller_meta, dict):
            merged.update(caller_meta)
        if caller_tags is not None:
            merged["tags"] = caller_tags
        body["metadata"] = merged

    resp = await _patch(f"/v1/memories/{safe_id}", body)
    return _mem_to_drawer(resp)


async def t_delete_drawer(args: Dict[str, Any]) -> Any:
    drawer_id = args.get("drawer_id") or args.get("id")
    if not drawer_id:
        return {"error": "drawer_id is required"}
    try:
        safe_id = _safe_path_segment(drawer_id, label="drawer_id")
    except ValueError as exc:
        return {"error": str(exc)}
    status = await _delete(f"/v1/memories/{safe_id}")
    return {"ok": 200 <= status < 300, "status": status}


# ─── Knowledge-graph tools ──────────────────────────────────────────────────


async def t_kg_add(args: Dict[str, Any]) -> Any:
    body = {
        "subject": args.get("subject"),
        "predicate": args.get("predicate"),
        "object": args.get("object"),
        "valid_from": args.get("valid_from"),
        "metadata": {"source_drawer_id": args.get("closet_id")} if args.get("closet_id") else {},
    }
    resp = await _post("/v1/kg/triples", body)
    return resp


async def t_kg_query(args: Dict[str, Any]) -> Any:
    entity = args.get("entity")
    as_of = args.get("as_of")
    direction = args.get("direction", "both")
    params = {"entity": entity, "direction": direction}
    if as_of:
        params["as_of"] = as_of
    return await _get("/v1/kg/triples", params=params)


async def t_kg_invalidate(args: Dict[str, Any]) -> Any:
    body = {
        "subject": args.get("subject"),
        "predicate": args.get("predicate"),
        "object": args.get("object"),
        "valid_until": args.get("valid_until"),
    }
    return await _post("/v1/kg/triples/invalidate", body)


async def t_kg_timeline(args: Dict[str, Any]) -> Any:
    subject = args.get("subject")
    if not subject:
        return {"error": "subject is required"}
    try:
        safe_subject = _safe_path_value(subject, label="subject")
    except ValueError as exc:
        return {"error": str(exc)}
    return await _get(f"/v1/kg/timeline/{safe_subject}")


async def t_kg_stats(_args: Dict[str, Any]) -> Any:
    stats = await _get("/stats")
    return {"triples": stats.get("total_kg_triples", 0)}


# ─── Tool registry ──────────────────────────────────────────────────────────


TOOLS: Dict[str, Dict[str, Any]] = {
    "mempalace_status": {
        "description": "Palace overview — total drawers, wing and room counts.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_status,
    },
    "mempalace_list_wings": {
        "description": "List all wings with drawer counts.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_list_wings,
    },
    "mempalace_list_rooms": {
        "description": "List rooms within a wing (or all rooms if no wing given).",
        "inputSchema": {
            "type": "object",
            "properties": {"wing": {"type": "string"}},
        },
        "handler": t_list_rooms,
    },
    "mempalace_get_taxonomy": {
        "description": "Full taxonomy: wing → room → drawer count.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_taxonomy,
    },
    "mempalace_search": {
        "description": (
            "Semantic search. Returns verbatim drawer content with "
            "similarity scores. Accepts MemPalace-compatible "
            "wing/room/max_distance filters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer"},
                "max_results": {"type": "integer"},
                "wing": {"type": "string"},
                "room": {"type": "string"},
                "max_distance": {"type": "number"},
            },
            "required": ["query"],
        },
        "handler": t_search,
    },
    "mempalace_check_duplicate": {
        "description": "Check if content already exists in the palace before filing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "threshold": {"type": "number"},
            },
            "required": ["content"],
        },
        "handler": t_check_duplicate,
    },
    "mempalace_list_drawers": {
        "description": "List drawers, optionally scoped by wing/room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string"},
                "room": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        "handler": t_list_drawers,
    },
    "mempalace_get_drawer": {
        "description": "Fetch a single drawer by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"drawer_id": {"type": "string"}},
            "required": ["drawer_id"],
        },
        "handler": t_get_drawer,
    },
    "mempalace_add_drawer": {
        "description": "File verbatim content into the palace. Does not check duplicates — call mempalace_check_duplicate first if needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string"},
                "room": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object"},
            },
            "required": ["content"],
        },
        "handler": t_add_drawer,
    },
    "mempalace_update_drawer": {
        "description": "Update an existing drawer's content / room / tags / metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string"},
                "content": {"type": "string"},
                "room": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object"},
            },
            "required": ["drawer_id"],
        },
        "handler": t_update_drawer,
    },
    "mempalace_delete_drawer": {
        "description": "Delete a drawer by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"drawer_id": {"type": "string"}},
            "required": ["drawer_id"],
        },
        "handler": t_delete_drawer,
    },
    "mempalace_kg_add": {
        "description": "Add a triple to the knowledge graph with optional temporal validity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "valid_from": {"type": "string"},
                "closet_id": {"type": "string"},
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": t_kg_add,
    },
    "mempalace_kg_query": {
        "description": "Query the knowledge graph for an entity's relationships.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "as_of": {"type": "string"},
                "direction": {"type": "string"},
            },
            "required": ["entity"],
        },
        "handler": t_kg_query,
    },
    "mempalace_kg_invalidate": {
        "description": "Mark a KG triple as no longer valid.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "valid_until": {"type": "string"},
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": t_kg_invalidate,
    },
    "mempalace_kg_timeline": {
        "description": "Chronological timeline of facts for an entity.",
        "inputSchema": {
            "type": "object",
            "properties": {"subject": {"type": "string"}},
            "required": ["subject"],
        },
        "handler": t_kg_timeline,
    },
    "mempalace_kg_stats": {
        "description": "Knowledge graph overview.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_kg_stats,
    },
}


# ─── MCP server wiring ──────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=spec["description"],
            inputSchema=spec["inputSchema"],
        )
        for name, spec in TOOLS.items()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    spec = TOOLS.get(name)
    if spec is None:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"unknown tool: {name}"}),
        )]
    try:
        result = await spec["handler"](arguments or {})
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500] if e.response is not None else ""
        result = {
            "error": "upstream_error",
            "status": e.response.status_code if e.response is not None else None,
            "body": body,
        }
    except Exception as e:
        logger.exception("tool %s failed", name)
        result = {"error": "exception", "type": type(e).__name__, "message": str(e)}
    return [types.TextContent(type="text", text=json.dumps(result))]


async def main() -> None:
    # Restore the real stdout for protocol framing.
    if _REAL_STDOUT_FD is not None:
        try:
            os.dup2(_REAL_STDOUT_FD, 1)
        except OSError:
            pass
    sys.stdout = _REAL_STDOUT
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

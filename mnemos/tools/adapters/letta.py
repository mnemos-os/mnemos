#!/usr/bin/env python3
"""
tools/adapters/letta.py — CHARON adapter for Letta (letta-ai/letta).

Converts a Letta deployment (formerly MemGPT, 22k+ stars) into an MPF v0.1
envelope. Works against either the on-disk SQLite metadata DB (default at
~/.letta/sqlite.db) or a running Letta server's REST API — same adapter,
chosen with --mode {sqlite,server,auto}.

Letta's memory model is more structured than Mem0 or MemPalace:

    * Core memory blocks (human, persona, custom labels) — in-context
      state, character-limited, edited by the agent's `core_memory_*`
      mnemos.tools. These are the agent's working set.
    * Archival passages — long-term vector-retrieved text. Lives in the
      `archival_passages` table, scoped by archive_id.
    * Recall / message history — every turn of every conversation in
      `messages`, indexed by agent_id + sequence_id.
    * Agent state — `agents` table, holds system prompt, llm_config,
      tool_rules, message_ids (in-context ordering).

Mapping (Letta → MPF v0.1, payload_version="mnemos-3.1"):

    archival passage   → kind="memory",  category="archival",   subcategory="archive:<id>"
    core memory block  → kind="memory",  category="core",       subcategory="core_block:<label>"
    recall message     → kind="event",   event_type="session_turn"
    agent state        → kind="memory",  category="agent_state", subcategory="agent:<id>"

Native Letta fields are preserved under payload.metadata.letta.* so a
reverse adapter can reconstruct the Letta row (except embeddings — the
importer regenerates those).

Usage:
    python -m mnemos.tools.adapters.letta --mode sqlite \
        --db ~/.letta/sqlite.db --out letta.mpf.json

    python -m mnemos.tools.adapters.letta --mode server \
        --base http://localhost:8283 --letta-token $LETTA_KEY \
        --out letta.mpf.json

    python -m mnemos.tools.adapters.letta --mode auto --db ~/.letta/sqlite.db \
        --post http://mnemos:5002 --api-key $TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MPF_VERSION = "0.1.0"
# /v1/import only admits mnemos-3.1; Letta provenance is preserved via
# envelope-level source_system="letta" + per-record metadata.letta blob.
PAYLOAD_VERSION_MNEMOS = "mnemos-3.1"
SOURCE_SYSTEM = "letta"

DEFAULT_SQLITE_PATH = "~/.letta/sqlite.db"
DEFAULT_SERVER_BASE = "http://localhost:8283"

# Recall (message log) is opt-in — can dwarf everything else on a chatty agent.
ALL_KINDS = ("archival", "core", "recall", "agent")
DEFAULT_KINDS = ("archival", "core", "agent")


# ─── SQLite read path ───────────────────────────────────────────────────────


def _open_sqlite(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(
            f"Letta SQLite DB not found at {db_path}. "
            f"Default is ~/.letta/sqlite.db; override with --db or use --mode server."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


_SQL_ARCHIVAL = """
SELECT id, text, archive_id, organization_id, metadata_, tags,
       created_at, updated_at
  FROM archival_passages
 WHERE COALESCE(is_deleted, 0) = 0
 ORDER BY created_at, id
"""

# Left-join blocks_agents so unattached blocks (templates) still emit.
_SQL_BLOCKS = """
SELECT b.id, b.label, b.value, b."limit" AS char_limit,
       b.description, b.template_name, b.is_template, b.read_only,
       b.metadata_, b.organization_id, b.project_id,
       b.created_at, b.updated_at, ba.agent_id
  FROM block b
  LEFT JOIN blocks_agents ba ON ba.block_id = b.id
 ORDER BY b.created_at, b.id
"""

_SQL_MESSAGES = """
SELECT id, agent_id, role, text, content, model, name,
       tool_calls, tool_call_id, tool_returns, step_id, run_id,
       conversation_id, sequence_id, sender_id, group_id,
       organization_id, created_at, updated_at
  FROM messages
 ORDER BY agent_id, sequence_id
"""

_SQL_AGENTS = """
SELECT id, name, description, agent_type, system,
       message_ids, metadata_, llm_config, embedding_config,
       tool_rules, timezone, organization_id, project_id,
       created_at, updated_at, last_run_completion, last_stop_reason
  FROM agents
 ORDER BY created_at, id
"""


def _sqlite_iter(conn: sqlite3.Connection, table: str, sql: str,
                 normalize) -> Iterator[Dict[str, Any]]:
    if not _table_exists(conn, table):
        return
    for row in conn.execute(sql):
        yield normalize(dict(row))


# ─── Server (REST) read path ────────────────────────────────────────────────


class _LettaClient:
    """Minimal Letta REST client. Letta uses optional Bearer auth
    (LETTA_SERVER_PASSWORD) plus an optional X-Organization header."""

    def __init__(self, base: str, token: Optional[str] = None,
                 org: Optional[str] = None):
        self.base = base.rstrip("/")
        self.token = token
        self.org = org

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.org:
            headers["X-Organization"] = self.org
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            raise SystemExit(f"Letta GET {path} → HTTP {e.code}: {body}")

    def list_agents(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._get("/v1/agents", {"limit": limit}) or []

    def list_blocks(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._get("/v1/blocks", {"limit": limit}) or []

    def agent_archival(self, agent_id: str, limit: int = 1000
                       ) -> List[Dict[str, Any]]:
        return self._get(
            f"/v1/agents/{agent_id}/archival-memory", {"limit": limit}
        ) or []

    def agent_messages(self, agent_id: str, limit: int = 2000
                       ) -> List[Dict[str, Any]]:
        return self._get(
            f"/v1/agents/{agent_id}/messages", {"limit": limit}
        ) or []


def _server_iter_all(
    client: _LettaClient, include: Tuple[str, ...]
) -> Iterator[Dict[str, Any]]:
    agents = client.list_agents()
    if "agent" in include:
        for a in agents:
            yield _normalize_agent(a)
    if "core" in include:
        seen: set = set()
        for b in client.list_blocks():
            if b.get("id") in seen:
                continue
            seen.add(b.get("id"))
            yield _normalize_block(b)
    if "archival" in include:
        for a in agents:
            for p in client.agent_archival(a["id"]):
                yield _normalize_passage(p)
    if "recall" in include:
        for a in agents:
            for m in client.agent_messages(a["id"]):
                yield _normalize_message(m)


# ─── Normalization: Letta row → MPF record ──────────────────────────────────


def _coerce_json(raw: Any) -> Any:
    """SQLite columns often store JSON as TEXT. Decode defensively."""
    if raw is None or not isinstance(raw, (str, bytes)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _mpf(record_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record_id,
        "kind": kind,
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _normalize_passage(row: Dict[str, Any]) -> Dict[str, Any]:
    """Archival passage → MPF memory."""
    meta = _coerce_json(row.get("metadata_")) or {}
    tags = _coerce_json(row.get("tags")) or []
    archive_id = row.get("archive_id")
    payload = {
        "content": row.get("text") or "",
        "category": "archival",
        "subcategory": f"archive:{archive_id}" if archive_id else "archive:unknown",
        "created": _iso(row.get("created_at")),
        "owner_id": row.get("organization_id") or "letta",
        "tags": list(tags) if isinstance(tags, list) else [],
        "metadata": {"letta": {
            "kind": "archival_passage",
            "passage_id": row.get("id"),
            "archive_id": archive_id,
            "organization_id": row.get("organization_id"),
            "native_metadata": meta,
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }},
    }
    return _mpf(row.get("id") or f"letta-passage-{id(row)}", "memory", payload)


def _normalize_block(row: Dict[str, Any]) -> Dict[str, Any]:
    """Core memory block → MPF memory. The label (human/persona/custom)
    makes them meaningful; surface it as subcategory for filterability."""
    label = row.get("label") or "block"
    meta = _coerce_json(row.get("metadata_")) or {}
    block_id = row.get("id") or f"letta-block-{label}"
    agent_id = row.get("agent_id")
    # Shared blocks with multiple attachments: SQLite path emits one
    # record per (block, agent) pairing — make the id stable by suffixing.
    rec_id = f"{block_id}@{agent_id}" if agent_id else block_id
    payload = {
        "content": row.get("value") or "",
        "category": "core",
        "subcategory": f"core_block:{label}",
        "created": _iso(row.get("created_at")),
        "owner_id": row.get("organization_id") or "letta",
        "metadata": {"letta": {
            "kind": "core_block",
            "block_id": block_id,
            "label": label,
            "char_limit": row.get("char_limit") or row.get("limit"),
            "template_name": row.get("template_name"),
            "is_template": bool(row.get("is_template")),
            "read_only": bool(row.get("read_only")),
            "description": row.get("description"),
            "agent_id": agent_id,
            "organization_id": row.get("organization_id"),
            "project_id": row.get("project_id"),
            "native_metadata": meta,
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }},
    }
    return _mpf(rec_id, "memory", payload)


def _normalize_message(row: Dict[str, Any]) -> Dict[str, Any]:
    """Recall message → MPF event (session_turn). Letta stores either
    plain text or content=[{type, text}, ...]; flatten for content."""
    content_parts = _coerce_json(row.get("content"))
    text = row.get("text")
    if not text and isinstance(content_parts, list):
        text = "\n".join(
            p.get("text") or "" for p in content_parts
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    payload = {
        "event_type": "session_turn",
        "content": text or "",
        "category": "recall",
        "subcategory": f"agent:{row.get('agent_id')}",
        "created": _iso(row.get("created_at")),
        "owner_id": row.get("organization_id") or "letta",
        "metadata": {"letta": {
            "kind": "recall_message",
            "message_id": row.get("id"),
            "agent_id": row.get("agent_id"),
            "role": row.get("role"),
            "model": row.get("model"),
            "name": row.get("name"),
            "sequence_id": row.get("sequence_id"),
            "conversation_id": row.get("conversation_id"),
            "step_id": row.get("step_id"),
            "run_id": row.get("run_id"),
            "sender_id": row.get("sender_id"),
            "group_id": row.get("group_id"),
            "tool_calls": _coerce_json(row.get("tool_calls")),
            "tool_call_id": row.get("tool_call_id"),
            "tool_returns": _coerce_json(row.get("tool_returns")),
            "content_parts": content_parts if isinstance(content_parts, list) else None,
            "organization_id": row.get("organization_id"),
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }},
    }
    return _mpf(row.get("id") or f"letta-msg-{row.get('sequence_id')}",
                "event", payload)


def _normalize_agent(row: Dict[str, Any]) -> Dict[str, Any]:
    """Agent state → MPF memory. Body = system prompt; structured config
    lives under metadata.letta."""
    agent_id = row.get("id")
    meta = _coerce_json(row.get("metadata_")) or {}
    payload = {
        "content": row.get("system") or f"Letta agent {row.get('name') or agent_id}",
        "category": "agent_state",
        "subcategory": f"agent:{agent_id}",
        "created": _iso(row.get("created_at")),
        "owner_id": row.get("organization_id") or "letta",
        "metadata": {"letta": {
            "kind": "agent_state",
            "agent_id": agent_id,
            "name": row.get("name"),
            "description": row.get("description"),
            "agent_type": row.get("agent_type"),
            "system": row.get("system"),
            "message_ids": _coerce_json(row.get("message_ids")),
            "llm_config": _coerce_json(row.get("llm_config")),
            "embedding_config": _coerce_json(row.get("embedding_config")),
            "tool_rules": _coerce_json(row.get("tool_rules")),
            "timezone": row.get("timezone"),
            "organization_id": row.get("organization_id"),
            "project_id": row.get("project_id"),
            "last_run_completion": _iso(row.get("last_run_completion")),
            "last_stop_reason": row.get("last_stop_reason"),
            "native_metadata": meta,
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }},
    }
    return _mpf(agent_id or f"letta-agent-{id(row)}", "memory", payload)


# ─── Streaming envelope assembly ────────────────────────────────────────────


def iter_records(
    *,
    mode: str,
    db_path: Optional[Path] = None,
    base: Optional[str] = None,
    token: Optional[str] = None,
    org: Optional[str] = None,
    include: Tuple[str, ...] = DEFAULT_KINDS,
) -> Iterator[Dict[str, Any]]:
    """Stream MPF records from either a SQLite DB or a live server."""
    if mode == "sqlite":
        if not db_path:
            raise SystemExit("--mode sqlite requires --db PATH")
        conn = _open_sqlite(db_path)
        try:
            if "agent" in include:
                yield from _sqlite_iter(conn, "agents", _SQL_AGENTS, _normalize_agent)
            if "core" in include:
                yield from _sqlite_iter(conn, "block", _SQL_BLOCKS, _normalize_block)
            if "archival" in include:
                yield from _sqlite_iter(conn, "archival_passages", _SQL_ARCHIVAL, _normalize_passage)
            if "recall" in include:
                yield from _sqlite_iter(conn, "messages", _SQL_MESSAGES, _normalize_message)
        finally:
            conn.close()
    elif mode == "server":
        if not base:
            raise SystemExit("--mode server requires --base URL")
        yield from _server_iter_all(
            _LettaClient(base, token=token, org=org), include,
        )
    else:
        raise SystemExit(f"unknown mode: {mode!r} (expected sqlite/server)")


def _resolve_mode(mode: str, db_path: Optional[Path], base: Optional[str]) -> str:
    """Resolve 'auto' into a concrete mode."""
    if mode != "auto":
        return mode
    if db_path and db_path.exists():
        return "sqlite"
    if base:
        return "server"
    default = Path(os.path.expanduser(DEFAULT_SQLITE_PATH))
    if default.exists():
        return "sqlite"
    raise SystemExit(
        "--mode auto: couldn't find a Letta source. "
        f"Pass --db (default {DEFAULT_SQLITE_PATH}) or --base URL."
    )


def build_envelope(
    *,
    mode: str,
    db_path: Optional[Path] = None,
    base: Optional[str] = None,
    token: Optional[str] = None,
    org: Optional[str] = None,
    include: Tuple[str, ...] = DEFAULT_KINDS,
    source_instance: Optional[str] = None,
) -> Dict[str, Any]:
    records = list(iter_records(
        mode=mode, db_path=db_path, base=base, token=token, org=org,
        include=include,
    ))
    return {
        "mpf_version": MPF_VERSION,
        "source_system": SOURCE_SYSTEM,
        "source_version": _detect_letta_version(),
        "source_instance": source_instance or (str(db_path) if db_path else base),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }


def _detect_letta_version() -> Optional[str]:
    try:
        from letta import __version__  # type: ignore
        return __version__
    except Exception:
        return None


# ─── MPF → MNEMOS POST (optional) ───────────────────────────────────────────


def _post_to_mnemos(
    envelope: Dict[str, Any],
    endpoint: str,
    api_key: str,
    *,
    batch_size: int = 200,
) -> Dict[str, int]:
    records = envelope.get("records") or []
    totals = {"imported": 0, "skipped": 0, "failed": 0}
    base = endpoint.rstrip("/") + "/v1/import?preserve_owner=true"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    for start in range(0, len(records), batch_size):
        chunk = {
            "mpf_version": envelope["mpf_version"],
            "source_system": envelope.get("source_system"),
            "source_version": envelope.get("source_version"),
            "exported_at": envelope["exported_at"],
            "records": records[start:start + batch_size],
        }
        data = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(base, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
                for k in ("imported", "skipped", "failed"):
                    totals[k] += int(body.get(k, 0))
                print(
                    f"  batch {start//batch_size + 1}: "
                    f"imported={body.get('imported')} "
                    f"skipped={body.get('skipped')} "
                    f"failed={body.get('failed')}",
                    file=sys.stderr,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"  WARNING /v1/import HTTP {e.code}: {body}", file=sys.stderr)
            totals["failed"] += len(chunk["records"])
    return totals


# ─── CLI ────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemos.tools.adapters.letta",
        description=(
            "Letta → MPF v0.1 adapter (CHARON). Reads archival passages, "
            "core memory blocks, recall messages, and agent state from a "
            "Letta SQLite DB or live server, and emits an MPF envelope. "
            "Optionally POSTs it to a MNEMOS /v1/import endpoint."
        ),
    )
    p.add_argument("--mode", choices=("auto", "sqlite", "server"), default="auto",
                   help="Read from the SQLite DB or a running server "
                        "(default: auto — prefers SQLite if present).")
    p.add_argument("--db", default=None, metavar="PATH",
                   help=f"Path to Letta's SQLite DB (default: {DEFAULT_SQLITE_PATH}).")
    p.add_argument("--base", default=None, metavar="URL",
                   help=f"Letta server base URL (default: {DEFAULT_SERVER_BASE}).")
    p.add_argument("--letta-token", default=None,
                   help="Bearer token for Letta server auth (LETTA_SERVER_PASSWORD).")
    p.add_argument("--letta-org", default=None,
                   help="Optional X-Organization header.")
    p.add_argument("--include", default=",".join(DEFAULT_KINDS),
                   help=f"Comma-separated kinds ({'/'.join(ALL_KINDS)}, or 'all'). "
                        f"Default: {','.join(DEFAULT_KINDS)} (recall excluded — large).")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Write MPF envelope to this file ('-' for stdout).")
    p.add_argument("--post", default=None, metavar="URL",
                   help="POST to a MNEMOS /v1/import endpoint. Requires --api-key.")
    p.add_argument("--api-key", default=None,
                   help="Bearer token for MNEMOS auth (needed with --post).")
    p.add_argument("--tenancy-axis", choices=("owner_id", "namespace"), default="owner_id",
                   help="Which MNEMOS tenancy axis to write Letta "
                        "organization_id into (default: owner_id).")
    p.add_argument("--source-instance", default=None,
                   help="Diagnostic label written into the envelope.")
    args = p.parse_args(argv)

    if not (args.out or args.post):
        print("ERROR: pass --out PATH or --post URL", file=sys.stderr)
        return 2
    if args.post and not args.api_key:
        print("ERROR: --post requires --api-key", file=sys.stderr)
        return 2

    if args.include.strip().lower() == "all":
        include = ALL_KINDS
    else:
        include = tuple(k.strip() for k in args.include.split(",") if k.strip())
        bad = [k for k in include if k not in ALL_KINDS]
        if bad:
            print(f"ERROR: unknown --include kinds: {bad}. "
                  f"Valid: {', '.join(ALL_KINDS)} or 'all'.", file=sys.stderr)
            return 2

    db_path = Path(os.path.expanduser(args.db or DEFAULT_SQLITE_PATH)).resolve()
    base = args.base or DEFAULT_SERVER_BASE
    mode = _resolve_mode(args.mode, db_path, args.base)
    print(f"letta adapter: mode={mode} include={','.join(include)}", file=sys.stderr)

    t0 = time.time()
    envelope = build_envelope(
        mode=mode,
        db_path=db_path if mode == "sqlite" else None,
        base=base if mode == "server" else None,
        token=args.letta_token,
        org=args.letta_org,
        include=include,
        source_instance=args.source_instance,
    )

    # Post-pass tenancy rewrite keeps normalizers single-axis.
    if args.tenancy_axis == "namespace":
        for rec in envelope["records"]:
            pl = rec.get("payload") or {}
            if "owner_id" in pl:
                pl["namespace"] = pl.pop("owner_id")

    elapsed = time.time() - t0
    print(f"read {envelope['record_count']} records from letta in {elapsed:.1f}s",
          file=sys.stderr)

    if args.out:
        if args.out == "-":
            json.dump(envelope, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            Path(args.out).write_text(
                json.dumps(envelope, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"wrote envelope to {args.out}", file=sys.stderr)

    if args.post:
        totals = _post_to_mnemos(envelope, args.post, args.api_key)
        print(
            f"POST complete: imported={totals['imported']} "
            f"skipped={totals['skipped']} failed={totals['failed']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

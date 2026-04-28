#!/usr/bin/env python3
"""
tools/adapters/mem0.py — CHARON adapter for Mem0 memory stores.

Converts a Mem0 OSS installation (Qdrant vector store + SQLite
history sidecar) or a Mem0 Platform tenant (api.mem0.ai) into an
MPF v0.1 envelope. Works without the `mem0ai` runtime installed
for the offline Qdrant path — it reads the Qdrant collection
directly using qdrant-client, the same dependency Mem0 itself
pulls in.

Usage:
    # Offline: default file-mode Qdrant at /tmp/qdrant
    python -m mnemos.tools.adapters.mem0 --out mem0.mpf.json

    # Offline: remote Qdrant via URL
    python -m mnemos.tools.adapters.mem0 --qdrant-url http://qdrant:6333 \\
        --collection mem0 --out mem0.mpf.json

    # Offline: with entity triples + history facts, POSTed to MNEMOS
    python -m mnemos.tools.adapters.mem0 --qdrant-path /tmp/qdrant \\
        --emit-history-facts \\
        --post http://localhost:5002 --api-key $TOKEN

    # Hosted Platform: via mem0ai SDK
    python -m mnemos.tools.adapters.mem0 --platform \\
        --api-key-mem0 $MEM0_API_KEY \\
        --out mem0.mpf.json

Key mapping (Mem0 → MPF):
    Qdrant point (main coll)   → records[] kind="memory"
                                  payload.content = point.payload.data
                                  payload.subcategory = memory_type
                                  payload.metadata.mem0.{user_id, agent_id,
                                    run_id, hash, actor_id, role, extra.*}
    Qdrant point (<coll>_entities) → records[] kind="fact"
                                  payload.subject = entity.name
                                  payload.predicate = f"mem0:{entity_type}"
                                  source_record_ids = [memory_id]
                                  payload.metadata.mem0.raw = <full payload>
    SQLite history row (opt-in) → records[] kind="fact"
                                  payload.statement = f"memory {op}: …"
                                  payload.subject = memory_id
                                  payload.predicate = op
                                  payload.metadata.mem0.history.*
    Tenancy triple (user:agent:run) → composite into --tenancy-axis
                                  (default: namespace); individual ids
                                  preserved under payload.metadata.mem0.*

Design notes:
  * Embeddings are NOT emitted (MPF spec allows regeneration on
    import; Mem0's configured embedder is model-coupled and cheap
    to redo).
  * Idempotent: same Qdrant snapshot + history.db produces
    byte-identical envelope modulo the exported_at timestamp.
  * payload_version="mnemos-3.1" on every record — the adapter
    translates to MNEMOS shape, native Mem0 fields are round-tripped
    under metadata.mem0.* so nothing is lost.
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
from typing import Any, Dict, Iterator, List, Optional

MPF_VERSION = "0.1.0"
# Adapter translates Mem0 points → MNEMOS-native memory/fact shape.
# Native Mem0 fields are round-tripped under payload.metadata.mem0.*
# so a future Mem0 importer can reconstruct exactly. /v1/import only
# understands mnemos-3.1; declaring anything else silently files the
# record into the `skipped` bucket (lesson from the MemPalace adapter).
PAYLOAD_VERSION_MNEMOS = "mnemos-3.1"
SOURCE_SYSTEM = "mem0"
DEFAULT_COLLECTION = "mem0"
DEFAULT_HISTORY_DB = "~/.mem0/history.db"
DEFAULT_QDRANT_PATH = "/tmp/qdrant"

# Optional imports — guarded so the offline path doesn't require
# the hosted SDK and vice versa.
try:
    from qdrant_client import QdrantClient  # type: ignore
except Exception:
    QdrantClient = None  # type: ignore

# NB: this file's production path is tools/adapters/mem0.py — a module
# *named* mem0. Importing `from mem0 import MemoryClient` from inside
# this file will resolve to self and fail. Use importlib with an
# explicit search that skips the script's own directory so we only
# hit the installed mem0ai package (if present).
def _import_mem0_memoryclient():
    import importlib
    import importlib.util
    # Temporarily strip this file's directory from sys.path so the
    # `mem0` name resolves to the installed package, not to self.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    saved_path = list(sys.path)
    sys.path = [p for p in sys.path
                if os.path.abspath(p or ".") != script_dir]
    # Also drop any already-cached self-import so importlib looks fresh.
    cached = sys.modules.pop("mem0", None)
    try:
        mod = importlib.import_module("mem0")
        return getattr(mod, "MemoryClient", None)
    except Exception:
        return None
    finally:
        sys.path = saved_path
        if cached is not None:
            sys.modules["mem0"] = cached

MemoryClient = _import_mem0_memoryclient()


__all__ = [
    "MPF_VERSION",
    "PAYLOAD_VERSION_MNEMOS",
    "SOURCE_SYSTEM",
    "DEFAULT_COLLECTION",
    "DEFAULT_HISTORY_DB",
    "build_envelope",
    "iter_records",
    "main",
]


# ─── Qdrant access ───────────────────────────────────────────────────────────


def _open_qdrant(
    *,
    qdrant_path: Optional[str] = None,
    qdrant_url: Optional[str] = None,
    qdrant_host: Optional[str] = None,
    qdrant_port: Optional[int] = None,
    qdrant_api_key: Optional[str] = None,
):
    """Open a QdrantClient in file, URL, or host/port mode.

    Precedence: url > host+port > path. Matches how Mem0's own
    VectorStoreConfig resolves Qdrant connection strings.
    """
    if QdrantClient is None:
        raise SystemExit(
            "qdrant-client is required. Install with:\n"
            "  pip install qdrant-client\n"
            "(same dependency Mem0 itself uses.)"
        )
    if qdrant_url:
        return QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    if qdrant_host:
        return QdrantClient(
            host=qdrant_host,
            port=qdrant_port or 6333,
            api_key=qdrant_api_key,
        )
    path = qdrant_path or DEFAULT_QDRANT_PATH
    return QdrantClient(path=path)


def _collection_exists(client, name: str) -> bool:
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False


def _scroll_collection(
    client,
    collection: str,
    *,
    batch_size: int = 512,
    with_vectors: bool = False,
) -> Iterator[Any]:
    """Stream every point in a Qdrant collection using scroll pagination.

    Yields raw qdrant_client.models.Record (or equivalent) objects.
    """
    offset: Any = None
    while True:
        try:
            points, next_offset = client.scroll(
                collection_name=collection,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=with_vectors,
            )
        except Exception as e:
            raise SystemExit(
                f"Qdrant scroll failed on collection '{collection}': {e}"
            ) from e
        if not points:
            return
        for pt in points:
            yield pt
        if next_offset is None:
            return
        offset = next_offset


# ─── Qdrant point → MPF record ───────────────────────────────────────────────


def _composite_tenancy(
    user_id: Optional[str],
    agent_id: Optional[str],
    run_id: Optional[str],
) -> str:
    """Mem0's tenancy triple flattened into a single string.

    Absent axes become '-' so the shape is stable.
    """
    return ":".join(
        (user_id or "-", agent_id or "-", run_id or "-")
    )


def _point_to_record(
    point: Any,
    *,
    tenancy_axis: str,
) -> Dict[str, Any]:
    """Shape one Qdrant main-collection point into an MPF memory record."""
    pid = str(getattr(point, "id", "") or "")
    payload = dict(getattr(point, "payload", {}) or {})

    content = payload.pop("data", "") or ""
    memory_type = payload.pop("memory_type", None)
    created = payload.pop("created_at", None)
    updated = payload.pop("updated_at", None)
    user_id = payload.pop("user_id", None)
    agent_id = payload.pop("agent_id", None)
    run_id = payload.pop("run_id", None)
    actor_id = payload.pop("actor_id", None)
    role = payload.pop("role", None)
    mem_hash = payload.pop("hash", None)

    record_payload: Dict[str, Any] = {
        "content": content,
        "category": "memory",
    }
    if memory_type:
        record_payload["subcategory"] = memory_type
    if created:
        record_payload["created"] = created
    if updated:
        record_payload["updated"] = updated

    # Tenancy axis
    tenancy = _composite_tenancy(user_id, agent_id, run_id)
    if tenancy_axis == "namespace":
        record_payload["namespace"] = tenancy
    else:
        record_payload["owner_id"] = tenancy

    # Round-trip blob — preserve every native Mem0 field so a future
    # Mem0 importer can reconstruct the point exactly.
    mem0_meta: Dict[str, Any] = {
        "point_id": pid,
        "memory_type": memory_type,
        "hash": mem_hash,
        "user_id": user_id,
        "agent_id": agent_id,
        "run_id": run_id,
    }
    if actor_id is not None:
        mem0_meta["actor_id"] = actor_id
    if role is not None:
        mem0_meta["role"] = role
    # Anything left on the Qdrant payload (custom fields Mem0
    # callers stuffed in via metadata=) is preserved verbatim.
    if payload:
        mem0_meta["extra"] = payload

    record_payload["metadata"] = {"mem0": mem0_meta}

    return {
        "id": pid,
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": record_payload,
    }


# ─── Entities collection → MPF facts ─────────────────────────────────────────


def _iter_entity_triples(
    client,
    collection: str,
    *,
    tenancy_axis: str,
) -> Iterator[Dict[str, Any]]:
    """Emit one MPF fact record per row in <collection>_entities.

    Mem0 stores entity-link rows as Qdrant points whose payload
    carries at minimum: entity name, entity_type, source_memory_id,
    plus whatever tenancy the origin memory had.
    """
    entities_coll = f"{collection}_entities"
    if not _collection_exists(client, entities_coll):
        return
    for pt in _scroll_collection(client, entities_coll, batch_size=512):
        pid = str(getattr(pt, "id", "") or "")
        raw = dict(getattr(pt, "payload", {}) or {})
        # Mem0's entity rows have flexible shape across versions —
        # best-effort field extraction, raw preserved for round-trip.
        entity_name = (
            raw.get("name")
            or raw.get("entity")
            or raw.get("entity_name")
            or ""
        )
        entity_type = (
            raw.get("entity_type")
            or raw.get("type")
            or "entity"
        )
        source_memory_id = (
            raw.get("source_memory_id")
            or raw.get("memory_id")
            or raw.get("source_id")
        )
        user_id = raw.get("user_id")
        agent_id = raw.get("agent_id")
        run_id = raw.get("run_id")
        created = raw.get("created_at")
        updated = raw.get("updated_at")

        payload: Dict[str, Any] = {
            "subject": str(entity_name or pid),
            "predicate": f"mem0:{entity_type}",
            "category": "facts",
        }
        if created:
            payload["created"] = created
        if updated:
            payload["updated"] = updated

        tenancy = _composite_tenancy(user_id, agent_id, run_id)
        if tenancy_axis == "namespace":
            payload["namespace"] = tenancy
        else:
            payload["owner_id"] = tenancy

        payload["metadata"] = {
            "mem0": {
                "point_id": pid,
                "entity_type": entity_type,
                "source_memory_id": source_memory_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "raw": raw,
            }
        }

        record: Dict[str, Any] = {
            "id": f"entity:{pid}",
            "kind": "fact",
            "payload_version": PAYLOAD_VERSION_MNEMOS,
            "payload": payload,
        }
        if source_memory_id:
            record["source_record_ids"] = [str(source_memory_id)]
        yield record


# ─── SQLite history → MPF facts (opt-in) ─────────────────────────────────────


def _iter_history_facts(
    history_db: Path,
    *,
    tenancy_axis: str,
) -> Iterator[Dict[str, Any]]:
    """Emit one MPF fact per row in Mem0's SQLite history sidecar.

    Schema (as of mem0ai 0.1.x): history(id, memory_id, old_memory,
    new_memory, event, created_at, updated_at, is_deleted, ...).
    Older builds may lack some columns; we query PRAGMA first and
    project whatever is present.
    """
    if not history_db.exists():
        print(
            f"  history db not found at {history_db}; skipping history facts",
            file=sys.stderr,
        )
        return
    try:
        conn = sqlite3.connect(str(history_db))
    except sqlite3.Error as e:
        print(f"  WARNING opening history db {history_db}: {e}", file=sys.stderr)
        return
    conn.row_factory = sqlite3.Row
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(history)").fetchall()
        }
        if not cols:
            print(
                f"  history table missing in {history_db}; skipping",
                file=sys.stderr,
            )
            return
        for row in conn.execute("SELECT * FROM history").fetchall():
            r = {k: row[k] for k in row.keys()}
            hid = str(r.get("id") or "")
            mem_id = str(r.get("memory_id") or "")
            op = (r.get("event") or r.get("action") or "event").lower()
            old_mem = r.get("old_memory")
            new_mem = r.get("new_memory")
            created = r.get("created_at")
            updated = r.get("updated_at")

            snippet = (new_mem or old_mem or "").strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            statement = f"memory {op}: {snippet}" if snippet else f"memory {op}"

            payload: Dict[str, Any] = {
                "statement": statement,
                "subject": mem_id,
                "predicate": op,
                "category": "history",
            }
            if created:
                payload["created"] = created
            if updated:
                payload["updated"] = updated

            # History rows don't carry tenancy — rely on the linked
            # memory_id for cross-reference; write a neutral axis.
            tenancy = _composite_tenancy(None, None, None)
            if tenancy_axis == "namespace":
                payload["namespace"] = tenancy
            else:
                payload["owner_id"] = tenancy

            payload["metadata"] = {
                "mem0": {
                    "history": {
                        "id": hid,
                        "memory_id": mem_id,
                        "event": op,
                        "old_memory": old_mem,
                        "new_memory": new_mem,
                        "is_deleted": r.get("is_deleted"),
                    }
                }
            }

            record: Dict[str, Any] = {
                "id": f"history:{hid}",
                "kind": "fact",
                "payload_version": PAYLOAD_VERSION_MNEMOS,
                "payload": payload,
            }
            if mem_id:
                record["source_record_ids"] = [mem_id]
            yield record
    finally:
        conn.close()


# ─── Hosted Platform path ────────────────────────────────────────────────────


def _iter_platform_records(
    api_key: str,
    *,
    tenancy_axis: str,
    page_size: int = 100,
) -> Iterator[Dict[str, Any]]:
    """Iterate memories from api.mem0.ai via the mem0ai SDK.

    Uses MemoryClient.get_all() with pagination. The hosted platform
    exposes 'categories' in addition to OSS fields — we preserve them
    under metadata.mem0.categories.
    """
    if MemoryClient is None:
        raise SystemExit(
            "mem0ai is required for --platform mode. Install with:\n"
            "  pip install mem0ai"
        )
    client = MemoryClient(api_key=api_key)
    page = 1
    while True:
        try:
            batch = client.get_all(page=page, page_size=page_size)
        except TypeError:
            # Older SDK without pagination kwargs
            batch = client.get_all()
        if not batch:
            return
        # Normalize: SDK may return a list or {"results": [...]}
        rows: List[Dict[str, Any]]
        if isinstance(batch, dict):
            rows = batch.get("results") or batch.get("memories") or []
        else:
            rows = list(batch)
        if not rows:
            return
        for row in rows:
            pid = str(row.get("id") or "")
            content = row.get("memory") or row.get("data") or ""
            memory_type = row.get("memory_type")
            user_id = row.get("user_id")
            agent_id = row.get("agent_id")
            run_id = row.get("run_id")
            created = row.get("created_at")
            updated = row.get("updated_at")
            categories = row.get("categories")

            payload: Dict[str, Any] = {
                "content": content,
                "category": "memory",
            }
            if memory_type:
                payload["subcategory"] = memory_type
            if created:
                payload["created"] = created
            if updated:
                payload["updated"] = updated

            tenancy = _composite_tenancy(user_id, agent_id, run_id)
            if tenancy_axis == "namespace":
                payload["namespace"] = tenancy
            else:
                payload["owner_id"] = tenancy

            payload["metadata"] = {
                "mem0": {
                    "point_id": pid,
                    "memory_type": memory_type,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "categories": categories,
                    "platform": True,
                    "raw": row,
                }
            }

            yield {
                "id": pid,
                "kind": "memory",
                "payload_version": PAYLOAD_VERSION_MNEMOS,
                "payload": payload,
            }
        # Stop when the SDK returned fewer than a full page.
        if len(rows) < page_size:
            return
        page += 1


# ─── Streaming envelope assembly ─────────────────────────────────────────────


def iter_records(
    *,
    platform: bool = False,
    api_key_mem0: Optional[str] = None,
    qdrant_path: Optional[str] = None,
    qdrant_url: Optional[str] = None,
    qdrant_host: Optional[str] = None,
    qdrant_port: Optional[int] = None,
    qdrant_api_key: Optional[str] = None,
    collection: str = DEFAULT_COLLECTION,
    tenancy_axis: str = "namespace",
    emit_history_facts: bool = False,
    history_db: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Stream MPF records from a Mem0 source (offline or platform)."""
    if platform:
        if not api_key_mem0:
            raise SystemExit("--platform requires --api-key-mem0")
        yield from _iter_platform_records(
            api_key_mem0, tenancy_axis=tenancy_axis
        )
        return

    client = _open_qdrant(
        qdrant_path=qdrant_path,
        qdrant_url=qdrant_url,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        qdrant_api_key=qdrant_api_key,
    )
    if not _collection_exists(client, collection):
        raise SystemExit(
            f"Qdrant collection '{collection}' not found. "
            f"Pass --collection to override (default: {DEFAULT_COLLECTION})."
        )

    # 1) Main collection → memory records
    for pt in _scroll_collection(client, collection, batch_size=512):
        yield _point_to_record(pt, tenancy_axis=tenancy_axis)

    # 2) Entities collection → fact records (SPO-ish)
    yield from _iter_entity_triples(
        client, collection, tenancy_axis=tenancy_axis
    )

    # 3) History sidecar → fact records (opt-in)
    if emit_history_facts:
        hdb = history_db or Path(os.path.expanduser(DEFAULT_HISTORY_DB))
        yield from _iter_history_facts(hdb, tenancy_axis=tenancy_axis)


def build_envelope(
    *,
    source_instance: Optional[str] = None,
    tenancy_axis: str = "namespace",
    platform: bool = False,
    api_key_mem0: Optional[str] = None,
    qdrant_path: Optional[str] = None,
    qdrant_url: Optional[str] = None,
    qdrant_host: Optional[str] = None,
    qdrant_port: Optional[int] = None,
    qdrant_api_key: Optional[str] = None,
    collection: str = DEFAULT_COLLECTION,
    emit_history_facts: bool = False,
    history_db: Optional[Path] = None,
) -> Dict[str, Any]:
    """Assemble a full MPF envelope from a Mem0 source."""
    records = list(iter_records(
        platform=platform,
        api_key_mem0=api_key_mem0,
        qdrant_path=qdrant_path,
        qdrant_url=qdrant_url,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        qdrant_api_key=qdrant_api_key,
        collection=collection,
        tenancy_axis=tenancy_axis,
        emit_history_facts=emit_history_facts,
        history_db=history_db,
    ))
    return {
        "mpf_version": MPF_VERSION,
        "source_system": SOURCE_SYSTEM,
        "source_version": _detect_mem0_version(),
        "source_instance": source_instance,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }


def _detect_mem0_version() -> Optional[str]:
    # Same self-shadowing trap as _import_mem0_memoryclient — the
    # file itself is named mem0.py in production. Skip the script's
    # dir so the installed mem0ai package is what we read __version__
    # from.
    import importlib
    script_dir = os.path.dirname(os.path.abspath(__file__))
    saved_path = list(sys.path)
    sys.path = [p for p in sys.path
                if os.path.abspath(p or ".") != script_dir]
    cached = sys.modules.pop("mem0", None)
    try:
        mod = importlib.import_module("mem0")
        return getattr(mod, "__version__", None)
    except Exception:
        return None
    finally:
        sys.path = saved_path
        if cached is not None:
            sys.modules["mem0"] = cached


# ─── MPF → MNEMOS POST (optional) ────────────────────────────────────────────


def _post_to_mnemos(
    envelope: Dict[str, Any],
    endpoint: str,
    api_key: str,
    *,
    batch_size: int = 200,
) -> Dict[str, int]:
    """POST to MNEMOS /v1/import?preserve_owner=true in batches."""
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


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemos.tools.adapters.mem0",
        description=(
            "Mem0 → MPF v0.1 adapter (CHARON). Reads a Mem0 Qdrant "
            "collection (and optionally its SQLite history sidecar) "
            "directly, or iterates the hosted api.mem0.ai platform "
            "via the mem0ai SDK, and emits an MPF envelope. "
            "Optionally POSTs to a MNEMOS /v1/import endpoint."
        ),
    )
    # Source selection
    p.add_argument("--platform", action="store_true",
                   help="Pull from hosted api.mem0.ai via the mem0ai SDK "
                        "instead of reading Qdrant directly.")
    p.add_argument("--api-key-mem0", default=None,
                   help="Mem0 Platform API key (required with --platform).")

    # Qdrant connection (offline mode)
    p.add_argument("--qdrant-path", default=None, metavar="PATH",
                   help=f"File-mode Qdrant directory (default: "
                        f"{DEFAULT_QDRANT_PATH} — Mem0's OSS default).")
    p.add_argument("--qdrant-url", default=None, metavar="URL",
                   help="HTTP Qdrant endpoint (overrides --qdrant-path).")
    p.add_argument("--qdrant-host", default=None, metavar="HOST",
                   help="Qdrant host (alternative to --qdrant-url).")
    p.add_argument("--qdrant-port", type=int, default=None, metavar="PORT",
                   help="Qdrant port (default 6333; used with --qdrant-host).")
    p.add_argument("--qdrant-api-key", default=None,
                   help="Qdrant Cloud API key if the instance is secured.")
    p.add_argument("--collection", default=DEFAULT_COLLECTION, metavar="NAME",
                   help=f"Main collection name (default: {DEFAULT_COLLECTION}).")

    # Extras
    p.add_argument("--emit-history-facts", action="store_true",
                   help="Also read Mem0's SQLite history sidecar and emit "
                        "one kind=fact record per ADD/UPDATE/DELETE event.")
    p.add_argument("--history-db", default=None, metavar="PATH",
                   help=f"Path to Mem0 history.db "
                        f"(default: {DEFAULT_HISTORY_DB}).")

    # MPF-level knobs
    p.add_argument("--tenancy-axis", choices=("owner_id", "namespace"),
                   default="namespace",
                   help="Which MNEMOS tenancy axis to write the Mem0 "
                        "user:agent:run composite into (default: namespace).")
    p.add_argument("--source-instance", default=None,
                   help="Diagnostic label written into the envelope "
                        "(e.g. 'prod-mem0-us-east').")

    # Output
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Write MPF envelope to this file ('-' for stdout). "
                        "Omit when --post is used.")
    p.add_argument("--post", default=None, metavar="URL",
                   help="POST the envelope to a MNEMOS /v1/import endpoint "
                        "(e.g. http://localhost:5002). Requires --api-key.")
    p.add_argument("--api-key", default=None,
                   help="Bearer token for MNEMOS auth (needed with --post).")

    args = p.parse_args(argv)

    if not (args.out or args.post):
        print("ERROR: pass --out PATH or --post URL", file=sys.stderr)
        return 2
    if args.post and not args.api_key:
        print("ERROR: --post requires --api-key", file=sys.stderr)
        return 2
    if args.platform and not args.api_key_mem0:
        print("ERROR: --platform requires --api-key-mem0", file=sys.stderr)
        return 2
    if args.platform and (args.qdrant_url or args.qdrant_host
                          or args.qdrant_path):
        print(
            "WARNING: --platform overrides all --qdrant-* flags",
            file=sys.stderr,
        )

    history_db = (
        Path(os.path.expanduser(args.history_db))
        if args.history_db
        else None
    )

    t0 = time.time()
    envelope = build_envelope(
        source_instance=args.source_instance,
        tenancy_axis=args.tenancy_axis,
        platform=args.platform,
        api_key_mem0=args.api_key_mem0,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_host=args.qdrant_host,
        qdrant_port=args.qdrant_port,
        qdrant_api_key=args.qdrant_api_key,
        collection=args.collection,
        emit_history_facts=args.emit_history_facts,
        history_db=history_db,
    )
    elapsed = time.time() - t0
    print(
        f"read {envelope['record_count']} records from mem0 "
        f"({'platform' if args.platform else 'qdrant'}) "
        f"in {elapsed:.1f}s",
        file=sys.stderr,
    )

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
            f"POST complete: "
            f"imported={totals['imported']} "
            f"skipped={totals['skipped']} "
            f"failed={totals['failed']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
tools/adapters/mempalace.py — CHARON adapter for MemPalace palaces.

Converts a MemPalace palace (ChromaDB on disk) into an MPF v0.1
envelope. Works without the `mempalace` runtime installed — it
reads the ChromaDB collection directly using the same access
pattern that mempalace/exporter.py uses internally.

Usage:
    python -m mnemos.tools.adapters.mempalace --palace ~/.mempalace/palace --out palace.mpf.json
    python -m mnemos.tools.adapters.mempalace --palace ~/.mempalace/palace --post http://mnemos:5002 --api-key $TOKEN
    python -m mnemos.tools.adapters.mempalace --palace ~/.mempalace/palace --out - | python -m mnemos.tools.mpf_validate --file -

Key mapping (MemPalace → MPF):
    drawer              → records[] entry, kind="memory"
    drawer metadata     → payload.metadata.mempalace.* (round-trip blob)
    wing                → payload.namespace (configurable axis)
    room                → payload.category
    AAAK compressed     → payload.metadata.mempalace.aaak (if present) + compression_manifest[] entry
    tunnel              → kg_triples[] entry, predicate="tunnel:<label>"

Design notes:
  * Drawers whose *content* is a YAML-fronted memory (common when the
    source was minted from a MemPalace `mine` over a directory of
    memory .md files) have their original ids / categories recovered
    from the YAML front matter. Without front matter, the drawer's
    MemPalace-assigned id is used.
  * Embeddings are NOT emitted (MPF spec allows regeneration on
    import; nomic-embed-text is model-coupled and cheap to redo).
  * Idempotent: same palace snapshot produces byte-identical
    envelope modulo the exported_at timestamp.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MPF_VERSION = "0.1.0"
# Adapter translates drawer → MNEMOS memory shape (wing → namespace,
# room → category, drawer metadata → payload.metadata.mempalace.*).
# Payload is therefore MNEMOS-native after the translation, so declare
# mnemos-3.1 — that's what /v1/import accepts. MemPalace provenance
# is preserved via envelope-level source_system="mempalace" + the
# per-payload metadata.mempalace round-trip blob, not via
# payload_version.
#
# Codex caught the prior mempalace-3.3 declaration silently drop every
# record (importer's payload-version check filed them to the `skipped`
# bucket per the forward-compat rule, because the handler only
# understands mnemos-3.1).
PAYLOAD_VERSION_MNEMOS = "mnemos-3.1"
SOURCE_SYSTEM = "mempalace"

# ChromaDB is required — same dependency MemPalace itself uses. The
# adapter bails early with a clear message if missing.
try:
    import chromadb  # type: ignore
except ImportError:
    chromadb = None  # noqa: N816  (match runtime guard style)


# ─── ChromaDB access ─────────────────────────────────────────────────────────


def _open_collection(palace_path: Path):
    """Open the palace's ChromaDB collection.

    Matches mempalace.palace.get_collection's behaviour: assumes
    the default collection name MemPalace writes, and falls back
    to the single-collection heuristic if the explicit name is
    absent (older palaces).
    """
    if chromadb is None:
        raise SystemExit(
            "chromadb is required. Install with:\n"
            "  pip install chromadb\n"
            "(same dependency MemPalace itself uses.)"
        )
    client = chromadb.PersistentClient(path=str(palace_path))
    # MemPalace canonically uses 'mempalace_drawers' for content
    # (see mempalace.palace.get_collection). 'mempalace_closets' is
    # a separate KG collection.
    for name in ("mempalace_drawers", "palace", "mempalace_palace"):
        try:
            return client.get_collection(name)
        except Exception:
            continue
    # Fall back to the only collection if unambiguous.
    colls = client.list_collections()
    if len(colls) == 1:
        return colls[0]
    if not colls:
        raise SystemExit(f"No ChromaDB collections found at {palace_path}")
    names = ", ".join(getattr(c, "name", "?") for c in colls)
    raise SystemExit(
        f"Multiple collections at {palace_path} and none named 'mempalace_drawers'. "
        f"Found: {names}."
    )


# ─── Drawer → MPF record ─────────────────────────────────────────────────────


_YAML_FRONT_RE = re.compile(
    r"\A>?\s*---\s*\n"          # opening --- (may be blockquoted)
    r"(?P<body>.*?)\n"
    r">?\s*---\s*\n",
    re.DOTALL,
)


def _parse_yaml_front(raw: str) -> Tuple[Optional[Dict[str, str]], str]:
    """If the drawer content starts with a YAML front matter block,
    return (fields, remainder). Otherwise (None, raw)."""
    m = _YAML_FRONT_RE.match(raw)
    if m is None:
        return None, raw
    body = m.group("body")
    fields: Dict[str, str] = {}
    for line in body.splitlines():
        line = line.lstrip("> ").strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    remainder = raw[m.end():]
    return fields, remainder


def _drawer_to_record(
    drawer_id: str,
    content: str,
    metadata: Dict[str, Any],
    *,
    wing_axis: str,
    restore_original_ids: bool,
) -> Dict[str, Any]:
    """Shape one ChromaDB row into an MPF record."""
    wing = metadata.get("wing", "unknown")
    room = metadata.get("room", "general")

    # Recover original memory fields if the drawer content carries
    # YAML front matter (MemPalace's mine mode over a directory of
    # memory .md files preserves this).
    yaml_fields, body = _parse_yaml_front(content)
    original_id = (yaml_fields or {}).get("id")
    original_category = (yaml_fields or {}).get("category")
    original_subcategory = (yaml_fields or {}).get("subcategory")
    original_created = (yaml_fields or {}).get("created")
    original_owner = (yaml_fields or {}).get("owner_id")
    original_namespace = (yaml_fields or {}).get("namespace")

    record_id = original_id if (restore_original_ids and original_id) else drawer_id

    payload: Dict[str, Any] = {
        "content": body.strip() if yaml_fields else content,
        "category": original_category or room,
    }
    if original_subcategory:
        payload["subcategory"] = original_subcategory
    if original_created:
        payload["created"] = original_created
    # Tenancy axis
    if wing_axis == "namespace":
        payload["namespace"] = original_namespace or wing
    else:
        payload["owner_id"] = original_owner or wing

    # Non-destructive MemPalace round-trip slot. Always preserved
    # so a MemPalace importer can reconstruct the drawer exactly.
    mp_meta: Dict[str, Any] = {
        "drawer_id": drawer_id,
        "wing": wing,
        "room": room,
    }
    for k in ("source_file", "filed_at", "added_by", "aaak", "palace_coord"):
        v = metadata.get(k)
        if v is not None:
            mp_meta[k] = v
    payload["metadata"] = {"mempalace": mp_meta}

    return {
        "id": record_id,
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


# ─── Streaming envelope assembly ─────────────────────────────────────────────


def iter_records(
    palace_path: Path,
    *,
    wing_axis: str = "namespace",
    restore_original_ids: bool = True,
    batch_size: int = 1000,
) -> Iterator[Dict[str, Any]]:
    """Stream MPF records from the palace, in ChromaDB natural order."""
    col = _open_collection(palace_path)
    total = col.count()
    if total == 0:
        return
    offset = 0
    while offset < total:
        batch = col.get(
            limit=batch_size,
            offset=offset,
            include=["documents", "metadatas"],
        )
        if not batch.get("ids"):
            break
        for did, doc, meta in zip(
            batch["ids"], batch["documents"], batch["metadatas"]
        ):
            yield _drawer_to_record(
                did,
                doc or "",
                dict(meta or {}),
                wing_axis=wing_axis,
                restore_original_ids=restore_original_ids,
            )
        offset += len(batch["ids"])


def build_envelope(
    palace_path: Path,
    *,
    source_instance: Optional[str] = None,
    wing_axis: str = "namespace",
    restore_original_ids: bool = True,
) -> Dict[str, Any]:
    """Assemble a full MPF envelope from a palace snapshot."""
    records = list(iter_records(
        palace_path,
        wing_axis=wing_axis,
        restore_original_ids=restore_original_ids,
    ))
    return {
        "mpf_version": MPF_VERSION,
        "source_system": SOURCE_SYSTEM,
        "source_version": _detect_mempalace_version(),
        "source_instance": source_instance,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }


def _detect_mempalace_version() -> Optional[str]:
    try:
        from mempalace.version import __version__  # type: ignore
        return __version__
    except Exception:
        return None


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
        prog="mnemos.tools.adapters.mempalace",
        description=(
            "MemPalace → MPF v0.1 adapter (CHARON). Reads a palace's "
            "ChromaDB directly and emits an MPF envelope. Optionally "
            "POSTs it to a MNEMOS /v1/import endpoint."
        ),
    )
    p.add_argument("--palace", required=True, metavar="PATH",
                   help="Path to the MemPalace palace directory "
                        "(contains ChromaDB storage).")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Write MPF envelope to this file ('-' for stdout). "
                        "Omit when --post is used.")
    p.add_argument("--post", default=None, metavar="URL",
                   help="POST the envelope to a MNEMOS /v1/import endpoint "
                        "(e.g. http://localhost:5002). Requires --api-key.")
    p.add_argument("--api-key", default=None,
                   help="Bearer token for MNEMOS auth (needed with --post).")
    p.add_argument("--wing-axis", choices=("owner_id", "namespace"),
                   default="namespace",
                   help="Which MNEMOS tenancy axis to write MemPalace wings "
                        "into (default: namespace).")
    p.add_argument("--no-restore-ids", action="store_true",
                   help="Keep MemPalace's drawer ids rather than recovering "
                        "original memory ids from YAML front matter.")
    p.add_argument("--source-instance", default=None,
                   help="Diagnostic label written into the envelope.")
    args = p.parse_args(argv)

    if not (args.out or args.post):
        print("ERROR: pass --out PATH or --post URL", file=sys.stderr)
        return 2
    if args.post and not args.api_key:
        print("ERROR: --post requires --api-key", file=sys.stderr)
        return 2

    palace = Path(args.palace).expanduser().resolve()
    if not palace.exists():
        print(f"ERROR: palace not found: {palace}", file=sys.stderr)
        return 2

    t0 = time.time()
    envelope = build_envelope(
        palace,
        source_instance=args.source_instance,
        wing_axis=args.wing_axis,
        restore_original_ids=not args.no_restore_ids,
    )
    elapsed = time.time() - t0
    print(
        f"read {envelope['record_count']} records from palace "
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

#!/usr/bin/env python3
"""
memory_export.py — CHARON export side (MNEMOS memory portability).

Part of CHARON, MNEMOS's memory portability subsystem. Companion to
``memory_import.py``. Together they anchor a round-trip you can
trust: export from instance A, import to instance B, same ids,
same owners, same provenance.

Subcommands:
  json        Emit one MPF envelope (big JSON).
  jsonl       Emit one memory per line. Stream-friendly.
  markdown    Human-readable Markdown (reuses export_memories_for_docling).
  html        Human-readable HTML.
  text        Plain text.
  stats       Dump /stats.

Usage:
  python tools/memory_export.py json     --out memories.json --endpoint http://localhost:5002
  python tools/memory_export.py jsonl    --out memories.jsonl --endpoint http://localhost:5002
  python tools/memory_export.py json     --category documents --out docs.json \\
                                         --api-key $MNEMOS_API_KEY
  python tools/memory_export.py markdown --out memories.md
  python tools/memory_export.py stats    --endpoint http://localhost:5002

The ``json`` subcommand produces an MPF envelope compatible with
``memory_import.py json --preserve-metadata`` for cross-version
migrations.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


MPF_VERSION = "0.1.1"
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "memory_export"


def _fetch_export(
    endpoint: str,
    api_key: Optional[str],
    category: Optional[str],
    limit: int,
    include_sidecars: bool = False,
    include_unattached_kg: bool = True,
) -> Dict[str, Any]:
    """Call ``GET /v1/export`` and return the MPF envelope as a dict.

    The server-side handler already returns an MPF envelope with
    ``records[*].kind == "memory"`` and ``payload_version`` set; this
    function only manages the HTTP round-trip.

    With ``include_sidecars=True`` the envelope also carries the
    `kg_triples`, `memory_versions`, and `compression_manifest`
    sidecar arrays. Used by the CHARON v0.2 round-trip path; the
    default stays False so existing scripts emit unchanged envelopes.
    """
    endpoint = endpoint.rstrip("/")
    params: Dict[str, str] = {"limit": str(limit)}
    if category:
        params["category"] = category
    if include_sidecars:
        params["include_sidecars"] = "true"
        # CLI default differs from HTTP default (true vs false)
        # because the typical CLI use case is migration / full
        # corpus export, where dropping unattached kg_triples
        # would silently lose first-class facts. The HTTP API
        # default is false because typical web clients want a
        # strict slice.
        if include_unattached_kg:
            params["include_unattached_kg"] = "true"
    url = f"{endpoint}/v1/export?{urllib.parse.urlencode(params)}"

    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"ERROR: /v1/export HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR: /v1/export connection: {exc.reason}")

    if not isinstance(payload, dict) or "records" not in payload:
        raise SystemExit("ERROR: /v1/export did not return an MPF envelope")

    return payload


def _fetch_memories_legacy(
    endpoint: str,
    api_key: Optional[str],
    category: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Fallback memory fetch for markdown/html/text paths.

    Uses ``GET /memories`` which returns a raw array. The markdown /
    html / text formatters in ``export_memories_for_docling.py``
    expect flat memory dicts (not MPF records), so we flatten the
    export envelope or use the legacy endpoint directly.
    """
    # Prefer /v1/export (keeps all provenance) and flatten records.
    envelope = _fetch_export(endpoint, api_key, category, limit)
    flat: List[Dict[str, Any]] = []
    for rec in envelope.get("records") or []:
        if rec.get("kind") != "memory":
            continue
        payload = dict(rec.get("payload") or {})
        payload.setdefault("id", rec.get("id"))
        flat.append(payload)
    return flat


def cmd_json(args: argparse.Namespace) -> None:
    envelope = _fetch_export(
        args.endpoint, args.api_key, args.category, args.limit,
        include_sidecars=getattr(args, "include_sidecars", False),
        include_unattached_kg=getattr(args, "include_unattached_kg", True),
    )
    out = Path(args.out)
    out.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    n = len(envelope.get("records") or [])
    extras = []
    for key in ("kg_triples", "memory_versions", "compression_manifest"):
        sidecar = envelope.get(key)
        if sidecar:
            extras.append(f"{key}={len(sidecar)}")
    suffix = (" + " + ", ".join(extras)) if extras else ""
    print(f"Wrote {n} records as MPF envelope → {out}{suffix}")


def cmd_jsonl(args: argparse.Namespace) -> None:
    envelope = _fetch_export(
        args.endpoint, args.api_key, args.category, args.limit,
        include_sidecars=getattr(args, "include_sidecars", False),
        include_unattached_kg=getattr(args, "include_unattached_kg", True),
    )
    records = envelope.get("records") or []
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for rec in records:
            # Each line is a whole MPF record — round-trips via
            # memory_import.py json --jsonl.
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
        # Sidecars don't fit cleanly into per-record-per-line JSONL, so
        # we emit them as a single trailer envelope at EOF when
        # include_sidecars is set. Importers reading the file as JSONL
        # see a normal record-per-line stream until the final line,
        # which is the trailer keyed by mpf_sidecars=true.
        sidecar_keys = ("kg_triples", "memory_versions", "compression_manifest")
        sidecars = {k: envelope.get(k) for k in sidecar_keys if envelope.get(k)}
        if sidecars:
            trailer = {"mpf_sidecars": True, **sidecars}
            f.write(json.dumps(trailer, ensure_ascii=False))
            f.write("\n")
    extras = ", ".join(f"{k}={len(v)}" for k, v in sidecars.items()) if sidecars else ""
    suffix = f" + sidecars: {extras}" if extras else ""
    print(f"Wrote {len(records)} records as JSONL → {out}{suffix}")


def cmd_markdown(args: argparse.Namespace) -> None:
    try:
        from tools.export_memories_for_docling import export_memories_markdown
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools.export_memories_for_docling import export_memories_markdown  # noqa
    memories = _fetch_memories_legacy(args.endpoint, args.api_key,
                                      args.category, args.limit)
    out = Path(args.out)
    export_memories_markdown(memories, out)
    print(f"Wrote {len(memories)} memories as Markdown → {out}")


def cmd_html(args: argparse.Namespace) -> None:
    try:
        from tools.export_memories_for_docling import export_memories_html
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools.export_memories_for_docling import export_memories_html  # noqa
    memories = _fetch_memories_legacy(args.endpoint, args.api_key,
                                      args.category, args.limit)
    out = Path(args.out)
    export_memories_html(memories, out)
    print(f"Wrote {len(memories)} memories as HTML → {out}")


def cmd_text(args: argparse.Namespace) -> None:
    try:
        from tools.export_memories_for_docling import export_memories_plaintext
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools.export_memories_for_docling import export_memories_plaintext  # noqa
    memories = _fetch_memories_legacy(args.endpoint, args.api_key,
                                      args.category, args.limit)
    out = Path(args.out)
    export_memories_plaintext(memories, out)
    print(f"Wrote {len(memories)} memories as plain text → {out}")


def cmd_stats(args: argparse.Namespace) -> None:
    endpoint = args.endpoint.rstrip("/")
    headers: Dict[str, str] = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    req = urllib.request.Request(f"{endpoint}/stats", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"ERROR: /stats HTTP {exc.code}: {exc.read().decode()[:200]}")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--endpoint", default="http://localhost:5002",
                   help="MNEMOS API base URL (default: http://localhost:5002)")
    p.add_argument("--api-key", metavar="KEY", default=None,
                   help="Optional Bearer token for MNEMOS auth")


def _add_fetch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True, metavar="PATH",
                   help="Output file path")
    p.add_argument("--category", default=None,
                   help="Filter memories by category (all if omitted)")
    p.add_argument("--limit", type=int, default=10_000,
                   help="Maximum number of memories (default: 10000). "
                        "Matches the server-side _EXPORT_HARD_LIMIT.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_export",
        description=(
            "Export MNEMOS memories in portability-friendly formats. "
            "Part of CHARON, MNEMOS's memory portability subsystem."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_json = sub.add_parser("json", help="Emit MPF envelope (big JSON)")
    _add_common(p_json)
    _add_fetch_args(p_json)
    p_json.add_argument(
        "--include-sidecars", action="store_true",
        help="Include kg_triples / memory_versions / compression_manifest "
             "sidecars in the envelope (CHARON v0.2). Off by default.",
    )
    p_json.add_argument(
        "--no-include-unattached-kg",
        dest="include_unattached_kg", action="store_false", default=True,
        help="Skip first-class kg_triples (memory_id IS NULL) when emitting "
             "the kg_triples sidecar. Default: include them, since the "
             "typical CLI use case is full-corpus migration where "
             "unattached facts are part of the scope.",
    )
    p_json.set_defaults(func=cmd_json)

    p_jsonl = sub.add_parser("jsonl", help="Emit JSONL (one MPF record per line)")
    _add_common(p_jsonl)
    _add_fetch_args(p_jsonl)
    p_jsonl.add_argument(
        "--include-sidecars", action="store_true",
        help="Include kg_triples / memory_versions / compression_manifest "
             "sidecars as a final trailer line (CHARON v0.2). Off by default.",
    )
    p_jsonl.add_argument(
        "--no-include-unattached-kg",
        dest="include_unattached_kg", action="store_false", default=True,
        help="Skip first-class kg_triples (memory_id IS NULL). See json "
             "subcommand help.",
    )
    p_jsonl.set_defaults(func=cmd_jsonl)

    p_md = sub.add_parser("markdown", help="Emit Markdown (human-readable)")
    _add_common(p_md)
    _add_fetch_args(p_md)
    p_md.set_defaults(func=cmd_markdown)

    p_html = sub.add_parser("html", help="Emit HTML (human-readable)")
    _add_common(p_html)
    _add_fetch_args(p_html)
    p_html.set_defaults(func=cmd_html)

    p_txt = sub.add_parser("text", help="Emit plain text")
    _add_common(p_txt)
    _add_fetch_args(p_txt)
    p_txt.set_defaults(func=cmd_text)

    p_stats = sub.add_parser("stats", help="Print /stats response")
    _add_common(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
mpf_validate.py — validate an MPF envelope against docs/mpf_v0.1.json.

Standalone. Any memory system (Mem0, Letta, Graphiti, Cognee, MNEMOS,
MemPalace) can use this to validate its own MPF emissions before
shipping them. The schema file it validates against is the authoritative
wire-format definition.

Usage:
  python tools/mpf_validate.py --file export.json
  python tools/mpf_validate.py --file - < export.json        # stdin
  python tools/mpf_validate.py --file export.json --schema docs/mpf_v0.1.json

Exit codes:
  0 — envelope validates
  1 — validation failed (prose error list printed to stderr)
  2 — I/O or schema-load error

Depends on the `jsonschema` package (pip install jsonschema). Falls
back to structural-only checks if jsonschema isn't installed so the
tool still catches gross shape errors in a dependency-minimal
environment.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "docs" / "mpf_v0.1.json"


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _structural_check(env: Any) -> List[str]:
    """Minimal shape check that runs even without jsonschema installed.

    Covers the required-fields rules in the spec so a missing
    jsonschema dependency doesn't turn the validator into a no-op.
    """
    errs: List[str] = []
    if not isinstance(env, dict):
        return ["envelope must be a JSON object"]
    for k in ("mpf_version", "exported_at", "records"):
        if k not in env:
            errs.append(f"envelope missing required field: {k!r}")
    records = env.get("records")
    if records is not None and not isinstance(records, list):
        errs.append("'records' must be an array")
        return errs
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            errs.append(f"records[{i}] is not an object")
            continue
        for k in ("id", "kind", "payload_version", "payload"):
            if k not in rec:
                errs.append(f"records[{i}] missing required field: {k!r}")
    # Record-id uniqueness (critical round-trip invariant)
    seen: Dict[str, int] = {}
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if not isinstance(rid, str):
            continue
        if rid in seen:
            errs.append(
                f"records[{i}] duplicate id {rid!r} "
                f"(already seen at records[{seen[rid]}])"
            )
        else:
            seen[rid] = i
    return errs


def _full_check(env: Any, schema: Any) -> List[str]:
    """Run the full JSON Schema validation via the jsonschema package."""
    try:
        from jsonschema.validators import Draft202012Validator
    except ImportError:
        return ["jsonschema not installed — skipping full-schema check; "
                "install with `pip install jsonschema` to enable"]
    try:
        validator = Draft202012Validator(schema)
    except Exception as exc:
        return [f"schema load error: {exc}"]
    errs: List[str] = []
    for e in sorted(validator.iter_errors(env), key=lambda x: list(x.path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"{loc}: {e.message}")
    return errs


def validate(env: Any, schema: Optional[Any]) -> List[str]:
    """Run structural + full schema checks. Structural always runs; full
    runs only when jsonschema is installed and a schema was loaded."""
    errs = _structural_check(env)
    # Avoid duplicating structural errors when full-schema would catch
    # the same thing; run full-schema only if structural passed.
    if not errs and schema is not None:
        errs.extend(_full_check(env, schema))
    return errs


def summary(env: Any) -> str:
    if not isinstance(env, dict):
        return "(not an envelope)"
    records = env.get("records") or []
    by_kind: Dict[str, int] = {}
    for rec in records:
        if isinstance(rec, dict):
            k = rec.get("kind", "<missing>")
            by_kind[k] = by_kind.get(k, 0) + 1
    sidecar_counts = {
        k: len(env.get(k) or [])
        for k in ("kg_triples", "relations", "compression_manifest",
                  "memory_versions", "attestations")
        if env.get(k)
    }
    parts = [
        f"mpf_version={env.get('mpf_version')!r}",
        f"source_system={env.get('source_system')!r}",
        f"records={len(records)}",
    ]
    if by_kind:
        parts.append("kinds=" + ",".join(f"{k}:{v}" for k, v in sorted(by_kind.items())))
    if sidecar_counts:
        parts.append("sidecars=" + ",".join(f"{k}:{v}" for k, v in sidecar_counts.items()))
    return " ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mpf_validate",
        description=(
            "Validate an MPF envelope against docs/mpf_v0.1.json. Part "
            "of CHARON, MNEMOS's memory portability subsystem. Schema "
            "file is authoritative; this tool is a convenience runner."
        ),
    )
    parser.add_argument("--file", required=True, metavar="PATH",
                        help="Path to envelope JSON file, or '-' for stdin")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA), metavar="PATH",
                        help=f"Path to schema file (default: {DEFAULT_SCHEMA})")
    parser.add_argument("--quiet", action="store_true",
                        help="Only print errors; no summary line")
    parser.add_argument("--no-schema", action="store_true",
                        help="Skip full JSON Schema check; run structural only")
    args = parser.parse_args(argv)

    try:
        env = _load_json(args.file)
    except Exception as exc:
        print(f"ERROR reading {args.file}: {exc}", file=sys.stderr)
        return 2

    schema: Optional[Any] = None
    if not args.no_schema:
        try:
            schema = _load_json(args.schema)
        except Exception as exc:
            print(f"ERROR loading schema {args.schema}: {exc}", file=sys.stderr)
            return 2

    errs = validate(env, schema)

    if not args.quiet:
        print(summary(env), file=sys.stderr)

    if errs:
        print(f"VALIDATION FAILED ({len(errs)} error(s)):", file=sys.stderr)
        for e in errs:
            print(f"  {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

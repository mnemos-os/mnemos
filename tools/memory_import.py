#!/usr/bin/env python3
"""
memory_import.py — CHARON import side (MNEMOS memory portability).

Part of CHARON, MNEMOS's memory portability subsystem (ferrying
memories between instances and across version boundaries). The
companion is memory_export.py; together they anchor the round-trip
that makes migrations repeatable.

Generic memory importers for common formats into MNEMOS.

Subcommands:
  json      Import from MNEMOS JSON export or simplified array
  csv       Import from CSV with column mapping
  chatgpt   Import from OpenAI conversations.json export
  obsidian  Import from Obsidian vault (.md files with YAML frontmatter)
  text      Import plain text files (one per file or per paragraph)
  stats     Show current MNEMOS memory statistics

Usage:
  python tools/memory_import.py json     --file memories.json --endpoint http://localhost:5002
  python tools/memory_import.py json     --file memories.jsonl --jsonl --endpoint http://localhost:5002
  python tools/memory_import.py json     --file memories.jsonl --jsonl --preserve-metadata \
                                         --api-key $MNEMOS_API_KEY --endpoint http://localhost:5002
  python tools/memory_import.py csv      --file data.csv --content-col text --endpoint http://localhost:5002
  python tools/memory_import.py chatgpt  --file conversations.json --endpoint http://localhost:5002
  python tools/memory_import.py obsidian --vault /path/to/vault --endpoint http://localhost:5002
  python tools/memory_import.py text     --source /path --category notes --endpoint http://localhost:5002
  python tools/memory_import.py stats    --endpoint http://localhost:5002

Preserve-metadata mode (the cross-version-migration path):
  When --preserve-metadata is set, the importer posts an MPF envelope
  to /v1/import?preserve_owner=true instead of /memories POSTs. This
  keeps the original id, owner_id, namespace, subcategory, created,
  updated, quality_rating, and source_* provenance fields. Requires
  root-tier bearer token (the endpoint refuses preserve_owner=true
  for non-root callers). Batched to keep request bodies bounded.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _stable_id_from_mem(mem: dict) -> str:
    """Derive a deterministic, content-addressed id for a memory dict
    that came in without one. Hashing the canonical payload keeps
    re-imports of the same source idempotent — same input yields
    same id, so ON CONFLICT DO NOTHING does the right thing.

    Older versions used `f"imported_{id(mem):x}"` which is a Python
    process-local pointer address. Two runs against the same JSONL
    produced different ids and bypassed ON CONFLICT — Codex round-5
    finding. The hash is content-only (no timestamp, no source path),
    so it's stable across machines and Python versions, and identical
    content from any source dedupes naturally.
    """
    canonical = json.dumps(
        {k: mem.get(k) for k in sorted(mem.keys()) if mem.get(k) is not None},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"imported_{digest}"


# ---------------------------------------------------------------------------
# Base importer
# ---------------------------------------------------------------------------

class BaseImporter:
    """Shared HTTP posting logic for all importers."""

    # MPF envelope constants (must match api/handlers/portability.py).
    MPF_VERSION = "0.1.1"
    MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
    # Keep envelope bodies small enough to avoid request-size limits.
    MPF_BATCH_SIZE = 200

    def __init__(
        self,
        endpoint: str = "http://localhost:5002",
        api_key: str = None,
        category: str = "imported",
        dry_run: bool = False,
        preserve_metadata: bool = False,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.category = category
        self.dry_run = dry_run
        # preserve_metadata routes through /v1/import (MPF envelope)
        # with preserve_owner=true, which requires a root bearer token
        # and keeps id/owner_id/namespace/timestamps verbatim.
        self.preserve_metadata = preserve_metadata
        # CHARON sidecar passthrough: when the input file is an MPF
        # envelope with kg_triples / memory_versions /
        # compression_manifest populated, _parse_source stashes the
        # ORIGINAL envelope here so the import path can POST it
        # verbatim as a single request. The earlier design (post
        # records first, then a sidecar trailer) created an ordering
        # bug — the version-snapshot trigger fired during the records
        # batch and the sidecar's authoritative v1 then collided on
        # the partial unique index. Single-envelope POST lets the
        # server's per-transaction trigger guard (mnemos_charon_trigger_guard
        # migration) cover the whole import in one shot.
        self.source_envelope: Optional[Dict[str, Any]] = None

    def _post(self, memories: list) -> tuple:
        """POST a list of memories to MNEMOS.

        When ``preserve_metadata=True`` was set on the importer, routes
        through ``/v1/import`` (MPF envelope, batched) instead of the
        per-memory ``/memories`` path. The MPF path keeps the original
        id, owner_id, namespace, subcategory, timestamps, and
        provenance fields — needed for cross-version migrations.

        Returns:
            (ok_count, fail_count)
        """
        if self.preserve_metadata:
            # CHARON sidecar passthrough: if the source was an MPF
            # envelope WITH sidecars, post it verbatim as a single
            # request. The reconstruct-and-batch path (_post_mpf)
            # would split records and sidecars across multiple POSTs,
            # which leaves a window where the version-snapshot
            # trigger fires on the records batch before the sidecar
            # trailer arrives — collisions on (memory_id, version_num).
            if self.source_envelope is not None:
                return self._post_mpf_passthrough(memories)
            return self._post_mpf(memories)

        ok = 0
        fail = 0
        for mem in memories:
            if self.dry_run:
                preview = str(mem.get("content", ""))[:100].replace("\n", " ")
                cat = mem.get("category", self.category)
                tags = mem.get("tags", [])
                print(f"  DRY RUN  cat={cat!r} tags={tags}  content={preview!r}")
                ok += 1
                continue

            url = f"{self.endpoint}/v1/memories"
            data = json.dumps(mem).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if 200 <= resp.status < 300:
                        ok += 1
                    else:
                        print(f"  WARNING  HTTP {resp.status} for memory")
                        fail += 1
            except urllib.error.HTTPError as exc:
                print(f"  WARNING  POST failed {exc.code}: {exc.reason}")
                fail += 1
            except urllib.error.URLError as exc:
                print(f"  WARNING  POST error: {exc.reason}")
                fail += 1
            except Exception as exc:
                print(f"  WARNING  POST exception: {exc}")
                fail += 1

        return ok, fail

    def _post_mpf(self, memories: list) -> tuple:
        """POST memories as MPF envelope batches to /v1/import?preserve_owner=true.

        Each entry in ``memories`` is the raw memory dict shape (with
        id, owner_id, namespace, created, ...). We wrap it into an
        MPFRecord with kind="memory" and payload_version matching the
        server's. Batched by MPF_BATCH_SIZE to keep request bodies
        bounded (FastAPI default body size is small).
        """
        url = f"{self.endpoint}/v1/import?preserve_owner=true"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        def _record(mem: dict) -> dict:
            # Strip None payload fields to keep envelope tidy; server
            # defaults missing fields.
            payload = {k: v for k, v in {
                "content": mem.get("content"),
                "category": mem.get("category") or self.category,
                "subcategory": mem.get("subcategory"),
                "created": mem.get("created"),
                "updated": mem.get("updated"),
                "owner_id": mem.get("owner_id") or "default",
                "namespace": mem.get("namespace") or "default",
                "permission_mode": mem.get("permission_mode"),
                "quality_rating": mem.get("quality_rating"),
                "metadata": mem.get("metadata") or {},
                "source_model": mem.get("source_model"),
                "source_provider": mem.get("source_provider"),
                "source_session": mem.get("source_session"),
                "source_agent": mem.get("source_agent"),
            }.items() if v is not None}
            return {
                "id": mem.get("id") or _stable_id_from_mem(mem),
                "kind": "memory",
                "payload_version": self.MEMORY_PAYLOAD_VERSION,
                "payload": payload,
            }

        ok = 0
        fail = 0
        for start in range(0, len(memories), self.MPF_BATCH_SIZE):
            batch = memories[start:start + self.MPF_BATCH_SIZE]
            if self.dry_run:
                for mem in batch:
                    preview = str(mem.get("content", ""))[:80].replace("\n", " ")
                    print(f"  DRY RUN  id={mem.get('id')!r}  content={preview!r}")
                ok += len(batch)
                continue

            envelope = {
                "mpf_version": self.MPF_VERSION,
                "source_system": "memory_import",
                "source_version": self.MEMORY_PAYLOAD_VERSION,
                # Required by both docs/mpf_v0.1.json and tools/mpf_validate.py.
                # Earlier importers omitted this and produced envelopes that
                # failed our own validator — added so the importer's output
                # round-trips through the schema check.
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "records": [_record(m) for m in batch],
            }
            data = json.dumps(envelope).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read())
                    imported = int(body.get("imported", 0))
                    skipped = int(body.get("skipped", 0))
                    failed = int(body.get("failed", 0))
                    ok += imported
                    fail += failed
                    print(f"  batch {start//self.MPF_BATCH_SIZE + 1}: "
                          f"imported={imported} skipped={skipped} failed={failed}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                print(f"  WARNING  /v1/import HTTP {exc.code}: {detail}")
                fail += len(batch)
            except urllib.error.URLError as exc:
                print(f"  WARNING  /v1/import error: {exc.reason}")
                fail += len(batch)
            except Exception as exc:
                print(f"  WARNING  /v1/import exception: {exc}")
                fail += len(batch)

        return ok, fail

    def _post_mpf_passthrough(self, memories: list) -> tuple:
        """POST the source MPF envelope verbatim as a single request.

        Used when the parsed source was an MPF envelope WITH sidecars.
        The records-and-sidecars-together posture is required for the
        server's per-transaction version-snapshot trigger guard to
        scope correctly — a separate sidecar trailer would arrive
        AFTER the trigger has already fired on the records batch.

        We don't batch here. Envelopes large enough to need batching
        also need a chunking design that splits sidecars by referenced
        memory_id; that's out of scope for this CLI.
        """
        url = f"{self.endpoint}/v1/import?preserve_owner=true"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.dry_run:
            for mem in memories:
                preview = str(mem.get("content", ""))[:80].replace("\n", " ")
                print(f"  DRY RUN  id={mem.get('id')!r}  content={preview!r}")
            sidecar_summary = ", ".join(
                f"{k}={len(self.source_envelope.get(k) or [])}"
                for k in ("kg_triples", "memory_versions", "compression_manifest")
                if self.source_envelope.get(k)
            )
            if sidecar_summary:
                print(f"  DRY RUN  sidecars: {sidecar_summary}")
            return len(memories), 0

        data = json.dumps(self.source_envelope).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
                imported = int(body.get("imported", 0))
                skipped = int(body.get("skipped", 0))
                failed = int(body.get("failed", 0))
                s_imp = body.get("sidecars_imported") or {}
                s_skip = body.get("sidecars_skipped") or {}
                s_fail = body.get("sidecars_failed") or {}
                print(
                    f"  envelope: imported={imported} skipped={skipped} failed={failed}"
                )
                for k in ("kg_triples", "memory_versions", "compression_manifest"):
                    if (s_imp.get(k) or s_skip.get(k) or s_fail.get(k)):
                        print(
                            f"  sidecar  {k}: imported={s_imp.get(k, 0)} "
                            f"skipped={s_skip.get(k, 0)} failed={s_fail.get(k, 0)}"
                        )
                return imported, failed
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            print(f"  WARNING  /v1/import HTTP {exc.code}: {detail}")
            return 0, len(memories)
        except urllib.error.URLError as exc:
            print(f"  WARNING  /v1/import error: {exc.reason}")
            return 0, len(memories)
        except Exception as exc:
            print(f"  WARNING  /v1/import exception: {exc}")
            return 0, len(memories)

    def run(self) -> dict:
        """Execute the import. Override in subclasses.

        Returns:
            {"imported": N, "failed": N, "skipped": N}
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# JSON importer
# ---------------------------------------------------------------------------

class JsonImporter(BaseImporter):
    """Import MNEMOS JSON / JSONL export or simplified array of memory objects.

    Accepts three wire shapes:

    1. A plain JSON array of memory dicts.
    2. A wrapped object: ``{"memories": [...]}`` or ``{"data": [...]}``.
    3. An MPF envelope: ``{"mpf_version": "0.1.0", "records": [...]}``.
    4. JSONL — one memory dict per line. Enable with ``jsonl=True`` or
       by passing a file with a ``.jsonl`` suffix.

    When ``preserve_metadata=True`` the importer routes through
    ``/v1/import`` (MPF envelope, batched) so the original id,
    owner_id, namespace, subcategory, timestamps, and provenance
    fields are kept verbatim. Required for cross-version MNEMOS
    migrations.
    """

    def __init__(self, file_path: str, jsonl: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.file_path = Path(file_path)
        # Explicit jsonl flag wins; otherwise infer from extension.
        self.jsonl = jsonl or self.file_path.suffix.lower() == ".jsonl"

    def _parse_source(self) -> list:
        """Return a list of raw memory dicts regardless of input shape."""
        if self.jsonl:
            items = []
            # mpf_records — lines that were already MPF-shaped
            # ({id, kind, payload_version, payload}). Pass through
            # verbatim when building the passthrough envelope.
            mpf_records: list = []
            # flat_memory_lines — flat memory-dict lines with no
            # MPF wrapping. If a sidecar trailer arrives later,
            # these get converted to MPF records on the fly so
            # they reach the passthrough envelope (Codex round-4
            # finding: silently dropping these is wrong).
            flat_memory_lines: list = []
            jsonl_sidecars: Dict[str, list] = {}
            with self.file_path.open(encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(f"WARNING: line {line_num}: bad JSON ({exc})",
                              file=sys.stderr)
                        continue
                    # CHARON sidecar trailer:
                    # memory_export.py jsonl mode emits a final line
                    # {"mpf_sidecars": true, "kg_triples": [...], ...}
                    # carrying any populated sidecar arrays. Detect
                    # and capture into source_envelope so the
                    # passthrough path picks them up.
                    if (isinstance(parsed, dict)
                            and parsed.get("mpf_sidecars") is True):
                        for k in ("kg_triples", "memory_versions",
                                  "compression_manifest"):
                            arr = parsed.get(k)
                            if isinstance(arr, list) and arr:
                                jsonl_sidecars[k] = arr
                        continue
                    # Per-line MPF record unwrap:
                    # memory_export.py emits one full MPF record per
                    # line ({id, kind, payload_version, payload}),
                    # not a flat memory dict. Detect that shape and
                    # unwrap the payload (promoting envelope id into
                    # the payload) so the rest of the pipeline treats
                    # it uniformly with the one-shot JSON path. Codex
                    # caught the asymmetry: exporter said "round-trips
                    # via memory_import --jsonl", but every line was
                    # getting dropped as empty because content lived
                    # at payload.content, not top-level content.
                    if (isinstance(parsed, dict)
                            and parsed.get("kind") == "memory"
                            and isinstance(parsed.get("payload"), dict)):
                        payload = dict(parsed["payload"])
                        if "id" in parsed:
                            payload.setdefault("id", parsed["id"])
                        items.append(payload)
                        # Keep the original record shape so we can
                        # rebuild a verbatim envelope below if a
                        # trailer was seen.
                        mpf_records.append(parsed)
                    else:
                        items.append(parsed)
                        # Track flat memory dicts in case a trailer
                        # arrives later and we need to lift them
                        # into MPF shape.
                        if isinstance(parsed, dict) and parsed.get("content"):
                            flat_memory_lines.append(parsed)
            # Trailer-aware source_envelope construction (Codex
            # round-3 finding): always materialize the passthrough
            # envelope when sidecars are present in preserve_metadata
            # mode, even if the records list is empty (sidecar-only
            # input) or the JSONL has flat memory dicts rather than
            # MPF record lines. Otherwise sidecars get silently
            # dropped on these shapes.
            if jsonl_sidecars and self.preserve_metadata:
                # When a sidecar trailer is present, both MPF-shaped
                # lines AND flat memory dicts must reach the
                # passthrough envelope. Lift the flat dicts into MPF
                # records here using the same wrapping logic the
                # JSON envelope path uses, so a flat-records-plus-
                # trailer JSONL imports identically to one-shot JSON.
                lifted: list = []
                for mem in flat_memory_lines:
                    payload = {
                        k: v for k, v in {
                            "content": mem.get("content"),
                            "category": mem.get("category") or self.category,
                            "subcategory": mem.get("subcategory"),
                            "created": mem.get("created"),
                            "updated": mem.get("updated"),
                            "owner_id": mem.get("owner_id") or "default",
                            "namespace": mem.get("namespace") or "default",
                            "permission_mode": mem.get("permission_mode"),
                            "quality_rating": mem.get("quality_rating"),
                            "metadata": mem.get("metadata") or {},
                            "source_model": mem.get("source_model"),
                            "source_provider": mem.get("source_provider"),
                            "source_session": mem.get("source_session"),
                            "source_agent": mem.get("source_agent"),
                        }.items() if v is not None
                    }
                    lifted.append({
                        "id": mem.get("id") or _stable_id_from_mem(mem),
                        "kind": "memory",
                        "payload_version": self.MEMORY_PAYLOAD_VERSION,
                        "payload": payload,
                    })

                all_records = mpf_records + lifted
                self.source_envelope = {
                    "mpf_version": self.MPF_VERSION,
                    "source_system": "memory_import",
                    "source_version": self.MEMORY_PAYLOAD_VERSION,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    # Carry MPF-shaped + lifted-flat records. May be
                    # empty for trailer-only inputs. The server
                    # accepts records=[] as valid (sidecar-only
                    # imports are a documented use-case).
                    "records": all_records,
                    **jsonl_sidecars,
                }
                if not all_records:
                    print(
                        f"  passthrough: trailer-only ({', '.join(jsonl_sidecars)})",
                        file=sys.stderr,
                    )
                elif lifted:
                    print(
                        f"  passthrough: {len(mpf_records)} MPF records + "
                        f"{len(lifted)} flat-memory-records lifted into MPF",
                        file=sys.stderr,
                    )
            elif jsonl_sidecars and not self.preserve_metadata:
                # Without preserve_metadata the per-record path is
                # used, which can't carry sidecars. Warn loudly so
                # operators don't get a silent partial import.
                kinds = ", ".join(jsonl_sidecars.keys())
                print(
                    f"WARNING: input JSONL contains sidecars ({kinds}) "
                    "but --preserve-metadata is not set; sidecars will "
                    "NOT be imported. Re-run with --preserve-metadata "
                    "to use the CHARON envelope passthrough path.",
                    file=sys.stderr,
                )
            return items

        raw = self.file_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Cannot parse JSON: {exc}", file=sys.stderr)
            return []

        # MPF envelope → flatten records back to payload dicts
        # (with the envelope id promoted into the payload).
        if isinstance(data, dict) and "records" in data and "mpf_version" in data:
            flat = []
            for rec in data.get("records", []):
                if not isinstance(rec, dict):
                    continue
                if rec.get("kind") != "memory":
                    continue
                payload = dict(rec.get("payload") or {})
                if "id" in rec:
                    payload.setdefault("id", rec["id"])
                flat.append(payload)
            # CHARON sidecar passthrough: if any sidecar array is
            # present in the source envelope, stash the WHOLE envelope
            # so the import POSTs it verbatim as a single request.
            # This is what guarantees the records and the sidecars hit
            # the server inside one transaction — required for the
            # version-snapshot-trigger guard to scope correctly.
            has_sidecars = any(
                isinstance(data.get(k), list) and data.get(k)
                for k in ("kg_triples", "memory_versions", "compression_manifest")
            )
            if has_sidecars and self.preserve_metadata:
                self.source_envelope = data
            return flat

        # Wrapped export format: {"memories": [...]}
        if isinstance(data, dict):
            data = data.get("memories", data.get("data", list(data.values())))

        if not isinstance(data, list):
            print("ERROR: JSON must be an array, wrapped object, or MPF envelope",
                  file=sys.stderr)
            return []
        return data

    def run(self) -> dict:
        stats = {"imported": 0, "failed": 0, "skipped": 0}
        raw_items = self._parse_source()

        memories = []
        for item in raw_items:
            if not isinstance(item, dict):
                stats["skipped"] += 1
                continue
            content = item.get("content", "")
            if not content or not str(content).strip():
                stats["skipped"] += 1
                continue

            if self.preserve_metadata:
                # Pass-through the whole record; BaseImporter._post_mpf
                # pulls what it needs.
                mem = dict(item)
                mem["content"] = str(content).strip()
                mem.setdefault("category", self.category)
            else:
                # Legacy per-memory POST path — just content/cat/tags/metadata.
                mem = {
                    "content": str(content).strip(),
                    "category": item.get("category", self.category),
                    "tags": item.get("tags", []),
                    "metadata": item.get("metadata", {}),
                }
            memories.append(mem)

        print(f"Loaded {len(memories)} memories from {self.file_path.name} "
              f"({stats['skipped']} skipped)")

        ok, fail = self._post(memories)
        stats["imported"] = ok
        stats["failed"] = fail
        print(f"Result: {ok} imported, {fail} failed")
        return stats


# ---------------------------------------------------------------------------
# CSV importer
# ---------------------------------------------------------------------------

class CsvImporter(BaseImporter):
    """Import rows from a CSV file with configurable column mapping."""

    def __init__(
        self,
        file_path: str,
        content_col: str,
        category_col: str = None,
        tags_col: str = None,
        id_col: str = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.file_path = Path(file_path)
        self.content_col = content_col
        self.category_col = category_col
        self.tags_col = tags_col
        self.id_col = id_col

    def run(self) -> dict:
        stats = {"imported": 0, "failed": 0, "skipped": 0}

        try:
            fh = self.file_path.open(newline="", encoding="utf-8-sig")
        except FileNotFoundError:
            print(f"ERROR: File not found: {self.file_path}", file=sys.stderr)
            return stats

        with fh:
            reader = csv.DictReader(fh)
            if self.content_col not in (reader.fieldnames or []):
                print(f"ERROR: Column '{self.content_col}' not in CSV. "
                      f"Available: {reader.fieldnames}", file=sys.stderr)
                return stats

            memories = []
            for row_num, row in enumerate(reader, start=2):
                content = (row.get(self.content_col) or "").strip()
                if not content:
                    stats["skipped"] += 1
                    continue

                category = self.category
                if self.category_col:
                    category = (row.get(self.category_col) or self.category).strip()

                tags = []
                if self.tags_col:
                    raw_tags = row.get(self.tags_col, "")
                    tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

                meta = {"source_file": self.file_path.name, "row": row_num}
                if self.id_col:
                    meta["original_id"] = row.get(self.id_col, "")

                memories.append({
                    "content": content,
                    "category": category,
                    "tags": tags,
                    "metadata": meta,
                })

        print(f"Loaded {len(memories)} rows from {self.file_path.name} "
              f"({stats['skipped']} skipped)")

        ok, fail = self._post(memories)
        stats["imported"] = ok
        stats["failed"] = fail
        print(f"Result: {ok} imported, {fail} failed")
        return stats


# ---------------------------------------------------------------------------
# ChatGPT importer
# ---------------------------------------------------------------------------

# Keywords that suggest a decision-type memory
_DECISION_KEYWORDS = re.compile(
    r'\b(decide[ds]?|decision|chose|chosen|pick|picked|select|selected|opt|opted|'
    r'go with|going with|prefer|preferred|recommend|recommended|should|must|will use|'
    r'architecture|strategy|approach|plan|design)\b',
    re.IGNORECASE,
)


class ChatGPTImporter(BaseImporter):
    """Import OpenAI conversations.json export into MNEMOS memories."""

    def __init__(self, file_path: str, **kwargs):
        super().__init__(**kwargs)
        self.file_path = Path(file_path)

    def _parse_message_content(self, content_obj) -> str:
        """Extract text from a message content object (parts array or string)."""
        if isinstance(content_obj, str):
            return content_obj
        if isinstance(content_obj, dict):
            parts = content_obj.get("parts", [])
            texts = []
            for p in parts:
                if isinstance(p, str):
                    texts.append(p)
                elif isinstance(p, dict):
                    texts.append(p.get("text", "") or p.get("content", ""))
            return "\n".join(t for t in texts if t)
        return ""

    def _classify_category(self, text: str) -> str:
        """Classify memory as 'decisions' or 'patterns' based on content."""
        if _DECISION_KEYWORDS.search(text):
            return "decisions"
        return "patterns"

    def run(self) -> dict:
        stats = {"imported": 0, "failed": 0, "skipped": 0}

        try:
            raw = self.file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: File not found: {self.file_path}", file=sys.stderr)
            return stats

        try:
            conversations = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Cannot parse JSON: {exc}", file=sys.stderr)
            return stats

        if not isinstance(conversations, list):
            print("ERROR: Expected a top-level JSON array of conversations", file=sys.stderr)
            return stats

        memories = []

        for conv in conversations:
            title = conv.get("title", "Untitled")
            create_time = conv.get("create_time")
            conv_date = None
            if create_time:
                try:
                    conv_date = datetime.fromtimestamp(
                        float(create_time), tz=timezone.utc
                    ).isoformat()
                except (ValueError, TypeError, OSError):
                    conv_date = None

            mapping = conv.get("mapping", {})
            if not mapping:
                stats["skipped"] += 1
                continue

            # Build ordered list of messages from the mapping
            # mapping is {node_id: {id, message, parent, children}}
            ordered_nodes = []
            node_map = {}
            for node_id, node in mapping.items():
                msg = node.get("message")
                if msg:
                    node_map[node_id] = msg

            # Walk tree to get ordered messages (find root then traverse)
            # Find nodes with no parent or parent not in mapping
            child_ids = set()
            for node in mapping.values():
                for cid in node.get("children", []):
                    child_ids.add(cid)

            roots = [nid for nid in mapping if nid not in child_ids]

            def _walk(nid, depth=0):
                node = mapping.get(nid, {})
                msg = node.get("message")
                if msg:
                    ordered_nodes.append(msg)
                for child_id in node.get("children", []):
                    _walk(child_id, depth + 1)

            for root in roots:
                _walk(root)

            # Pair user → assistant messages
            prev_user_text = None
            for msg in ordered_nodes:
                author = (msg.get("author") or {}).get("role", "")
                content_obj = msg.get("content") or {}
                text = self._parse_message_content(content_obj).strip()

                if author == "user":
                    prev_user_text = text
                elif author == "assistant" and text and len(text) >= 100:
                    combined = text
                    if prev_user_text:
                        combined = f"Q: {prev_user_text}\nA: {text}"
                        prev_user_text = None

                    # Honor an explicit --category override; only auto-classify
                    # when the operator left the flag unset (CLI default for
                    # the chatgpt subcommand is None). Earlier behaviour
                    # silently reclassified every assistant turn as
                    # `decisions` / `patterns`, ignoring the operator's
                    # explicit lineage choice.
                    category = self.category if self.category else self._classify_category(combined)
                    meta = {
                        "conversation_title": title,
                        "source_file": self.file_path.name,
                        "import_tool": "chatgpt_import",
                    }
                    if conv_date:
                        meta["conversation_date"] = conv_date

                    memories.append({
                        "content": combined,
                        "category": category,
                        "tags": ["chatgpt", "conversation"],
                        "metadata": meta,
                    })

        print(f"Extracted {len(memories)} assistant messages from "
              f"{len(conversations)} conversation(s)")

        ok, fail = self._post(memories)
        stats["imported"] = ok
        stats["failed"] = fail
        stats["skipped"] += (len(conversations) - ok - fail)
        print(f"Result: {ok} imported, {fail} failed")
        return stats


# ---------------------------------------------------------------------------
# Obsidian importer
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(text: str) -> tuple:
    """Parse YAML frontmatter delimited by '---'.

    Returns:
        (frontmatter_dict, body_text)  — frontmatter_dict is {} if not present.
    """
    if not text.startswith("---"):
        return {}, text

    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}, text

    fm_text = text[3:end_idx].strip()
    body = text[end_idx + 4:].strip()

    fm = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # Handle inline list: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                fm[key] = [v.strip().strip('"\'') for v in inner.split(",") if v.strip()]
            # Handle quoted string
            elif value.startswith('"') and value.endswith('"'):
                fm[key] = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                fm[key] = value[1:-1]
            # Handle YAML list continuation (simple single-line case)
            else:
                fm[key] = value

    return fm, body


class ObsidianImporter(BaseImporter):
    """Import Obsidian vault (.md files with optional YAML frontmatter)."""

    def __init__(self, vault_path: str, **kwargs):
        super().__init__(**kwargs)
        self.vault_path = Path(vault_path)

    def run(self) -> dict:
        stats = {"imported": 0, "failed": 0, "skipped": 0}

        if not self.vault_path.is_dir():
            print(f"ERROR: Not a directory: {self.vault_path}", file=sys.stderr)
            return stats

        md_files = [
            p for p in self.vault_path.rglob("*.md")
            if ".obsidian" not in p.parts
        ]
        md_files.sort()
        print(f"Found {len(md_files)} markdown file(s) in vault (excluding .obsidian/)")

        memories = []
        for fpath in md_files:
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception as exc:
                print(f"  SKIP  {fpath.name}: cannot read ({exc})")
                stats["skipped"] += 1
                continue

            fm, body = _parse_yaml_frontmatter(text)

            if not body.strip():
                stats["skipped"] += 1
                continue

            # Tags from frontmatter
            raw_tags = fm.get("tags", fm.get("tag", []))
            if isinstance(raw_tags, str):
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if t]
            else:
                tags = []

            # Category from frontmatter, else parent directory name
            category = fm.get("category", fm.get("type", None))
            if not category:
                parent = fpath.parent
                if parent != self.vault_path:
                    category = parent.name
                else:
                    category = self.category

            meta = {
                "source_file": fpath.name,
                "source_path": str(fpath.resolve()),
                "vault": str(self.vault_path.resolve()),
                "import_tool": "obsidian_import",
                "import_date": datetime.now(timezone.utc).isoformat(),
            }
            # Carry over any other frontmatter fields as metadata
            for k, v in fm.items():
                if k not in ("tags", "tag", "category", "type"):
                    meta[f"fm_{k}"] = v

            memories.append({
                "content": body.strip(),
                "category": str(category),
                "tags": tags,
                "metadata": meta,
            })

        print(f"Prepared {len(memories)} memories ({stats['skipped']} files skipped)")

        ok, fail = self._post(memories)
        stats["imported"] = ok
        stats["failed"] = fail
        print(f"Result: {ok} imported, {fail} failed")
        return stats


# ---------------------------------------------------------------------------
# Text importer
# ---------------------------------------------------------------------------

class TextImporter(BaseImporter):
    """Import plain .txt or .md files — one memory per file or per paragraph."""

    SUPPORTED_EXTENSIONS = {'.txt', '.md'}

    def __init__(
        self,
        source: str,
        per_paragraph: bool = False,
        min_paragraph_chars: int = 50,
        recursive: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.source = Path(source)
        self.per_paragraph = per_paragraph
        self.min_paragraph_chars = min_paragraph_chars
        self.recursive = recursive

    def _collect_files(self) -> list:
        if self.source.is_file():
            return [self.source]
        if not self.source.is_dir():
            print(f"ERROR: Not a file or directory: {self.source}", file=sys.stderr)
            return []
        glob = "**/*" if self.recursive else "*"
        return sorted(
            p for p in self.source.glob(glob)
            if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXTENSIONS
        )

    def run(self) -> dict:
        stats = {"imported": 0, "failed": 0, "skipped": 0}

        files = self._collect_files()
        if not files:
            print(f"No supported files found in {self.source}")
            return stats

        print(f"Found {len(files)} file(s)")
        memories = []

        for fpath in files:
            try:
                text = fpath.read_text(encoding="utf-8").strip()
            except Exception as exc:
                print(f"  SKIP  {fpath.name}: cannot read ({exc})")
                stats["skipped"] += 1
                continue

            if not text:
                stats["skipped"] += 1
                continue

            meta = {
                "source_file": fpath.name,
                "source_path": str(fpath.resolve()),
                "import_tool": "text_import",
                "import_date": datetime.now(timezone.utc).isoformat(),
            }

            if self.per_paragraph:
                paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
                for para in paragraphs:
                    if len(para) < self.min_paragraph_chars:
                        continue
                    memories.append({
                        "content": para,
                        "category": self.category,
                        "tags": [fpath.suffix.lstrip("."), "text"],
                        "metadata": dict(meta),
                    })
            else:
                memories.append({
                    "content": text,
                    "category": self.category,
                    "tags": [fpath.suffix.lstrip("."), "text"],
                    "metadata": meta,
                })

        print(f"Prepared {len(memories)} memories")

        ok, fail = self._post(memories)
        stats["imported"] = ok
        stats["failed"] = fail
        print(f"Result: {ok} imported, {fail} failed")
        return stats


# ---------------------------------------------------------------------------
# Stats command
# ---------------------------------------------------------------------------

class StatsCommand:
    """Fetch and pretty-print MNEMOS statistics."""

    def __init__(self, endpoint: str = "http://localhost:5002", api_key: str = None):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key

    def run(self) -> None:
        url = f"{self.endpoint}/stats"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            print(f"ERROR: HTTP {exc.code}: {exc.reason}", file=sys.stderr)
            return
        except urllib.error.URLError as exc:
            print(f"ERROR: {exc.reason}", file=sys.stderr)
            return

        # Pretty-print
        print("\n=== MNEMOS Statistics ===\n")
        total = data.get("total_memories", data.get("total", "?"))
        print(f"  Total memories : {total}")

        by_category = data.get("by_category", data.get("categories", {}))
        if by_category:
            print("\n  By category:")
            max_key = max((len(k) for k in by_category), default=10)
            for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
                print(f"    {cat:<{max_key}}  {count}")

        compressions = data.get("compressions", data.get("compression_runs"))
        if compressions is not None:
            print(f"\n  Compression runs: {compressions}")

        last_compression = data.get("last_compression")
        if last_compression:
            print(f"  Last compression: {last_compression}")

        # Print any other top-level keys we haven't handled
        known = {"total_memories", "total", "by_category", "categories",
                 "compressions", "compression_runs", "last_compression"}
        extras = {k: v for k, v in data.items() if k not in known}
        if extras:
            print("\n  Additional info:")
            for k, v in extras.items():
                print(f"    {k}: {v}")

        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add endpoint / api-key / dry-run to a subparser."""
    parser.add_argument("--endpoint", default="http://localhost:5002",
                        help="MNEMOS API base URL (default: http://localhost:5002)")
    parser.add_argument("--api-key", metavar="KEY", default=None,
                        help="Optional Bearer token for MNEMOS auth")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be imported without POSTing")
    parser.add_argument("--preserve-metadata", action="store_true",
                        help="Route through /v1/import (MPF envelope, batched) "
                             "keeping id/owner_id/namespace/timestamps verbatim. "
                             "Requires root bearer token.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_import",
        description="Import memories into MNEMOS from various formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # --- json ---
    p_json = sub.add_parser("json", help="Import from MNEMOS JSON / JSONL export, array, or MPF envelope")
    p_json.add_argument("--file", required=True, metavar="PATH",
                        help="Path to JSON or JSONL file")
    p_json.add_argument("--category", default="imported",
                        help="Default category if not present in records")
    p_json.add_argument("--jsonl", action="store_true",
                        help="Parse as JSONL (one memory per line). "
                             "Auto-enabled for *.jsonl files.")
    _add_common_args(p_json)

    # --- csv ---
    p_csv = sub.add_parser("csv", help="Import from CSV with column mapping")
    p_csv.add_argument("--file", required=True, metavar="PATH",
                       help="Path to CSV file")
    p_csv.add_argument("--content-col", required=True, metavar="COL",
                       help="Column name containing the memory content")
    p_csv.add_argument("--category-col", default=None, metavar="COL",
                       help="Column name for category (optional)")
    p_csv.add_argument("--tags-col", default=None, metavar="COL",
                       help="Column name for comma-separated tags (optional)")
    p_csv.add_argument("--id-col", default=None, metavar="COL",
                       help="Column name for an ID to store in metadata (optional)")
    p_csv.add_argument("--category", default="imported",
                       help="Default category when --category-col absent or empty")
    _add_common_args(p_csv)

    # --- chatgpt ---
    p_cgpt = sub.add_parser("chatgpt", help="Import from OpenAI conversations.json export")
    p_cgpt.add_argument("--file", required=True, metavar="PATH",
                        help="Path to conversations.json")
    p_cgpt.add_argument("--category", default=None,
                        help="Override auto-detected category (decisions/patterns)")
    _add_common_args(p_cgpt)

    # --- obsidian ---
    p_obs = sub.add_parser("obsidian", help="Import from Obsidian vault directory")
    p_obs.add_argument("--vault", required=True, metavar="DIR",
                       help="Path to Obsidian vault root directory")
    p_obs.add_argument("--category", default="notes",
                       help="Default category when not set in frontmatter")
    _add_common_args(p_obs)

    # --- text ---
    p_txt = sub.add_parser("text", help="Import plain .txt or .md files")
    p_txt.add_argument("--source", required=True, metavar="PATH",
                       help="File or directory to import")
    p_txt.add_argument("--category", default="notes",
                       help="Memory category (default: notes)")
    p_txt.add_argument("--per-paragraph", action="store_true",
                       help="Create one memory per paragraph (min 50 chars) instead of per file")
    p_txt.add_argument("--recursive", action="store_true",
                       help="Recurse into sub-directories")
    _add_common_args(p_txt)

    # --- stats ---
    p_stats = sub.add_parser("stats", help="Show MNEMOS memory statistics")
    p_stats.add_argument("--endpoint", default="http://localhost:5002",
                         help="MNEMOS API base URL")
    p_stats.add_argument("--api-key", metavar="KEY", default=None,
                         help="Optional Bearer token for MNEMOS auth")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    common = dict(
        endpoint=args.endpoint,
        api_key=args.api_key,
    )
    if hasattr(args, "preserve_metadata"):
        common["preserve_metadata"] = args.preserve_metadata

    if args.subcommand == "json":
        importer = JsonImporter(
            file_path=args.file,
            category=args.category,
            dry_run=args.dry_run,
            jsonl=getattr(args, "jsonl", False),
            **common,
        )
        importer.run()

    elif args.subcommand == "csv":
        importer = CsvImporter(
            file_path=args.file,
            content_col=args.content_col,
            category_col=args.category_col,
            tags_col=args.tags_col,
            id_col=args.id_col,
            category=args.category,
            dry_run=args.dry_run,
            **common,
        )
        importer.run()

    elif args.subcommand == "chatgpt":
        kwargs = dict(dry_run=args.dry_run, **common)
        if args.category:
            kwargs["category"] = args.category
        importer = ChatGPTImporter(file_path=args.file, **kwargs)
        importer.run()

    elif args.subcommand == "obsidian":
        importer = ObsidianImporter(
            vault_path=args.vault,
            category=args.category,
            dry_run=args.dry_run,
            **common,
        )
        importer.run()

    elif args.subcommand == "text":
        importer = TextImporter(
            source=args.source,
            category=args.category,
            per_paragraph=args.per_paragraph,
            recursive=args.recursive,
            dry_run=args.dry_run,
            **common,
        )
        importer.run()

    elif args.subcommand == "stats":
        cmd = StatsCommand(endpoint=args.endpoint, api_key=args.api_key)
        cmd.run()


if __name__ == "__main__":
    main()

# MNEMOS Tools — CHARON (Memory Portability)

CHARON is MNEMOS's memory portability subsystem — the ferryman that
carries memories between MNEMOS instances and across version
boundaries. Named after the boatman of the Styx for the same reason
the rest of the stack uses Greek names: it's a boundary-crosser and
it's built to be trustworthy when you can't casually recross.

## Components

| Piece | Role |
|---|---|
| `tools/memory_import.py` | Load side. Reads JSON / JSONL / CSV / ChatGPT / Obsidian / text into MNEMOS. |
| `tools/memory_export.py` | Save side. Writes MNEMOS memories as JSON / JSONL / Markdown / HTML / plain text. |
| `tools/export_memories_for_docling.py` | Formatter library for Docling-compatible text outputs (called by `memory_export.py`). |
| `tools/docling_import.py` | Document ingest (PDF / DOCX / HTML / etc) via IBM Docling. |
| `api/handlers/portability.py` | REST endpoints — `GET /v1/export`, `POST /v1/import`. Native MPF envelope. |

## MPF (Memory Portability Format) envelope

```json
{
  "mpf_version": "0.1.0",
  "source_system": "mnemos",
  "source_version": "3.2.0",
  "records": [
    {
      "id": "mem_abc123",
      "kind": "memory",
      "payload_version": "mnemos-3.1",
      "payload": {
        "content": "...",
        "category": "...",
        "subcategory": "...",
        "created": "2026-04-23T...",
        "updated": "2026-04-23T...",
        "owner_id": "...",
        "namespace": "...",
        "quality_rating": 75,
        "metadata": {},
        "source_model": "...",
        "source_provider": "..."
      }
    }
  ]
}
```

## Cross-version migration (v2.3.0 → v3.x)

The `--preserve-metadata` flag on `memory_import.py json` routes
through the MPF envelope path (`POST /v1/import?preserve_owner=true`)
so the original `id`, `owner_id`, `namespace`, `subcategory`,
timestamps, and provenance fields are kept verbatim. Without it, the
importer POSTs to `/memories` and the server assigns fresh ids plus
rewrites owner/namespace to the caller's identity.

### End-to-end example

```bash
# 1. Dump v2 memories as JSONL. Use plain SELECT, NOT `COPY TO STDOUT`
#    — COPY applies a backslash-escape layer that corrupts embedded
#    JSON strings containing newlines or tabs.
PGPASSWORD=$PG_PASSWORD psql -h localhost -U mnemos_user -d mnemos \
  -t -A -c "SELECT row_to_json(t) FROM (
    SELECT id, content, category, subcategory,
           created, updated, metadata,
           owner_id, group_id, namespace,
           quality_rating, source_model, source_provider
    FROM memories ORDER BY created
  ) t" > memories-v2.jsonl

# 2. Validate the JSONL parses cleanly (optional but encouraged).
python3 -c 'import json, sys; [json.loads(l) for l in open("memories-v2.jsonl")]; print("OK")'

# 3. Load into the v3.x instance, preserving ids/owners/namespaces.
python3 tools/memory_import.py json \
    --file memories-v2.jsonl \
    --jsonl \
    --preserve-metadata \
    --endpoint http://localhost:5003 \
    --api-key $MNEMOS_ROOT_TOKEN
```

`--preserve-metadata` requires a root bearer token — the server
refuses `preserve_owner=true` for non-root callers. The import is
idempotent (`ON CONFLICT DO NOTHING` on the `id` column), so re-runs
of the same envelope are no-ops.

### Known pitfall — psql `COPY` escape layer

PostgreSQL `COPY (SELECT row_to_json(t) ...) TO STDOUT` applies
text-format backslash escapes for tab / newline / backslash / null
AFTER `row_to_json` has produced valid JSON. Any memory whose
`content` contains an embedded newline or tab comes out with a
double-escaped sequence that `json.loads` rejects. The dump
_succeeds_ but roughly 40–50% of records fail to parse downstream.

Use plain `psql -t -A -c "SELECT row_to_json(t) FROM ..."`
(tuples-only + unaligned) instead. That writes each row as raw JSON
with no COPY escape layer.

This pitfall was documented during the 2026-04-24 v2.3.0→v3.x
migration of 6,688 memories on PYTHIA; out of the 3,041 first-run
failures, 100% were recovered by re-exporting with plain SELECT.

## Import subcommand reference

All subcommands accept `--endpoint`, `--api-key`, `--dry-run`, and
the new `--preserve-metadata` flag.

### `json` — JSON / JSONL / MPF envelope → MNEMOS

| Flag | Purpose |
|---|---|
| `--file PATH` | Path to `.json` or `.jsonl` file. |
| `--jsonl` | Force JSONL parsing (auto-enabled for `.jsonl`). |
| `--category NAME` | Fallback category when records don't carry one. |
| `--preserve-metadata` | Route through `/v1/import` (MPF) instead of `/memories`. |

Accepts plain arrays, `{"memories": [...]}` / `{"data": [...]}`
wrapped objects, AND full MPF envelopes — `records[*].payload` is
flattened back to raw memory dicts.

### `csv`, `chatgpt`, `obsidian`, `text`

See `--help` on each subcommand. The `--preserve-metadata` flag
works on all of them in principle, but only `json` is guaranteed to
carry the original ids and provenance fields through; the others
synthesize memories from sources that don't have those fields.

### `stats`

Fetches `/stats` and prints a summary. Useful as a pre/post CHARON
round-trip sanity check.

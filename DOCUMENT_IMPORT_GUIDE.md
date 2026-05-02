# MNEMOS Document Import with Docling

v3.0.0 includes intelligent document import using IBM's [Docling](https://www.ibm.com/products/docling) library. This allows automatic conversion of documents (PDFs, Word, Excel, PowerPoint, etc.) into MNEMOS memory records with automatic chunking and metadata extraction.

## Installation

Document import is optional and requires the Docling extra:

```bash
pip install mnemos-os[docling]
```

This installs:
- `docling>=2.5.0` — Main document parsing library
- `docling-core>=2.0.0` — Core parsing utilities
- `pillow>=10.0.0` — Image handling for PDF/multi-format support

If Docling is not installed, the document import endpoints return `501 Not Implemented`.

## Supported Formats

| Format | Extension | Notes |
|--------|-----------|-------|
| PDF | `.pdf` | Full support, including images and tables |
| Word Document | `.docx`, `.doc` | Preserves formatting and structure |
| PowerPoint | `.pptx`, `.ppt` | Converts slides to structured content |
| Excel Spreadsheet | `.xlsx`, `.xls` | Converts sheets to tables |
| Text | `.txt` | Plain text files |
| Markdown | `.md` | Preserves structure and formatting |
| HTML | `.html` | Web content extraction |

## API Endpoints

### Single Document Import

```bash
POST /v1/documents/import
Content-Type: multipart/form-data

file: <document file>
category: "documents"  # Optional, defaults to "documents"
subcategory: "pdfs"     # Optional
```

**Response (200 OK):**
```json
{
  "source_file": "report.pdf",
  "memories_created": 5,
  "memory_ids": ["uuid-1", "uuid-2", ...],
  "chunks_processed": 5,
  "errors": [],
  "metadata": {
    "source_file": "report.pdf",
    "source_type": "PDF",
    "parsed_at": "2026-04-20T10:30:00.000Z",
    "page_count": 12
  },
  "total_text_length": 15240
}
```

### Batch Import

```bash
POST /v1/documents/batch-import
Content-Type: multipart/form-data

files: <multiple document files>
category: "documents"
```

**Response:** Array of import results (one per document)

## Usage Examples

### Python with httpx

```python
import httpx

client = httpx.Client(base_url="http://localhost:5002")

# Single document
with open("research_paper.pdf", "rb") as f:
    response = client.post(
        "/v1/documents/import",
        files={"file": f},
        data={"category": "research", "subcategory": "papers"}
    )
    result = response.json()
    print(f"Created {result['memories_created']} memory records")
    for mem_id in result['memory_ids']:
        print(f"  - {mem_id}")
```

### cURL

```bash
# Single document
curl -X POST http://localhost:5002/v1/documents/import \
  -F "file=@document.pdf" \
  -F "category=documents" \
  -F "subcategory=reports" \
  -H "Authorization: Bearer $TOKEN"

# Batch import
curl -X POST http://localhost:5002/v1/documents/batch-import \
  -F "file=@doc1.pdf" \
  -F "file=@doc2.docx" \
  -F "file=@doc3.xlsx" \
  -F "category=bulk_import" \
  -H "Authorization: Bearer $TOKEN"
```

## How It Works

1. **Document Parsing** — Docling extracts structured content using AI-powered layout analysis
2. **Metadata Extraction** — Automatically captures:
   - Source filename and type
   - Parse timestamp
   - Page count (for PDFs)
   - Section headings and hierarchy
3. **Intelligent Chunking** — Content is split into memory-sized segments (~1500 chars / ~500 tokens) aligned with semantic boundaries (sections, paragraphs)
4. **Memory Creation** — Each chunk becomes a separate memory record with:
   - Content
   - Metadata (source, chunk number, section title, page count)
   - User context (owner_id, namespace)
   - Category/subcategory tags

## Metadata in Created Memories

Each imported document chunk includes:

```json
{
  "id": "memory-uuid",
  "content": "Chunk text content...",
  "category": "documents",
  "subcategory": "pdf",
  "metadata": {
    "source_file": "report.pdf",
    "source_type": "PDF",
    "parsed_at": "2026-04-20T10:30:00Z",
    "page_count": 12,
    "chunk_num": 2,
    "chunk_title": "Methodology"
  },
  "owner_id": "user-uuid",
  "namespace": "default",
  "created": "2026-04-20T10:30:00Z"
}
```

## Error Handling

| Status | Meaning | Example |
|--------|---------|---------|
| **200** | Success | Document parsed, memories created |
| **400** | Bad Request | Empty file, invalid format |
| **401** | Unauthorized | Missing/invalid auth token |
| **413** | Payload Too Large | File exceeds MAX_BODY_BYTES (default 5 MB) |
| **501** | Not Implemented | Docling not installed (`pip install mnemos-os[docling]`) |
| **503** | Service Unavailable | Database not available |

**Example error response:**
```json
{
  "detail": "Document parsing failed: Unsupported file format .xyz"
}
```

## Performance Characteristics

- **PDF (5 pages):** ~2–3 seconds
- **DOCX (50 pages):** ~1–2 seconds
- **XLSX (multiple sheets):** ~1–2 seconds
- **Batch of 10 documents:** ~15–30 seconds (sequential per file, parallel chunks)

Times depend on:
- File size (MB)
- Content complexity (images, tables, formatting)
- System load
- Docling model cache state (first run slower)

## Configuration

No configuration needed — Docling uses sensible defaults. Override via environment variables if needed:

```bash
# Max file size (default 5 MB)
export MAX_BODY_BYTES=10485760  # 10 MB

# Server port (default 5002)
export MNEMOS_PORT=5002

# Docling parser (if multiple available)
# Docling auto-selects best parser for format
```

## Limitations

1. **File size:** Default limit is 5 MB (configurable via MAX_BODY_BYTES)
2. **Languages:** Docling best supports English; other languages may lose some structure
3. **OCR:** Not included by default (requires additional setup)
4. **Tables:** Complex multi-level tables may lose some formatting
5. **Images:** Images are extracted as text descriptions, not stored as binary

## Integration with Other MNEMOS Features

Imported memories automatically integrate with:

- **Semantic Search** — Query imported content with `/v1/memories/search`
- **Full-Text Search** — Find documents by keyword
- **Compression** — On-demand compression of large document chunks
- **DAG Versioning** — Track changes to imported memories
- **RLS (Row-Level Security)** — Imported memories respect user namespace restrictions
- **Audit Logging** — All imports tracked in MNEMOS audit ledger

## Next Steps

After importing documents:

```bash
# Search imported content
curl -X POST http://localhost:5002/v1/memories/search \
  -d '{"query": "key concept", "category": "documents"}' \
  -H "Authorization: Bearer $TOKEN"

# Create memory branch (e.g., for annotations)
curl -X POST http://localhost:5002/v1/memories/{memory_id}/branch \
  -d '{"branch_name": "annotated"}' \
  -H "Authorization: Bearer $TOKEN"

# Update memory with additional context
curl -X PATCH http://localhost:5002/v1/memories/{memory_id} \
  -d '{"metadata": {"custom_tag": "important"}}' \
  -H "Authorization: Bearer $TOKEN"
```

## Troubleshooting

**Q: "Docling not installed" error**
```bash
A: Install with: pip install mnemos-os[docling]
```

**Q: Document parsing fails with "Unsupported format"**
```bash
A: Check file extension is correct. Docling supports: PDF, DOCX, PPTX, XLSX, TXT, MD, HTML
```

**Q: Imports are slow**
```bash
A: Docling models are cached after first run. First run slower (model download). 
   Subsequent imports faster. Check disk space for model cache (~1 GB).
```

**Q: Memory chunking seems off**
```bash
A: Chunking is content-aware (semantic boundaries). Use metadata.chunk_title 
   to understand chunk source. Adjust target_chunk_size in document_import.py (default 1500 chars).
```

## Retry semantics under infrastructure failure

`POST /v1/documents/import` and `POST /v1/documents/batch-import` can return **HTTP 503** when the deployment hits an infrastructure-class failure (asyncpg connection drop, asyncio.TimeoutError on pool acquire, mid-flight commit-ack loss). The 503 payload preserves whatever progress had been confirmed by the database before the failure:

| Field                       | Meaning                                                                       |
|-----------------------------|-------------------------------------------------------------------------------|
| `memories_created`          | Count of chunks whose transaction COMMIT was acknowledged.                    |
| `memory_ids`                | IDs of those confirmed-committed memories.                                    |
| `unconfirmed_memory_ids`    | IDs of chunks whose INSERT was accepted but commit-ack was lost.              |
| `errors`                    | Per-chunk content errors AND a single `infrastructure error: ...` entry.     |

### What `unconfirmed_memory_ids` does NOT mean

A chunk listed in `unconfirmed_memory_ids` is in a **commit-ambiguous** state. The INSERT statement reached Postgres; the `COMMIT` may or may not have succeeded. The client cannot resolve this ambiguity from a single read:

> **A `GET /v1/memories/{id}` returning 404 does NOT prove the commit rolled back.** Under Postgres MVCC, a fresh-connection read can return 404 while the original transaction is still resolving (e.g., visibility lag during a connection-storm). The COMMIT can become visible after the client has already retried, creating a duplicate row.

### Operator-honest retry contract

The stable-chunk-identity primitive shipped in **v4.2.0a14 round-68** (migration `migrations_v4_2_document_import_chunk_idempotency.sql`). Each chunk now carries an `import_chunk_key` derived from `sha256(owner_id NUL namespace NUL source_file NUL chunk_num)`, with a partial UNIQUE index on the column. The chunk INSERT uses `ON CONFLICT (import_chunk_key) DO UPDATE SET import_chunk_key = EXCLUDED.import_chunk_key RETURNING id`, so:

- A retry of the same chunk hits the existing row.
- The `RETURNING id` clause returns the existing row's canonical id.
- The helper appends THAT id to `memory_ids`, not the surrogate `new_memory_id()` value.
- No duplicate row is created.

This closes the duplicate-on-retry hazard documented in earlier rounds. Clients can safely retry an import after a 503, and the response on retry will surface the canonical ids of the chunks that actually committed during the original attempt — `unconfirmed_memory_ids` is now redundant on the retry response (the chunk's id appears in `memory_ids` instead).

### What `unconfirmed_memory_ids` still does

Even with the idempotency primitive, the field stays useful for the **first** 503 response (before the client retries):

- A 200 on `GET /v1/memories/{id}` proves the original commit succeeded; the client can skip the chunk.
- A 404 on the same endpoint does not prove rollback (Postgres MVCC visibility lag), but the client can simply retry the import and trust the ON CONFLICT path to deduplicate. With the v4.2.0a14 migration applied this is safe; without it (older deployments) the operator-honest retry options below still apply.

### Pre-migration retry contract (deployments without round-68's migration)

For deployments still on v4.1.x or pre-round-68 v4.2.0a14 alphas:

1. **Treat the import as failed-pending.** Surface 503 to the human operator; require manual reconciliation.
2. **Accept potential duplicate imports.** Retry the entire file or just the unconfirmed chunks; tolerate that some confirmed-committed memories may be re-imported under fresh non-deterministic IDs.
3. **Skip retry of unconfirmed chunks.** Retry only chunks that 4xx-failed on content (those did rollback).

Apply the v4.2.0a14 migration to enable safe automatic retry.

### Round-68 alpha → round-73+ upgrade (operator action required)

The round-68 alpha of `migrations_v4_2_document_import_chunk_idempotency.sql` created a **partial unique index** (with a `WHERE import_chunk_key IS NOT NULL` predicate). The helper's `ON CONFLICT (import_chunk_key)` clause cannot infer a partial index as its arbiter, so document import fails with `no unique or exclusion constraint matching the ON CONFLICT specification` on every chunk.

Round-73 ships a **non-partial** index via `CREATE UNIQUE INDEX CONCURRENTLY`. The migration is online-safe for fresh installs and for deployments that already have the non-partial index (idempotent). It cannot, however, repair an existing partial index from inside its `psql -f` script (CONCURRENTLY can't run inside a DO block, and a non-CONCURRENT rebuild would block writes).

If you ran the round-68 alpha (any commit between round-68 and round-72), repair the partial index manually BEFORE applying the round-73 migration:

```bash
sudo -u postgres psql -d mnemos -f db/scripts/repair_round_68_partial_chunk_key_index.sql
```

The script does a fully-online `CREATE INDEX CONCURRENTLY` under a temp name + `DROP INDEX CONCURRENTLY` of the old partial + `ALTER INDEX RENAME TO`. No write-blocking step. Idempotent — safe to re-run.

Verify before and after:

```sql
SELECT indpred IS NULL AS is_non_partial
  FROM pg_index
 WHERE indexrelid = 'memories_import_chunk_key_uniq'::regclass;
```

`is_non_partial = t` after the repair. Document import then works on the round-73+ migration.

Fresh installs of v4.2.0a14 round-73+ get the non-partial index directly and do **not** need this script.

## See Also

- [Docling Documentation](https://github.com/DS4SD/docling)
- [Memory API Reference](./API.md#memories)
- [MNEMOS Semantic Search Guide](./SEMANTIC_SEARCH.md)

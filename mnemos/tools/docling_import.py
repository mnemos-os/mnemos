#!/usr/bin/env python3
"""
docling_import.py - IBM Docling integration for importing documents as MNEMOS memories.

Supports PDF, DOCX, DOC, HTML, HTM, Markdown, PPTX, TXT via Docling.

CLI usage:
    python tools/docling_import.py --file /path/to/doc.pdf --endpoint http://localhost:5002
    python tools/docling_import.py --source /path/to/docs --endpoint http://localhost:5002 \
        --category documents --chunk-size 800 --overlap 100 --recursive --tags "tag1,tag2"

Library usage:
    from mnemos.tools.docling_import import DoclingImporter
    importer = DoclingImporter(endpoint="http://localhost:5002", category="documents")
    stats = importer.import_directory(Path("/path/to/docs"), recursive=True)
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class DoclingImporter:
    """Import documents via IBM Docling into MNEMOS as chunked memories."""

    SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.html', '.htm', '.md', '.pptx', '.txt'}

    def __init__(
        self,
        endpoint: str = "http://localhost:5002",
        api_key: str = None,
        category: str = "documents",
        chunk_size: int = 800,
        overlap: int = 100,
        tags: list = None,
        dry_run: bool = False,
    ):
        """
        Args:
            endpoint: MNEMOS API base URL (e.g. http://localhost:5002)
            api_key:  Optional Bearer token for MNEMOS auth
            category: Memory category to assign imported chunks
            chunk_size: Target token count per chunk (1 token ≈ 0.75 words)
            overlap:    Token overlap between consecutive chunks
            tags:       List of extra tags to attach to every memory
            dry_run:    If True, print what would be imported without POSTing
        """
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.category = category
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.tags = tags or []
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_file(self, path: Path) -> list:
        """Extract, chunk, and (optionally) POST a single file.

        Returns:
            List of memory dicts that were (or would be) imported.
        """
        path = Path(path)
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            print(f"  SKIP  {path.name}  (unsupported extension '{path.suffix}')")
            return []

        print(f"  Processing {path.name} ...")
        try:
            sections = self._extract_text(path)
        except ImportError as exc:
            print(f"  ERROR  {path.name}: {exc}")
            raise
        except Exception as exc:
            print(f"  ERROR  {path.name}: {exc}")
            return []

        memories = []
        for section in sections:
            chunks = self._chunk(section["text"], {
                "source_file": path.name,
                "source_path": str(path.resolve()),
                "page": section.get("page"),
                "section": section.get("section"),
                "title": section.get("title"),
                "import_tool": "docling_import",
                "import_date": datetime.now(timezone.utc).isoformat(),
            })
            memories.extend(chunks)

        # Re-number total_chunks across all sections for this file
        total = len(memories)
        for idx, mem in enumerate(memories):
            mem["metadata"]["chunk_index"] = idx
            mem["metadata"]["total_chunks"] = total

        if self.dry_run:
            print(f"  DRY RUN  {path.name}: {total} chunk(s) would be created")
            for mem in memories:
                preview = mem["content"][:120].replace("\n", " ")
                print(f"    chunk {mem['metadata']['chunk_index']}/{total - 1}: "
                      f"{preview!r} | meta={mem['metadata']}")
            return memories

        ok, fail = self._post_batch(memories)
        print(f"  Done  {path.name}: {ok} imported, {fail} failed")
        return memories

    def import_directory(self, path: Path, recursive: bool = False) -> dict:
        """Import all supported files found under *path*.

        Args:
            path:      Directory to scan
            recursive: If True, descend into sub-directories

        Returns:
            Stats dict: {"files_found": N, "files_ok": N, "files_err": N,
                         "memories_imported": N, "memories_failed": N}
        """
        path = Path(path)
        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")

        glob_pattern = "**/*" if recursive else "*"
        candidates = [
            p for p in path.glob(glob_pattern)
            if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXTENSIONS
        ]
        candidates.sort()

        stats = {
            "files_found": len(candidates),
            "files_ok": 0,
            "files_err": 0,
            "memories_imported": 0,
            "memories_failed": 0,
        }

        print(f"Found {len(candidates)} supported file(s) under {path}")
        for i, fpath in enumerate(candidates, start=1):
            print(f"Importing [{i}/{len(candidates)}] {fpath.name}")
            try:
                memories = self.import_file(fpath)
                if memories:
                    if not self.dry_run:
                        # _post_batch already ran inside import_file; just tally
                        stats["files_ok"] += 1
                    else:
                        stats["files_ok"] += 1
                        stats["memories_imported"] += len(memories)
                else:
                    stats["files_err"] += 1
            except Exception:
                stats["files_err"] += 1

        print(f"\nImport complete: {stats}")
        return stats

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_text(self, path: Path) -> list:
        """Use Docling to extract text from *path*.

        Returns:
            List of dicts: [{text, page, section, title}, ...]
        """
        try:
            from docling.document_converter import DocumentConverter
        except ImportError:
            raise ImportError(
                "Docling is not installed.\n"
                "Install docling: pip install docling\n"
                "For OCR support: pip install docling[ocr]"
            )

        converter = DocumentConverter()
        result = converter.convert(str(path))
        doc = result.document

        sections = []

        # Try to iterate export_to_dict structure for rich metadata
        try:
            doc_dict = doc.export_to_dict()
            body = doc_dict.get("body", [])

            current_section = None
            current_title = None

            def _harvest(items, page_num=None):
                nonlocal current_section, current_title
                for item in items:
                    itype = item.get("type", "")
                    text_val = item.get("text", "").strip()
                    pnum = page_num or (item.get("prov", [{}])[0].get("page_no") if item.get("prov") else None)

                    if itype in ("section_header", "title") and text_val:
                        current_title = text_val
                        current_section = text_val
                    elif itype in ("paragraph", "text", "list_item", "table") and text_val:
                        sections.append({
                            "text": text_val,
                            "page": pnum,
                            "section": current_section,
                            "title": current_title,
                        })

                    children = item.get("children", [])
                    if children:
                        _harvest(children, pnum)

            _harvest(body)

        except Exception:
            # Fallback: export whole document as markdown and treat as one section
            try:
                md_text = doc.export_to_markdown()
            except Exception:
                md_text = ""

            if not md_text:
                # Last resort: concatenate all text items
                try:
                    md_text = "\n\n".join(
                        item.text for item in doc.texts if hasattr(item, "text") and item.text
                    )
                except Exception:
                    md_text = str(doc)

            if md_text.strip():
                sections.append({
                    "text": md_text.strip(),
                    "page": None,
                    "section": None,
                    "title": path.stem,
                })

        if not sections:
            # Nothing extracted at all - return one empty-ish entry so caller knows
            sections.append({
                "text": "",
                "page": None,
                "section": None,
                "title": path.stem,
            })

        return sections

    def _chunk(self, text: str, metadata: dict) -> list:
        """Split *text* into overlapping chunks sized by token approximation.

        Token approximation: 1 token ≈ 0.75 words  →  words_per_chunk = chunk_size * 0.75

        Strategy:
          1. Split on paragraph boundaries (\\n\\n)
          2. If a paragraph exceeds chunk_size, split further on sentences ('. ')
          3. Prepend overlap words from the previous chunk

        Returns:
            List of memory dicts ready for POSTing.
        """
        if not text or not text.strip():
            return []

        words_per_chunk = max(1, int(self.chunk_size * 0.75))
        words_per_overlap = max(0, int(self.overlap * 0.75))

        # Step 1: split into paragraphs
        raw_paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

        # Step 2: further split oversized paragraphs on sentence boundaries
        segments = []
        for para in raw_paragraphs:
            words = para.split()
            if len(words) <= words_per_chunk:
                segments.append(para)
            else:
                # Split on '. ' boundaries
                sentences = re.split(r'(?<=\.)\s+', para)
                current_words = []
                for sentence in sentences:
                    s_words = sentence.split()
                    if current_words and len(current_words) + len(s_words) > words_per_chunk:
                        segments.append(" ".join(current_words))
                        current_words = s_words
                    else:
                        current_words.extend(s_words)
                if current_words:
                    segments.append(" ".join(current_words))

        # Step 3: assemble chunks with overlap
        chunks = []
        overlap_words = []

        for seg in segments:
            seg_words = seg.split()

            # Prefix with overlap from previous chunk
            if overlap_words:
                chunk_words = overlap_words + seg_words
            else:
                chunk_words = seg_words

            chunk_text = " ".join(chunk_words)

            # Save last N words as next overlap
            if words_per_overlap > 0:
                overlap_words = seg_words[-words_per_overlap:]
            else:
                overlap_words = []

            # Build memory dict (chunk_index / total_chunks filled in by caller)
            mem = {
                "content": chunk_text,
                "category": self.category,
                "tags": list(self.tags),
                "metadata": dict(metadata),
            }
            # Attach source tags automatically
            source = metadata.get("source_file", "")
            if source:
                ext = Path(source).suffix.lstrip(".")
                if ext and ext not in mem["tags"]:
                    mem["tags"].append(ext)

            chunks.append(mem)

        return chunks

    def _post_memory(self, memory: dict) -> bool:
        """POST a single memory to MNEMOS.

        Returns:
            True on HTTP 2xx, False otherwise.
        """
        url = f"{self.endpoint}/memories"
        data = json.dumps(memory).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            print(f"    WARNING  POST failed {exc.code}: {exc.reason}")
            return False
        except urllib.error.URLError as exc:
            print(f"    WARNING  POST error: {exc.reason}")
            return False
        except Exception as exc:
            print(f"    WARNING  POST exception: {exc}")
            return False

    def _post_batch(self, memories: list, batch_size: int = 20) -> tuple:
        """POST memories in batches.

        Returns:
            (success_count, failure_count)
        """
        ok = 0
        fail = 0
        total = len(memories)
        source = memories[0]["metadata"].get("source_file", "?") if memories else "?"

        for i, mem in enumerate(memories, start=1):
            chunk_idx = mem["metadata"].get("chunk_index", i - 1)
            total_chunks = mem["metadata"].get("total_chunks", total)
            print(f"    Importing [{i}/{total}] {source} chunk {chunk_idx}/{total_chunks - 1} ...")

            if self._post_memory(mem):
                ok += 1
            else:
                fail += 1

            # Yield in batches (no-op here but preserves batch_size contract)
            _ = batch_size  # used for API contract

        return ok, fail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docling_import",
        description="Import documents into MNEMOS memories via IBM Docling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file
  python tools/docling_import.py --file report.pdf --endpoint http://localhost:5002

  # Directory (recursive)
  python tools/docling_import.py --source /docs --recursive --category documents \\
      --tags "project,q1" --dry-run

  # With auth
  python tools/docling_import.py --file deck.pptx --api-key secret123
""",
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--file", metavar="PATH", help="Single file to import")
    source_group.add_argument("--source", metavar="DIR", help="Directory of files to import")

    parser.add_argument("--endpoint", default="http://localhost:5002",
                        help="MNEMOS API base URL (default: http://localhost:5002)")
    parser.add_argument("--api-key", metavar="KEY", default=None,
                        help="Optional Bearer token for MNEMOS auth")
    parser.add_argument("--category", default="documents",
                        help="Memory category (default: documents)")
    parser.add_argument("--chunk-size", type=int, default=800, metavar="TOKENS",
                        help="Target tokens per chunk (default: 800)")
    parser.add_argument("--overlap", type=int, default=100, metavar="TOKENS",
                        help="Overlap tokens between chunks (default: 100)")
    parser.add_argument("--tags", metavar="TAG1,TAG2", default="",
                        help="Comma-separated extra tags to attach to every memory")
    parser.add_argument("--recursive", action="store_true",
                        help="Recurse into sub-directories (only with --source)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be imported without POSTing")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    importer = DoclingImporter(
        endpoint=args.endpoint,
        api_key=args.api_key,
        category=args.category,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        tags=tags,
        dry_run=args.dry_run,
    )

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"ERROR: File not found: {path}", file=sys.stderr)
            sys.exit(1)
        memories = importer.import_file(path)
        total = len(memories)
        print(f"\nResult: {total} chunk(s) processed from {path.name}")
    else:
        path = Path(args.source)
        if not path.is_dir():
            print(f"ERROR: Not a directory: {path}", file=sys.stderr)
            sys.exit(1)
        stats = importer.import_directory(path, recursive=args.recursive)
        print(f"\nFinal stats: {stats}")


if __name__ == "__main__":
    main()

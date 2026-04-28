"""Export MNEMOS memories in Docling-compatible formats (Markdown, HTML, Plain Text).

This is a library of formatters. To fetch memories from a running server,
set the following environment variables and use your own HTTP client:

    MNEMOS_BASE      — API base URL (e.g. http://localhost:5002)
    MNEMOS_API_KEY   — Bearer token for authentication

Nothing in this module makes network calls or carries a default host/key —
bring your own.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


def export_memories_markdown(memories: List[Dict[str, Any]], output_file: Path) -> None:
    """Export memories as a single Markdown document (Docling-compatible)."""
    with open(output_file, 'w') as f:
        f.write("# MNEMOS Memory Export\n\n")
        f.write(f"Exported: {datetime.now().isoformat()}\n")
        f.write(f"Total memories: {len(memories)}\n\n")
        f.write("---\n\n")

        for mem in memories:
            f.write(f"## {mem.get('id', 'Unknown')}\n\n")
            f.write(f"**Category:** {mem.get('category', 'N/A')}\n\n")
            if mem.get('subcategory'):
                f.write(f"**Subcategory:** {mem.get('subcategory')}\n\n")
            f.write(f"**Created:** {mem.get('created', 'N/A')}\n\n")
            f.write("### Content\n\n")
            f.write(mem.get('content', '(empty)'))
            f.write("\n\n---\n\n")


def export_memories_plaintext(memories: List[Dict[str, Any]], output_file: Path) -> None:
    """Export memories as plain text (Docling-compatible)."""
    with open(output_file, 'w') as f:
        f.write("MNEMOS MEMORY EXPORT\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Exported: {datetime.now().isoformat()}\n")
        f.write(f"Total memories: {len(memories)}\n\n")
        f.write("=" * 80 + "\n\n")

        for mem in memories:
            f.write(f"ID: {mem.get('id', 'Unknown')}\n")
            f.write(f"Category: {mem.get('category', 'N/A')}\n")
            if mem.get('subcategory'):
                f.write(f"Subcategory: {mem.get('subcategory')}\n")
            f.write(f"Created: {mem.get('created', 'N/A')}\n")
            f.write("\n" + "-" * 80 + "\n\n")
            f.write(mem.get('content', '(empty)'))
            f.write("\n\n" + "=" * 80 + "\n\n")


def export_memories_html(memories: List[Dict[str, Any]], output_file: Path) -> None:
    """Export memories as HTML (Docling-compatible)."""
    with open(output_file, 'w') as f:
        f.write("<!DOCTYPE html>\n")
        f.write("<html>\n<head>\n")
        f.write("<meta charset='UTF-8'>\n")
        f.write("<title>MNEMOS Memory Export</title>\n")
        f.write("<style>\n")
        f.write("body { font-family: Arial, sans-serif; line-height: 1.6; margin: 20px; }\n")
        f.write(".memory { border: 1px solid #ddd; padding: 15px; margin: 20px 0; }\n")
        f.write(".memory-id { font-weight: bold; color: #333; }\n")
        f.write(".memory-meta { color: #666; font-size: 0.9em; margin: 10px 0; }\n")
        f.write(".memory-content { white-space: pre-wrap; background: #f5f5f5; padding: 10px; }\n")
        f.write("</style>\n")
        f.write("</head>\n<body>\n")
        f.write("<h1>MNEMOS Memory Export</h1>\n")
        f.write(f"<p>Exported: {datetime.now().isoformat()}</p>\n")
        f.write(f"<p>Total memories: {len(memories)}</p>\n")

        for mem in memories:
            f.write("<div class='memory'>\n")
            f.write(f"<div class='memory-id'>{mem.get('id', 'Unknown')}</div>\n")
            f.write("<div class='memory-meta'>\n")
            f.write(f"Category: {mem.get('category', 'N/A')}<br/>\n")
            if mem.get('subcategory'):
                f.write(f"Subcategory: {mem.get('subcategory')}<br/>\n")
            f.write(f"Created: {mem.get('created', 'N/A')}\n")
            f.write("</div>\n")
            f.write("<div class='memory-content'>\n")
            f.write(mem.get('content', '(empty)'))
            f.write("</div>\n")
            f.write("</div>\n")

        f.write("</body>\n</html>\n")


def print_usage():
    """Print usage instructions."""
    print("""
MNEMOS Memory Export for Docling
================================

This module provides formatters to render MNEMOS memories as Markdown, plain
text, or HTML suitable for ingestion by IBM Docling.

Usage (from Python):

  from mnemos.tools.export_memories_for_docling import (
      export_memories_markdown,
      export_memories_plaintext,
      export_memories_html,
  )

  memories = [...]  # fetched from your MNEMOS server
  export_memories_markdown(memories, Path('output.md'))
  export_memories_plaintext(memories, Path('output.txt'))
  export_memories_html(memories, Path('output.html'))

Supported Docling input formats:
  - Markdown (.md)
  - Plain Text (.txt)
  - HTML (.html)
  - PDF (.pdf) — if Docling has PDF support

Fetching memories from MNEMOS:

  export MNEMOS_BASE=http://localhost:5002
  export MNEMOS_API_KEY=<your-bearer-token>

  curl -X POST "$MNEMOS_BASE/v1/memories/search" \\
    -H "Authorization: Bearer $MNEMOS_API_KEY" \\
    -H 'Content-Type: application/json' \\
    -d '{"query":"*","limit":10000}'

Feed the JSON result into one of the formatters above.
""")


if __name__ == "__main__":
    print_usage()

"""Document import utilities using Docling for intelligent content extraction."""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

try:
    from docling.document_converter import DocumentConverter
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/documents", tags=["document-import"])


class DoclingImporter:
    """Handles document parsing and memory extraction via Docling."""

    def __init__(self):
        if not DOCLING_AVAILABLE:
            raise ImportError("Docling not installed. Install with: pip install mnemos-os[docling]")
        self.converter = DocumentConverter()

    def parse_document(
        self, file_content: bytes, filename: str
    ) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
        """Parse document and extract content, metadata, and chunks.

        Returns:
            (full_text, metadata, chunks) where chunks are memory-sized segments
        """
        try:
            # Parse document with Docling
            doc = self.converter.convert_bytes(
                file_content,
                file_name=filename,
                format_hint=self._guess_format(filename),
            )

            # Extract full text
            full_text = doc.document.export_to_markdown()

            # Extract metadata
            metadata = {
                "source_file": filename,
                "source_type": self._get_document_type(filename),
                "parsed_at": datetime.utcnow().isoformat(),
                "page_count": len(doc.pages) if hasattr(doc, "pages") else None,
            }

            # Create memory chunks (split by semantic boundaries)
            chunks = self._chunk_content(
                full_text, metadata, doc
            )

            logger.info(
                f"[DOCLING] Parsed {filename}: {len(full_text)} chars, "
                f"{len(chunks)} chunks, {metadata.get('page_count', '?')} pages"
            )

            return full_text, metadata, chunks

        except Exception as e:
            logger.error(f"[DOCLING] Parse error for {filename}: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Document parsing failed: {str(e)}"
            )

    def _guess_format(self, filename: str) -> str:
        """Guess document format from filename."""
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        format_map = {
            "pdf": "pdf",
            "docx": "docx",
            "doc": "docx",
            "pptx": "pptx",
            "ppt": "pptx",
            "xlsx": "xlsx",
            "xls": "xlsx",
            "txt": "txt",
            "md": "md",
            "html": "html",
        }
        return format_map.get(ext, "auto")

    def _get_document_type(self, filename: str) -> str:
        """Extract document type from filename."""
        ext = filename.lower().split(".")[-1] if "." in filename else "unknown"
        type_map = {
            "pdf": "PDF",
            "docx": "Word Document",
            "doc": "Word Document",
            "pptx": "PowerPoint",
            "ppt": "PowerPoint",
            "xlsx": "Excel Spreadsheet",
            "xls": "Excel Spreadsheet",
            "txt": "Text File",
            "md": "Markdown",
            "html": "HTML",
        }
        return type_map.get(ext, "Unknown")

    def _chunk_content(
        self,
        text: str,
        metadata: Dict[str, Any],
        doc: Any,
    ) -> List[Dict[str, Any]]:
        """Split document content into memory-sized chunks with semantic awareness."""
        chunks = []
        target_chunk_size = 1500  # ~500 tokens, typical memory unit

        # Try to use document structure if available
        sections = self._extract_sections(text, doc)

        current_chunk = ""
        current_metadata = metadata.copy()
        chunk_num = 0

        for section_title, section_text in sections:
            if len(current_chunk) + len(section_text) > target_chunk_size:
                if current_chunk:
                    chunks.append({
                        "chunk_num": chunk_num,
                        "title": section_title or current_metadata.get("chunk_title", ""),
                        "content": current_chunk.strip(),
                        "metadata": {**current_metadata, "chunk_num": chunk_num},
                    })
                    chunk_num += 1
                current_chunk = section_text
            else:
                current_chunk += f"\n{section_text}" if current_chunk else section_text

        # Final chunk
        if current_chunk:
            chunks.append({
                "chunk_num": chunk_num,
                "title": sections[-1][0] if sections else "Content",
                "content": current_chunk.strip(),
                "metadata": {**current_metadata, "chunk_num": chunk_num},
            })

        return chunks

    def _extract_sections(
        self,
        text: str,
        doc: Any,
    ) -> List[Tuple[str, str]]:
        """Extract hierarchical sections from document for better chunking."""
        sections = []

        # Simple heuristic: split by markdown headings
        lines = text.split("\n")
        current_section = ""
        current_title = ""

        for line in lines:
            if line.startswith("#"):
                if current_section:
                    sections.append((current_title, current_section))
                current_title = line.lstrip("#").strip()
                current_section = ""
            else:
                current_section += f"{line}\n"

        if current_section:
            sections.append((current_title, current_section))

        return sections if sections else [("", text)]


async def import_memories_from_document(
    file: UploadFile,
    category: str = Form("documents"),
    subcategory: Optional[str] = Form(None),
    user: UserContext = Depends(get_current_user),
) -> Dict[str, Any]:
    """Import document into MNEMOS as memory records.

    Creates one memory per document chunk with automatic metadata extraction.
    Requires docling extra: pip install mnemos-os[docling]
    """
    if not DOCLING_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="Docling not installed. Install with: pip install mnemos-os[docling]"
        )

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database not available")

    # Read file
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # Parse with Docling
    importer = DoclingImporter()
    full_text, doc_metadata, chunks = importer.parse_document(
        content, file.filename or "document"
    )

    # Create memories from chunks. Match the canonical /v1/memories create
    # path exactly: `mem_<hex12>` id, populate verbatim_content +
    # quality_rating + permission_mode, and fire `memory.created` webhooks +
    # search-cache invalidation so document-imported memories behave like
    # any other for downstream subscribers and search consumers. The
    # AFTER INSERT trigger writes memory_versions v1 automatically.
    memory_ids = []
    errors = []

    async with _lc._pool.acquire() as conn:
        for chunk in chunks:
            try:
                memory_id = f"mem_{uuid.uuid4().hex[:12]}"
                chunk_metadata = {
                    **doc_metadata,
                    **chunk["metadata"],
                    "chunk_title": chunk["title"],
                }

                await conn.execute(
                    "INSERT INTO memories "
                    "(id, content, category, subcategory, metadata, quality_rating, "
                    " verbatim_content, owner_id, namespace, permission_mode) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, 600)",
                    memory_id,
                    chunk["content"],
                    category,
                    subcategory,
                    json.dumps(chunk_metadata),
                    chunk["content"],            # verbatim_content == content for chunks
                    user.user_id,
                    user.namespace,
                )
                memory_ids.append(memory_id)
                logger.debug(f"[DOCLING] Created memory {memory_id} from chunk {chunk['chunk_num']}")

            except Exception as e:
                logger.error(f"[DOCLING] Failed to create memory for chunk {chunk['chunk_num']}: {e}")
                errors.append({"chunk": chunk["chunk_num"], "error": str(e)})

    # Side-effects done outside the per-chunk acquire so the connection isn't
    # held while we contact Redis / dispatch webhooks. Cache is invalidated
    # ONCE after the whole document — a 100-chunk document would otherwise
    # thrash search cache N times. Webhooks fire per-memory to stay
    # consistent with the single-create endpoint's semantics.
    if memory_ids:
        if _lc._cache:
            try:
                await _lc._cache.delete("stats:global")
                try:
                    async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                        await _lc._cache.delete(_k)
                except Exception:
                    pass
            except Exception:
                pass
        try:
            from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook
            for mid, chunk in zip(memory_ids, chunks):
                await _dispatch_webhook(
                    "memory.created",
                    {
                        "memory_id": mid,
                        "category": category,
                        "subcategory": subcategory,
                        "content": chunk["content"],
                        "owner_id": user.user_id,
                        "namespace": user.namespace,
                        "source": "document_import",
                    },
                    owner_id=user.user_id,
                    namespace=user.namespace,
                )
        except Exception:
            logger.warning(
                "webhook dispatch failed for document_import (%d memories)",
                len(memory_ids), exc_info=True,
            )

    return {
        "source_file": file.filename,
        "memories_created": len(memory_ids),
        "memory_ids": memory_ids,
        "chunks_processed": len(chunks),
        "errors": errors,
        "metadata": doc_metadata,
        "total_text_length": len(full_text),
    }


# Route: POST /v1/documents/import
@router.post("/import", response_model=dict)
async def import_document(
    file: UploadFile = File(...),
    category: str = Form("documents"),
    subcategory: Optional[str] = Form(None),
    user: UserContext = Depends(get_current_user),
):
    """Import document file into MNEMOS as memory records.

    Supported formats: PDF, DOCX, PPTX, XLSX, TXT, MD, HTML

    Returns: {
        source_file: filename,
        memories_created: number of memory records,
        memory_ids: list of created memory UUIDs,
        chunks_processed: number of content chunks,
        errors: any chunk-level errors,
        metadata: extracted document metadata,
        total_text_length: total character count
    }
    """
    return await import_memories_from_document(file, category, subcategory, user)


# Route: POST /v1/documents/batch-import
@router.post("/batch-import", response_model=list)
async def batch_import_documents(
    files: List[UploadFile] = File(...),
    category: str = Form("documents"),
    user: UserContext = Depends(get_current_user),
):
    """Batch import multiple documents into MNEMOS.

    Returns list of import results (one per document).
    """
    results = []
    for file in files:
        try:
            result = await import_memories_from_document(
                file, category=category, subcategory=None, user=user
            )
            results.append(result)
        except HTTPException as e:
            results.append({
                "source_file": file.filename,
                "error": e.detail,
                "memories_created": 0,
            })
    return results

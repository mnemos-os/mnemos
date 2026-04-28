"""Integration tests for Docling document import functionality."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


class TestDoclingImporter:
    """Test document parsing and chunk creation."""

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    def test_docling_import_available(self):
        """Verify Docling import gracefully handles availability."""
        # When DOCLING_AVAILABLE is True, importer should initialize
        from mnemos.api.routes.document_import import DoclingImporter

        # Mock the DocumentConverter
        with patch('mnemos.api.routes.document_import.DocumentConverter'):
            importer = DoclingImporter()
            assert importer.converter is not None

    def test_guess_format(self):
        """Test format detection from filename."""
        from mnemos.api.routes.document_import import DoclingImporter

        with patch('mnemos.api.routes.document_import.DocumentConverter'):
            importer = DoclingImporter()

            assert importer._guess_format("document.pdf") == "pdf"
            assert importer._guess_format("report.docx") == "docx"
            assert importer._guess_format("presentation.pptx") == "pptx"
            assert importer._guess_format("data.xlsx") == "xlsx"
            assert importer._guess_format("notes.txt") == "txt"
            assert importer._guess_format("README.md") == "md"
            assert importer._guess_format("page.html") == "html"
            assert importer._guess_format("unknown.xyz") == "auto"

    def test_get_document_type(self):
        """Test document type extraction."""
        from mnemos.api.routes.document_import import DoclingImporter

        with patch('mnemos.api.routes.document_import.DocumentConverter'):
            importer = DoclingImporter()

            assert importer._get_document_type("paper.pdf") == "PDF"
            assert importer._get_document_type("report.docx") == "Word Document"
            assert importer._get_document_type("slides.pptx") == "PowerPoint"
            assert importer._get_document_type("budget.xlsx") == "Excel Spreadsheet"

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    def test_chunk_content(self):
        """Test content chunking into memory-sized segments."""
        from mnemos.api.routes.document_import import DoclingImporter

        with patch('mnemos.api.routes.document_import.DocumentConverter'):
            importer = DoclingImporter()

            text = "# Section 1\nContent for section 1\n# Section 2\nContent for section 2"
            metadata = {"source_file": "test.md", "source_type": "Markdown"}
            doc = MagicMock()

            chunks = importer._chunk_content(text, metadata, doc)

            assert len(chunks) > 0
            assert all("chunk_num" in c for c in chunks)
            assert all("content" in c for c in chunks)
            assert all("metadata" in c for c in chunks)

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    def test_extract_sections(self):
        """Test markdown section extraction."""
        from mnemos.api.routes.document_import import DoclingImporter

        with patch('mnemos.api.routes.document_import.DocumentConverter'):
            importer = DoclingImporter()

            text = "# Heading 1\nContent 1\n## Heading 2\nContent 2"
            doc = MagicMock()

            sections = importer._extract_sections(text, doc)

            assert len(sections) > 0
            # Each section is a tuple of (title, content)
            assert all(isinstance(s, tuple) and len(s) == 2 for s in sections)


class TestDocumentImportEndpoints:
    """Test HTTP endpoints for document import."""

    @pytest.fixture
    async def client(self):
        """Create test HTTP client."""
        from httpx import AsyncClient

        from mnemos.api.main import app

        async with AsyncClient(app=app, base_url="http://test") as ac:
            yield ac

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', False)
    async def test_import_document_not_available(self, client):
        """Test graceful handling when Docling is not installed."""
        # Create a test PDF file
        pdf_bytes = b"%PDF-1.4\n%test pdf"

        response = await client.post(
            "/v1/documents/import",
            files={"file": ("test.pdf", pdf_bytes)},
            data={"category": "documents"}
        )

        # Should return 501 Not Implemented
        assert response.status_code == 501
        assert "Docling not installed" in response.json()["detail"]

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    @patch('mnemos.api.routes.document_import._pool')
    async def test_import_document_empty_file(self, mock_pool, client):
        """Test rejection of empty files."""
        response = await client.post(
            "/v1/documents/import",
            files={"file": ("empty.pdf", b"")},
            data={"category": "documents"}
        )

        assert response.status_code == 400
        assert "Empty file" in response.json()["detail"]

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    @patch('mnemos.api.routes.document_import.DoclingImporter')
    @patch('mnemos.api.routes.document_import._pool')
    async def test_import_document_success(self, mock_pool, mock_importer_class, client):
        """Test successful document import."""
        # Mock Docling parser
        mock_importer = MagicMock()
        mock_importer.parse_document.return_value = (
            "Full text content",
            {"source_file": "test.pdf", "page_count": 5},
            [
                {
                    "chunk_num": 0,
                    "title": "Introduction",
                    "content": "Chunk 0 content",
                    "metadata": {"source_file": "test.pdf", "chunk_num": 0}
                },
                {
                    "chunk_num": 1,
                    "title": "Content",
                    "content": "Chunk 1 content",
                    "metadata": {"source_file": "test.pdf", "chunk_num": 1}
                }
            ]
        )
        mock_importer_class.return_value = mock_importer

        # Mock DB
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None

        pdf_bytes = b"%PDF-1.4\n%test pdf content"

        response = await client.post(
            "/v1/documents/import",
            files={"file": ("test.pdf", pdf_bytes)},
            data={"category": "documents", "subcategory": "papers"}
        )

        # Should succeed
        assert response.status_code == 200
        result = response.json()

        assert result["source_file"] == "test.pdf"
        assert result["memories_created"] == 2
        assert len(result["memory_ids"]) == 2
        assert result["chunks_processed"] == 2

    @patch('mnemos.api.routes.document_import.DOCLING_AVAILABLE', True)
    @patch('mnemos.api.routes.document_import.DoclingImporter')
    @patch('mnemos.api.routes.document_import._pool')
    async def test_batch_import_documents(self, mock_pool, mock_importer_class, client):
        """Test batch document import."""
        # Mock Docling
        mock_importer = MagicMock()
        mock_importer.parse_document.return_value = (
            "Content", {"page_count": 1}, [{"chunk_num": 0, "title": "Test", "content": "Text", "metadata": {}}]
        )
        mock_importer_class.return_value = mock_importer

        # Mock DB
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None

        response = await client.post(
            "/v1/documents/batch-import",
            files=[
                ("file", ("doc1.pdf", b"%PDF-1.4\ntest1")),
                ("file", ("doc2.pdf", b"%PDF-1.4\ntest2")),
            ],
            data={"category": "documents"}
        )

        assert response.status_code == 200
        results = response.json()
        assert len(results) == 2


class TestDoclingIntegration:
    """Test end-to-end document import workflow."""

    @pytest.mark.skipif(
        not pytest.importorskip("docling", minversion=None),
        reason="Docling not installed"
    )
    def test_parse_markdown_document(self):
        """Test parsing markdown-like content."""
        from mnemos.api.routes.document_import import DoclingImporter

        # Create mock Docling response for markdown
        mock_doc = MagicMock()
        mock_doc.document.export_to_markdown.return_value = """
# Introduction
This is the introduction.

## Background
Some background info.

# Main Content
Main content here.

## Details
More details.
"""

        with patch('mnemos.api.routes.document_import.DocumentConverter') as mock_converter_class:
            mock_converter = MagicMock()
            mock_converter.convert_bytes.return_value = mock_doc
            mock_converter_class.return_value = mock_converter

            importer = DoclingImporter()
            full_text, metadata, chunks = importer.parse_document(
                b"mock content", "test.md"
            )

            assert full_text
            assert "Introduction" in full_text
            assert len(chunks) > 0
            assert metadata["source_file"] == "test.md"
            assert metadata["source_type"] == "Markdown"

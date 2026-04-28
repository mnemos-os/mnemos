"""Tests for GET /v1/memories/{id}/compression-manifests.

Pure-logic check on _render_content_preview. The full-endpoint
behavior was live-validated on the CERBERUS test instance against
real winner/no-winner/404 cases (see the 2026-04-23 benchmark doc).
"""

from __future__ import annotations

import pytest

from mnemos.api.routes.memories import _render_content_preview


@pytest.mark.parametrize(
    "content, include_content, expected",
    [
        # None in → None out regardless of flag
        (None, False, None),
        (None, True, None),
        # Short content: returned whole in both modes
        ("hello", False, "hello"),
        ("hello", True, "hello"),
        # Exactly at the 200-char boundary: returned whole (preview only
        # elides strictly longer)
        ("x" * 200, False, "x" * 200),
        ("x" * 200, True, "x" * 200),
        # Over 200 chars: truncate + ellipsis in preview mode; full in
        # include_content mode
        ("x" * 350, False, "x" * 200 + "…"),
        ("x" * 350, True, "x" * 350),
        # Empty string: empty, not None
        ("", False, ""),
        ("", True, ""),
    ],
)
def test_render_content_preview(content, include_content, expected):
    assert _render_content_preview(content, include_content) == expected

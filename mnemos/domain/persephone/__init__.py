"""PERSEPHONE archival subsystem."""

from mnemos.core.extras import require_extra

require_extra("persephone")

from mnemos.domain.persephone.runner import (
    archive_memory,
    is_archived,
    restore_memory,
    sweep_for_archival,
)

__all__ = [
    "archive_memory",
    "is_archived",
    "restore_memory",
    "sweep_for_archival",
]

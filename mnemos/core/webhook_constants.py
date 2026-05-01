"""Webhook constants needed below the webhook layer.

These constants are referenced by the persistence layer when it
INSERTs into ``webhook_deliveries``. They live here (and not in
``mnemos.webhooks.types``) so the import graph stays clean —
persistence cannot import from webhooks per the layered-architecture
contract.

The webhook layer re-exports these symbols from
``mnemos.webhooks.types`` for backwards compatibility.
"""
from __future__ import annotations

# Schema-version sentinel written into the ``writer_revision`` column
# of ``webhook_deliveries`` rows. Bumped when the row shape changes
# in a way that recovery workers need to distinguish (e.g. column
# additions that legacy rows do not have).
NEW_CODE_WRITER_REVISION = 1

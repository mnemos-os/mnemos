"""Shared authentication context DTOs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class UserContext:
    user_id: str
    group_ids: List[str]
    role: str
    namespace: str
    authenticated: bool
    session_id: str | None = None

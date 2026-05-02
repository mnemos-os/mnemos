"""Per-session usage caps for PANTHEON consultation-only models."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class ConsultationCapResult:
    allowed: bool
    cap: int
    used: int
    remaining: int


class InMemoryConsultationCapBucket:
    """Process-local hard-cap bucket keyed by ``(user_id, session_id)``."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    def check_and_increment(self, *, user_id: str, session_id: str, cap: int) -> ConsultationCapResult:
        key = (user_id, session_id)
        with self._lock:
            used = self._counts.get(key, 0)
            if used >= cap:
                return ConsultationCapResult(
                    allowed=False,
                    cap=cap,
                    used=used,
                    remaining=0,
                )
            used += 1
            self._counts[key] = used
            return ConsultationCapResult(
                allowed=True,
                cap=cap,
                used=used,
                remaining=max(cap - used, 0),
            )

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


consultation_cap_bucket = InMemoryConsultationCapBucket()


__all__ = [
    "ConsultationCapResult",
    "InMemoryConsultationCapBucket",
    "consultation_cap_bucket",
]

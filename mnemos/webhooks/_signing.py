"""Webhook HMAC signing helper."""
from __future__ import annotations

import hashlib
import hmac


def _sign(secret: str, body: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

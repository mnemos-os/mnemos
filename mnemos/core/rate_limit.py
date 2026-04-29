"""MNEMOS HTTP rate limiting.

On by default (`RATE_LIMIT_ENABLED=true`). Tune via environment variables:

    RATE_LIMIT_ENABLED=true          # opt-out by setting "false"
    RATE_LIMIT_DEFAULT=300/minute    # global ceiling per client
    RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1  # or memory:// (single-worker)
    RATE_LIMIT_TRUST_PROXY=false     # set true ONLY behind a trusted reverse proxy
                                     # that rewrites X-Forwarded-For.

Route-specific limits (e.g. on `/v1/consultations`) are applied via
`@limiter.limit()` in the relevant handler.
"""
import logging

from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: F401 — re-exported
from slowapi.errors import RateLimitExceeded  # noqa: F401
from slowapi.middleware import SlowAPIMiddleware  # noqa: F401
from slowapi.util import get_remote_address
from starlette.requests import Request

from mnemos.core.config import get_settings

logger = logging.getLogger(__name__)

_RATE_LIMIT_SETTINGS = get_settings().rate_limit
RATE_LIMIT_ENABLED = _RATE_LIMIT_SETTINGS.enabled
RATE_LIMIT_DEFAULT = _RATE_LIMIT_SETTINGS.default
RATE_LIMIT_STORAGE = _RATE_LIMIT_SETTINGS.storage
RATE_LIMIT_TRUST_PROXY = _RATE_LIMIT_SETTINGS.trust_proxy


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate-limit bucketing.

    By default we trust only the direct TCP peer (safe anywhere). When
    RATE_LIMIT_TRUST_PROXY=true, we honour the left-most entry in
    X-Forwarded-For — only enable this when the server sits behind a proxy
    that you control and that strips client-supplied XFF headers, otherwise
    clients can spoof their IP and evade rate limits.
    """
    if RATE_LIMIT_TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Left-most is the original client per RFC convention.
            return xff.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=_client_ip,
    default_limits=[RATE_LIMIT_DEFAULT] if RATE_LIMIT_ENABLED else [],
    storage_uri=RATE_LIMIT_STORAGE,
    enabled=RATE_LIMIT_ENABLED,
)

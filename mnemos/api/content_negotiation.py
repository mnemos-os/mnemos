"""Accept-header content negotiation for memory read paths.

The roadmap entry "Read-path routing on Accept headers" surfaces the
existing prose-vs-dense narrate dispatch through HTTP content
negotiation in addition to the explicit ``?format=`` query parameter
already exposed by ``GET /v1/memories/{id}/narrate``.

Two recognised non-default media types:

  * ``text/plain``                 → prose narration (APOLLO dense
                                     expanded to human-readable text;
                                     ARTEMIS prose passes through;
                                     no-variant falls back to raw
                                     memory content).
  * ``application/x-apollo-dense`` → raw winning-variant content
                                     (APOLLO dense form verbatim, or
                                     raw memory content when no
                                     variant exists).

Anything else (``application/json``, ``*/*``, missing header) preserves
the existing JSON ``MemoryItem`` response shape — content negotiation
NEVER turns an OK request into a 406. The non-default media types are
opt-in opportunistic affordances.
"""
from __future__ import annotations

from typing import Optional


# Mapping from recognised request media types to narrate format
# identifiers. Keys are lower-cased; callers normalise.
_RECOGNISED: dict[str, str] = {
    "text/plain": "prose",
    "application/x-apollo-dense": "dense",
}

# Media types where the caller is asking for the existing JSON
# representation. ``*/*`` is in here because RFC 7231 specifies it
# matches any representation; we treat the JSON response as the
# default representation.
_JSON_DEFAULT: frozenset[str] = frozenset(
    {"application/json", "*/*", "application/*"}
)


def _parse_accept(accept: str) -> list[tuple[str, float]]:
    """Parse an Accept header into [(media_type, q), ...] sorted by q desc.

    Lenient parser — malformed q-values default to 1.0, missing q
    parameter defaults to 1.0 (RFC 7231 §5.3.1). Unknown extension
    parameters are ignored.

    The sort is stable by original position within the same q value
    so that callers that list the more-specific type first get
    deterministic tie-breaking even when q is equal.
    """
    if not accept:
        # No Accept → treat as ``*/*`` (default representation).
        return [("*/*", 1.0)]
    parsed: list[tuple[int, str, float]] = []
    for index, raw_part in enumerate(accept.split(",")):
        part = raw_part.strip()
        if not part:
            continue
        if ";" in part:
            mt, *params = part.split(";")
            mt = mt.strip().lower()
            q = 1.0
            for param in params:
                param = param.strip()
                if param.startswith("q="):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        q = 1.0
                    # Clamp to RFC's [0, 1] range; values outside are
                    # malformed but we lean toward "still
                    # acceptable" rather than rejecting.
                    if q < 0.0:
                        q = 0.0
                    elif q > 1.0:
                        q = 1.0
                    break
        else:
            mt = part.lower()
            q = 1.0
        if mt:
            parsed.append((index, mt, q))
    parsed.sort(key=lambda entry: (-entry[2], entry[0]))
    return [(mt, q) for (_, mt, q) in parsed]


def negotiate_narrate_format(accept: str) -> Optional[str]:
    """Return the narrate format the caller prefers, or None for JSON.

    Returns ``"prose"`` when ``text/plain`` is the highest-q acceptable
    type, ``"dense"`` for ``application/x-apollo-dense``, ``None`` for
    everything else (including missing or default-JSON Accept values).

    Q=0 entries are treated as explicit refusals and skipped. If the
    caller refuses every type we know about and never expresses a
    preference, we fall back to JSON — the goal is to never surprise
    legacy clients with a 406.
    """
    parsed = _parse_accept(accept)
    for mt, q in parsed:
        if q == 0.0:
            continue
        if mt in _RECOGNISED:
            return _RECOGNISED[mt]
        if mt in _JSON_DEFAULT:
            return None
    return None


__all__ = ["negotiate_narrate_format"]

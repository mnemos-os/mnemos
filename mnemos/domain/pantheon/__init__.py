"""PANTHEON unified LLM facade domain package."""

from mnemos.core.extras import require_extra

require_extra("pantheon")

from mnemos.domain.pantheon.catalog import list_models, models_response
from mnemos.domain.pantheon.router import RouteDecision, explain_route, route_model

__all__ = [
    "RouteDecision",
    "explain_route",
    "list_models",
    "models_response",
    "route_model",
]

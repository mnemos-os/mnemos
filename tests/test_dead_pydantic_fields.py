"""Slice #179: Pydantic request fields that are declared but never
consumed by their route handler must not exist.

Builds on #178 (MemoryUpdateRequest.quality_rating). Both #178 and
#179 caught real silent doc-vs-behavior gaps where clients setting
a field expected behavior that never happened.

This test pins specific removals AND scans request models for
fields not consumed by their bound route handler. The scan is
**handler-scoped** (uses ``inspect.getsource`` on each route
function whose annotated parameter is the model) rather than a
global routes-blob — the global scan in round-1 produced
false-negatives via field-name collisions across handlers (e.g.
``RehydrationRequest.subcategory`` passed because ``request.
subcategory`` appeared in the unrelated search handler).
"""
from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path


def _request_models_with_fields() -> dict[str, list[str]]:
    """Parse mnemos/domain/models.py and yield {ModelName: [fields]}
    for every BaseModel subclass whose name ends with 'Request'."""
    models_src = Path(__file__).resolve().parents[1].joinpath(
        "mnemos/domain/models.py"
    ).read_text()
    tree = ast.parse(models_src)

    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Request"):
            fields = []
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    fields.append(stmt.target.id)
            if fields:
                out[node.name] = fields
    return out


def _handlers_consuming(model_name: str) -> list:
    """Find every route handler function whose annotated parameter
    references ``model_name``. Returns the function objects."""
    import mnemos.api.routes as routes_pkg

    handlers: list = []
    base_path = Path(routes_pkg.__file__).parent
    for path in base_path.glob("*.py"):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        module_name = f"mnemos.api.routes.{path.stem}"
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception:
            continue
        for name in dir(module):
            obj = getattr(module, name)
            if not callable(obj) or not getattr(obj, "__module__", None):
                continue
            if obj.__module__ != module_name:
                continue
            try:
                sig = inspect.signature(obj)
            except (TypeError, ValueError):
                continue
            for param in sig.parameters.values():
                annotation_str = str(param.annotation)
                if model_name in annotation_str:
                    handlers.append(obj)
                    break
    return handlers


# Fields known to be consumed indirectly (via model_dump,
# **body.dict(), or other dynamic forwarding). Add to this list
# rather than removing the test if a new field shows up that the
# scanner can't see.
_KNOWN_INDIRECT_REFERENCES: set[tuple[str, str]] = {
    # FederationPeerCreateRequest.* fields are forwarded directly
    # via `**model_dump(...)` to the persistence layer.
    ("FederationPeerCreateRequest", "name"),
    ("FederationPeerCreateRequest", "base_url"),
    ("FederationPeerCreateRequest", "auth_token"),
    ("FederationPeerCreateRequest", "namespace_filter"),
    ("FederationPeerCreateRequest", "category_filter"),
    ("FederationPeerCreateRequest", "enabled"),
    ("FederationPeerCreateRequest", "sync_interval_secs"),
    ("FederationPeerCreateRequest", "compat_mode"),
    # FederationPeerUpdateRequest is round-tripped via model_dump.
    ("FederationPeerUpdateRequest", "base_url"),
    ("FederationPeerUpdateRequest", "auth_token"),
    ("FederationPeerUpdateRequest", "namespace_filter"),
    ("FederationPeerUpdateRequest", "category_filter"),
    ("FederationPeerUpdateRequest", "enabled"),
    ("FederationPeerUpdateRequest", "sync_interval_secs"),
    ("FederationPeerUpdateRequest", "compat_mode"),
    # OAuthProvider* fields handled via the same pattern.
    ("OAuthProviderCreateRequest", "name"),
    ("OAuthProviderCreateRequest", "display_name"),
    ("OAuthProviderCreateRequest", "kind"),
    ("OAuthProviderCreateRequest", "issuer_url"),
    ("OAuthProviderCreateRequest", "client_id"),
    ("OAuthProviderCreateRequest", "client_secret"),
    ("OAuthProviderCreateRequest", "scope"),
    ("OAuthProviderCreateRequest", "authorize_url"),
    ("OAuthProviderCreateRequest", "token_url"),
    ("OAuthProviderCreateRequest", "userinfo_url"),
    ("OAuthProviderCreateRequest", "enabled"),
    ("OAuthProviderUpdateRequest", "display_name"),
    ("OAuthProviderUpdateRequest", "issuer_url"),
    ("OAuthProviderUpdateRequest", "client_id"),
    ("OAuthProviderUpdateRequest", "client_secret"),
    ("OAuthProviderUpdateRequest", "scope"),
    ("OAuthProviderUpdateRequest", "authorize_url"),
    ("OAuthProviderUpdateRequest", "token_url"),
    ("OAuthProviderUpdateRequest", "userinfo_url"),
    ("OAuthProviderUpdateRequest", "enabled"),
    # DeletionRequestCreate is consumed via model_dump in the worker
    # write-path, not directly in the route.
    ("DeletionRequestCreate", "target_user_id"),
    ("DeletionRequestCreate", "target_namespace"),
    ("DeletionRequestCreate", "reason"),
    ("DeletionRequestCreate", "notes"),
}


def test_consultation_request_does_not_declare_context():
    """#179 round-1: ConsultationRequest.context was declared but
    never read by consult_graeae. Removed."""
    from mnemos.domain.models import ConsultationRequest

    fields = typing.get_type_hints(ConsultationRequest)
    assert "context" not in fields, (
        "ConsultationRequest.context was re-introduced. Either wire "
        "it through to consult_graeae's GRAEAEEngine call or keep "
        "it removed."
    )


def test_session_history_request_class_does_not_exist():
    """#179 round-1: SessionHistoryRequest was a fully dead model
    (route uses Query() params directly). Removed."""
    import mnemos.domain.models as models_mod

    assert not hasattr(models_mod, "SessionHistoryRequest"), (
        "SessionHistoryRequest was re-introduced. The "
        "/sessions/{id}/history route accepts limit and offset as "
        "Query() parameters, not via a request body model — if you "
        "need a request model, also wire the route to consume it."
    )


def test_provider_list_response_class_does_not_exist():
    """#180: ProviderListResponse was a dead model — only
    OAuthProviderListResponse is used by any route."""
    import mnemos.domain.models as models_mod

    assert not hasattr(models_mod, "ProviderListResponse"), (
        "ProviderListResponse was re-introduced. No route uses it; "
        "the live one is OAuthProviderListResponse for the OAuth "
        "provider listing endpoint."
    )


def test_rehydration_request_does_not_declare_subcategory():
    """#179 round-2: RehydrationRequest.subcategory was declared but
    never read by rehydrate_memories. Caught by the handler-scoped
    scan that the round-1 global-blob scanner missed."""
    from mnemos.domain.models import RehydrationRequest

    fields = typing.get_type_hints(RehydrationRequest)
    assert "subcategory" not in fields, (
        "RehydrationRequest.subcategory was re-introduced. Either "
        "wire it through to rehydrate_memories' SQL filter or keep "
        "it removed."
    )


def test_no_silently_unread_request_fields():
    """Handler-scoped scan: every field on a *Request model in
    mnemos/domain/models.py should be referenced in at least one
    route handler that consumes the model. Fields consumed
    indirectly (via model_dump or **body.dict()) are listed in
    `_KNOWN_INDIRECT_REFERENCES`.

    Round-2 fix: the scan walks each model -> its bound handlers
    individually rather than a global routes-blob. The global scan
    in round-1 produced false-negatives via field-name collisions
    (e.g. RehydrationRequest.subcategory passed because
    ``request.subcategory`` appeared in the unrelated search
    handler).
    """
    request_models = _request_models_with_fields()

    candidates: list[str] = []
    for model, fields in request_models.items():
        handlers = _handlers_consuming(model)
        if not handlers:
            # Model isn't used by any route handler. Skip — that's
            # a different-shape gap (dead model) handled by
            # `test_session_history_request_class_does_not_exist`
            # for known cases.
            continue
        # Concatenate handler sources so a field consumed in any of
        # the handlers passes.
        handler_src = "\n".join(inspect.getsource(h) for h in handlers)
        for field in fields:
            patterns = [
                f"request.{field}",
                f"req.{field}",
                f"body.{field}",
                f"r.{field}",
                f"_request.{field}",
            ]
            if any(p in handler_src for p in patterns):
                continue
            if (model, field) in _KNOWN_INDIRECT_REFERENCES:
                continue
            candidates.append(f"{model}.{field}")

    assert not candidates, (
        f"{len(candidates)} request-model field(s) declared but not "
        f"referenced in any handler that consumes the model:\n  "
        + "\n  ".join(candidates)
        + "\n\nIf the field is consumed indirectly (model_dump, "
        "**body.dict()), add (Model, field) to "
        "_KNOWN_INDIRECT_REFERENCES in this test file. Otherwise "
        "remove the field — clients setting it expect behavior "
        "that never happens (#178/#179 pattern)."
    )

"""Slice #171: EntityCreateRequest.entity_type is Pydantic Literal.

Continues the #168/#169/#170 pattern: replaced the route handler's
`if req.entity_type not in ENTITY_TYPES` check with a Literal[...]
on the model. Pydantic auto-422s on invalid values at parse time.

The Literal values are hard-coded in the model rather than computed
from ``ENTITY_TYPES`` (PEP 646 ``Literal[*tuple]`` isn't supported
in stable Python). The parity test below asserts the two stay in
sync — a future addition to ENTITY_TYPES that doesn't update the
Literal will fail this test.
"""
from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from mnemos.api.routes.entities import EntityCreateRequest
from mnemos.domain.memory_categorization.constants import ENTITY_TYPES


def _literal_values_for(model: type, field: str) -> tuple[str, ...]:
    """Pull the Literal[...] values out of a model field's annotation."""
    annotations = typing.get_type_hints(model)
    annotation = annotations[field]
    args = typing.get_args(annotation)
    if not args:
        raise AssertionError(
            f"{model.__name__}.{field} is not a Literal type: {annotation!r}"
        )
    return args


def test_entity_type_literal_matches_entity_types_list():
    """The Literal values in EntityCreateRequest must mirror the
    ENTITY_TYPES constant. If you add a new entity type, update both.
    """
    literal_values = set(_literal_values_for(EntityCreateRequest, "entity_type"))
    assert literal_values == set(ENTITY_TYPES), (
        f"EntityCreateRequest.entity_type Literal drifted from "
        f"ENTITY_TYPES: literal={sorted(literal_values)}, "
        f"constant={sorted(ENTITY_TYPES)}"
    )


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_entity_create_accepts_each_documented_type(entity_type):
    """Every value in ENTITY_TYPES must construct without error."""
    req = EntityCreateRequest(entity_type=entity_type, name="x")
    assert req.entity_type == entity_type


@pytest.mark.parametrize(
    "invalid",
    [
        "PERSON",          # uppercase
        "person ",         # trailing whitespace
        "person\n",        # trailing newline
        "individual",      # not in the list
        "",                # empty
        "person,project",  # comma-injected
    ],
)
def test_entity_create_rejects_invalid_type(invalid):
    """#171: invalid entity_type values must 422 at the model level
    (replaces the runtime handler check that previously emitted
    HTTPException(400))."""
    with pytest.raises(ValidationError) as exc_info:
        EntityCreateRequest(entity_type=invalid, name="x")
    assert "entity_type" in str(exc_info.value).lower()

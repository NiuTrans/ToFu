"""Reusable repository-free orchestration authoring operations."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from lib.orchestration._layout import layout_definition as _layout_definition
from lib.orchestration.authoring_contract import build_builtin_definition
from lib.orchestration.authoring_results import (
    AuthoringBuiltinResult,
    AuthoringPlanResult,
)
from lib.orchestration.definition_inspection import (
    inspect_definition,
    prepare_definition,
)
from lib.orchestration.errors import AuthoringServiceError
from lib.orchestration.service_call import orchestration_dependency_call


Composer = Callable[..., Any]


def _project_composed_definition(result: Any) -> dict:
    """Apply canonical inspection after the dependency boundary returns."""
    projected = copy.deepcopy(result)
    definition = projected.get('definition') \
        if isinstance(projected, dict) else None
    if isinstance(definition, dict):
        prepared = prepare_definition(definition)
        inspection = prepared.inspection
        if prepared.definition is not None:
            projected['definition'] = prepared.definition
        projected['inspection'] = inspection
        projected['validation'] = inspection
    return projected


def _compose_with(
    composer: Composer,
    requirement: str,
    *,
    current: dict | None = None,
    history: list[dict] | None = None,
) -> dict:
    current_snapshot = copy.deepcopy(current)
    history_snapshot = copy.deepcopy(history)
    result = orchestration_dependency_call(
        lambda: composer(
            requirement,
            current=current_snapshot,
            history=history_snapshot,
        ),
        error_type=AuthoringServiceError,
        message='failed to compose orchestration definition',
    )
    return _project_composed_definition(result)


def compose_definition(requirement: str, *, current: dict | None = None,
                       history: list[dict] | None = None) -> dict:
    """Compose or revise a graph through the shared authoring seam."""
    from lib.orchestration_composer import compose

    return _compose_with(
        compose, requirement, current=current, history=history)


def layout_authoring_definition(definition: dict) -> dict:
    """Return a detached, canonically laid-out authoring snapshot."""
    arranged = copy.deepcopy(definition)
    _layout_definition(arranged)
    return arranged


def inspect_builtin_definition(name: str) -> AuthoringBuiltinResult:
    """Resolve and inspect a built-in through one application operation."""
    definition = build_builtin_definition(name)
    return AuthoringBuiltinResult(
        definition=definition,
        inspection=inspect_definition(definition)
        if definition is not None else None,
    )


def plan_authoring_definition(definition: dict) -> AuthoringPlanResult:
    """Compile one dry-run plan without leaking inspection internals to HTTP."""
    inspection = inspect_definition(definition, include_plan=True)
    plan = inspection.pop('plan')
    assert isinstance(plan, dict)
    return AuthoringPlanResult(plan=plan, inspection=inspection)


__all__ = [
    'Composer', 'compose_definition', 'inspect_builtin_definition',
    'layout_authoring_definition', 'plan_authoring_definition',
]

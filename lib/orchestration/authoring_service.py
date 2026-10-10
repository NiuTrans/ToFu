"""Unified application service for repository-free orchestration authoring."""

from __future__ import annotations

from lib.orchestration.authoring_contract import (
    authoring_contract,
    build_builtin_definition,
)
from lib.orchestration.authoring_operations import (
    Composer,
    _compose_with,
    compose_definition,
    inspect_builtin_definition,
    layout_authoring_definition,
    plan_authoring_definition,
)
from lib.orchestration.authoring_results import (
    AuthoringBuiltinResult,
    AuthoringPlanResult,
)
from lib.orchestration.definition_inspection import inspect_definition
from lib.orchestration.errors import AuthoringServiceError


class OrchestrationAuthoringService:
    """Uniform application interface for all repository-free authoring."""

    def __init__(self, *, composer: Composer | None = None):
        self._composer = composer

    def inspect(self, definition: dict) -> dict:
        return inspect_definition(definition)

    def build_builtin(self, name: str, **options: object) -> dict | None:
        return build_builtin_definition(name, **options)

    def compose(self, requirement: str, *, current: dict | None = None,
                history: list[dict] | None = None) -> dict:
        if self._composer is not None:
            return _compose_with(
                self._composer,
                requirement,
                current=current,
                history=history,
            )
        return compose_definition(
            requirement, current=current, history=history,
        )

    def builtin(self, name: str) -> dict | None:
        return self.build_builtin(name)

    def builtin_inspection(self, name: str) -> AuthoringBuiltinResult:
        return inspect_builtin_definition(name)

    def contract(self) -> dict:
        return authoring_contract()

    def layout(self, definition: dict) -> dict:
        return layout_authoring_definition(definition)

    def plan(self, definition: dict) -> AuthoringPlanResult:
        return plan_authoring_definition(definition)


__all__ = [
    'AuthoringBuiltinResult',
    'AuthoringPlanResult',
    'AuthoringServiceError',
    'Composer',
    'OrchestrationAuthoringService',
    'compose_definition',
    'inspect_builtin_definition',
    'layout_authoring_definition',
    'plan_authoring_definition',
]

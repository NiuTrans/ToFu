"""Stable application-error contract for orchestration service boundaries."""

from __future__ import annotations


class OrchestrationServiceError(RuntimeError):
    """Base class for expected failures below an application-service port."""

    def public_fields(self) -> dict[str, object]:
        """Return safe top-level fields preserved by the HTTP boundary."""
        return {}


class DefinitionServiceError(OrchestrationServiceError):
    """The definition repository is unavailable."""


class AuthoringServiceError(OrchestrationServiceError):
    """An external authoring dependency failed below the service boundary."""


class RunServiceError(OrchestrationServiceError):
    """A durable-run operation failed below the application boundary."""


class HumanGateServiceError(OrchestrationServiceError):
    """A shared human-gate registry failed below the application boundary."""


class RuntimeStartError(OrchestrationServiceError):
    """A canonical run could not be handed to its background worker."""

    def __init__(self, message: str, *, run_id: str = ''):
        super().__init__(message)
        self.run_id = str(run_id or '')

    def public_fields(self) -> dict[str, object]:
        """Expose a durable row that survived a failed worker handoff."""
        if not self.run_id:
            return {}
        from lib.orchestration.runtime_wire_contracts import (
            project_runtime_start,
        )
        return {'start': project_runtime_start(self.run_id, 'durable')}


class RuntimeMutationError(OrchestrationServiceError):
    """A transient runtime mutation dependency failed."""


__all__ = [
    'OrchestrationServiceError',
    'DefinitionServiceError',
    'AuthoringServiceError',
    'RunServiceError',
    'HumanGateServiceError',
    'RuntimeStartError',
    'RuntimeMutationError',
]

"""One HTTP projection for expected orchestration application failures."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from lib.api_response import api_internal_error
from lib.log import get_logger
from lib.orchestration.errors import OrchestrationServiceError


logger = get_logger(__name__)

_APPLICATION_SERVICE_SOURCE = 'orchestration:application-service'

_ResultT = TypeVar('_ResultT')
_FailureT = TypeVar('_FailureT')
_ResponseT = TypeVar('_ResponseT')


def orchestration_service_call(
    context: str,
    operation: Callable[[], _ResultT],
    *,
    project_error: Callable[..., _FailureT] | None = None,
) -> tuple[_ResultT | None, _FailureT | None]:
    """Execute one service operation and map only its declared error family.

    Unexpected exceptions deliberately escape so programmer defects are not
    mislabeled as routine infrastructure failures.
    """
    projector = project_error or api_internal_error
    try:
        return operation(), None
    except OrchestrationServiceError as error:
        logger.debug('[OrchestrationHTTP] %s: %s', context, error)
        return None, projector(
            error,
            context=context,
            source=_APPLICATION_SERVICE_SOURCE,
            **error.public_fields(),
        )


def orchestration_service_response(
    context: str,
    operation: Callable[[], _ResultT],
    project_result: Callable[[_ResultT], _ResponseT],
    *,
    project_error: Callable[..., _FailureT] | None = None,
) -> _ResponseT | _FailureT:
    """Execute and project one service operation through the shared boundary.

    Result projection deliberately happens after ``orchestration_service_call``
    so adapter defects are never mislabeled as expected service failures.
    """
    result, failure = orchestration_service_call(
        context,
        operation,
        project_error=project_error,
    )
    if failure is not None:
        return failure
    return project_result(cast(_ResultT, result))


__all__ = ['orchestration_service_call', 'orchestration_service_response']

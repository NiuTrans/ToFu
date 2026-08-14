"""Shared dependency-call boundary for orchestration application services."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from lib.orchestration.errors import OrchestrationServiceError


_ResultT = TypeVar('_ResultT')
_ErrorT = TypeVar('_ErrorT', bound=OrchestrationServiceError)


def orchestration_dependency_call(
    callback: Callable[[], _ResultT],
    *,
    error_type: type[_ErrorT],
    message: str,
) -> _ResultT:
    """Translate dependency failures without hiding service/programmer bugs.

    Existing application errors retain their identity. Only the injected
    dependency invocation belongs inside this boundary; validation and result
    projection must stay outside so their defects are never mislabeled as
    repository or persistence outages.
    """
    try:
        return callback()
    except OrchestrationServiceError:
        raise
    except Exception as error:
        raise error_type(message) from error


__all__ = ['orchestration_dependency_call']

"""Shared application-service dependency boundary contracts."""

from __future__ import annotations

import pytest

from lib.orchestration.errors import DefinitionServiceError, RunServiceError
from lib.orchestration.service_call import orchestration_dependency_call


pytestmark = pytest.mark.unit


def test_dependency_failure_is_typed_once_with_original_cause():
    failure = OSError('offline')
    with pytest.raises(RunServiceError, match='runs unavailable') as captured:
        orchestration_dependency_call(
            lambda: (_ for _ in ()).throw(failure),
            error_type=RunServiceError,
            message='runs unavailable',
        )
    assert captured.value.__cause__ is failure


def test_existing_service_error_is_not_double_wrapped():
    failure = DefinitionServiceError('definitions unavailable')
    with pytest.raises(DefinitionServiceError) as captured:
        orchestration_dependency_call(
            lambda: (_ for _ in ()).throw(failure),
            error_type=RunServiceError,
            message='wrong wrapper',
        )
    assert captured.value is failure

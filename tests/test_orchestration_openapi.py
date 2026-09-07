"""Tests for shared orchestration OpenAPI response composition."""

from __future__ import annotations

import pytest

from routes.api_v1.orchestration_openapi import (
    orchestration_api_responses,
    orchestration_error_response,
    orchestration_json_response,
)


pytestmark = pytest.mark.unit


def test_orchestration_json_response_owns_the_shared_envelope_shape():
    schema = {'type': 'object'}
    assert orchestration_json_response('Projected result', schema) == {
        'description': 'Projected result',
        'content': {'application/json': {'schema': schema}},
    }


def test_orchestration_response_composition_is_standard_and_detached():
    success = {'200': {'description': 'OK', 'content': {'sentinel': []}}}
    responses = orchestration_api_responses(success, 400, 401, 403, 500)

    assert set(responses) == {'200', '400', '401', '403', '500'}
    for status in ('400', '401', '403', '500'):
        assert responses[status]['content']['application/json']['schema'] == {
            '$ref': '#/components/schemas/ErrorEnvelope',
        }
    responses['200']['content']['sentinel'].append('mutation')
    assert success['200']['content']['sentinel'] == []


def test_orchestration_error_response_rejects_unowned_statuses():
    with pytest.raises(ValueError, match='unsupported orchestration'):
        orchestration_error_response(418)

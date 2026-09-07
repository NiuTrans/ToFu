"""Contract tests for the shared definition mutation service adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import routes.api_v1.orchestration_definition_service_http as service_http


pytestmark = pytest.mark.unit


def test_definition_write_service_uses_endpoint_operation_and_success_hook(
    monkeypatch,
):
    result = SimpleNamespace(entry={'id': 'flow-1'})
    projected = []
    successes = []

    monkeypatch.setattr(
        service_http,
        'orchestration_service_response',
        lambda context, operation, projector: (
            projected.append(context) or projector(operation())
        ),
    )
    monkeypatch.setattr(
        service_http,
        'definition_write_response',
        lambda value, **fields: (
            projected.append((value, fields)) or ('response', value.entry)
        ),
    )

    response = service_http.orchestration_definition_write_service_response(
        'api.test.create',
        lambda: result,
        endpoint='definition-create',
        on_success=lambda entry: successes.append(entry),
    )

    assert response == 'response'
    assert projected == [
        'api.test.create',
        (result, {'operation': 'create', 'expected_updated_at': None}),
    ]
    assert successes == [result.entry]


def test_definition_delete_service_only_runs_hook_after_delete(monkeypatch):
    projected = []
    successes = []
    results = [
        SimpleNamespace(deleted=False),
        SimpleNamespace(deleted=True),
    ]

    monkeypatch.setattr(
        service_http,
        'orchestration_service_response',
        lambda context, operation, projector: projector(operation()),
    )
    monkeypatch.setattr(
        service_http,
        'definition_delete_response',
        lambda value, **fields: (
            projected.append((value, fields)) or ('response', value.deleted)
        ),
    )

    for result in results:
        assert service_http.orchestration_definition_delete_service_response(
            'api.test.delete',
            lambda result=result: result,
            endpoint='definition-delete',
            expected_updated_at=42,
            on_success=lambda: successes.append('deleted'),
        ) == 'response'

    assert projected == [
        (results[0], {'operation': 'delete', 'expected_updated_at': 42}),
        (results[1], {'operation': 'delete', 'expected_updated_at': 42}),
    ]
    assert successes == ['deleted']


@pytest.mark.parametrize(('adapter', 'endpoint', 'adapter_kwargs'), [
    (service_http.orchestration_definition_write_service_response,
     'definition-read', {}),
    (service_http.orchestration_definition_delete_service_response,
     'definition-create', {'expected_updated_at': 42}),
])
def test_definition_service_rejects_endpoint_with_wrong_write_semantics(
    adapter,
    endpoint,
    adapter_kwargs,
):
    with pytest.raises(ValueError, match='is not a definition'):
        adapter(
            'api.test.invalid', lambda: None,
            endpoint=endpoint, **adapter_kwargs,
        )

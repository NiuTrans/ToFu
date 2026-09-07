"""Shared orchestration application-error to HTTP projection contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration.errors import (
    AuthoringServiceError,
    DefinitionServiceError,
    HumanGateServiceError,
    OrchestrationServiceError,
    RunServiceError,
    RuntimeMutationError,
    RuntimeStartError,
)
import routes.api_v1.orchestration_service_http as service_http
from routes.api_v1.orchestration_service_http import (
    orchestration_service_call,
    orchestration_service_response,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('failure', 'public_fields'), [
    (DefinitionServiceError('definitions offline'), {}),
    (AuthoringServiceError('composer offline'), {}),
    (RunServiceError('runs offline'), {}),
    (HumanGateServiceError('gate registry offline'), {}),
    (RuntimeMutationError('runtime registry offline'), {}),
    (RuntimeStartError('worker offline', run_id='run-1'), {
        'start': {
            'format': 'tofu.orchestration.runtime-start/v1',
            'kind': 'durable',
            'id': 'run-1',
        },
    }),
])
def test_expected_service_errors_share_one_http_projection(
    failure, public_fields,
):
    seen = []

    result, response = orchestration_service_call(
        'api.test.operation',
        lambda: (_ for _ in ()).throw(failure),
        project_error=lambda error, **fields: (
            seen.append((error, fields)) or {'status': 500}
        ),
    )

    assert result is None
    assert response == {'status': 500}
    assert seen == [(failure, {
        'context': 'api.test.operation',
        'source': 'orchestration:application-service',
        **public_fields,
    })]
    assert isinstance(failure, OrchestrationServiceError)


def test_programmer_errors_still_escape_the_shared_boundary():
    with pytest.raises(RuntimeError, match='programmer bug'):
        orchestration_service_call(
            'api.test.operation',
            lambda: (_ for _ in ()).throw(RuntimeError('programmer bug')),
            project_error=lambda *_args, **_kwargs: None,
        )


def test_default_internal_error_projection_is_owned_by_shared_boundary(
    monkeypatch,
):
    failure = DefinitionServiceError('definitions offline')
    projected = object()
    seen = []
    monkeypatch.setattr(
        service_http, 'api_internal_error',
        lambda error, **fields: seen.append((error, fields)) or projected,
    )

    result, response = orchestration_service_call(
        'api.test.default',
        lambda: (_ for _ in ()).throw(failure),
    )

    assert result is None
    assert response is projected
    assert seen == [(failure, {
        'context': 'api.test.default',
        'source': 'orchestration:application-service',
    })]


def test_service_response_projects_success_without_repeating_tuple_branch():
    seen = []

    response = orchestration_service_response(
        'api.test.success',
        lambda: {'id': 'run-1'},
        lambda result: seen.append(result) or {'ok': True, **result},
    )

    assert response == {'ok': True, 'id': 'run-1'}
    assert seen == [{'id': 'run-1'}]


def test_service_response_skips_success_projector_for_expected_failure():
    failure = RunServiceError('runs offline')
    projected = object()
    seen = []

    response = orchestration_service_response(
        'api.test.failure',
        lambda: (_ for _ in ()).throw(failure),
        lambda result: seen.append(result),
        project_error=lambda *_args, **_kwargs: projected,
    )

    assert response is projected
    assert seen == []


def test_service_response_does_not_mask_projector_defects():
    with pytest.raises(RuntimeError, match='adapter bug'):
        orchestration_service_response(
            'api.test.projector',
            lambda: 'value',
            lambda _result: (_ for _ in ()).throw(
                RuntimeError('adapter bug')),
        )


def test_http_adapters_depend_on_error_contract_not_service_implementations():
    service_http = (ROOT / 'routes/api_v1/'
                    'orchestration_service_http.py').read_text()
    errors = (ROOT / 'lib/orchestration/errors.py').read_text()
    for name in (
        'orchestration_authoring_http.py',
        'orchestration_definition_http.py',
        'orchestration_mutation_http.py',
        'orchestration_mutation_service_http.py',
        'orchestration_definition_service_http.py',
        'orchestration_task_http.py',
        'orchestration_run_http.py',
        'orchestration_runtime_start_http.py',
    ):
        source = (ROOT / 'routes/api_v1' / name).read_text()
        assert 'def authoring_service_call(' not in source
        assert 'def definition_service_call(' not in source
        assert 'def mutation_service_call(' not in source
        assert 'def durable_run_service_call(' not in source
        assert 'def runtime_start_service_call(' not in source
        assert 'from lib.orchestration.run_service import' not in source
        assert 'from lib.orchestration.runtime_start_service import' not in source
        assert 'DefinitionServiceError' not in source

    assert 'except OrchestrationServiceError as error:' in service_http
    assert 'operation: Callable[[], _ResultT]' in service_http
    assert 'Callable[[], Any]' not in service_http
    assert 'class OrchestrationServiceError(RuntimeError)' in errors

    route_calls = {
        'orchestration_definition_routes.py': 2,
        'orchestration_authoring_routes.py': 5,
        'orchestration_runtime_routes.py': 1,
        'orchestration_runtime_start_http.py': 1,
        'orchestration_task_routes.py': 3,
    }
    for name, call_count in route_calls.items():
        source = (ROOT / 'routes/api_v1' / name).read_text()
        assert ('from .orchestration_service_http import '
                'orchestration_service_response') in source
        assert source.count('orchestration_service_response(') == call_count
        assert 'orchestration_service_call' not in source
        for legacy_name in (
            'authoring_service_call(', 'definition_service_call(',
            'mutation_service_call(', 'durable_run_service_call(',
            'runtime_start_service_call(',
        ):
            assert legacy_name not in source
    mutation_routes = (ROOT / 'routes/api_v1/'
                       'orchestration_mutation_routes.py').read_text()
    mutation_service_http = (ROOT / 'routes/api_v1/'
                             'orchestration_mutation_service_http.py').read_text()
    assert mutation_routes.count(
        'orchestration_mutation_service_response(') == 5
    assert 'orchestration_service_response(' not in mutation_routes
    assert mutation_service_http.count('orchestration_service_response(') == 1
    definition_routes = (ROOT / 'routes/api_v1/'
                         'orchestration_definition_routes.py').read_text()
    definition_service_http = (ROOT / 'routes/api_v1/'
        'orchestration_definition_service_http.py').read_text()
    assert definition_routes.count(
        'orchestration_definition_write_service_response(') == 2
    assert definition_routes.count(
        'orchestration_definition_delete_service_response(') == 1
    assert definition_service_http.count(
        'orchestration_service_response(') == 2

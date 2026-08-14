"""Contract tests for shared orchestration mutation OpenAPI projections."""

from __future__ import annotations

import pytest

from lib.orchestration_mutation import (
    MUTATION_ACCEPTED,
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_CONFLICT,
    MUTATION_TERMINAL,
    mutation_contract,
)
from lib.orchestration.mutation_endpoint_contract import (
    mutation_endpoint_compatibility,
    mutation_endpoint_contracts,
)
from routes.api_v1.orchestration_mutation_openapi import (
    mutation_payload_schema,
    mutation_response_schema,
    mutation_route_response_registry,
    mutation_route_responses,
)


pytestmark = pytest.mark.unit


def test_mutation_payload_schema_derives_versioned_policy_fields():
    import lib.orchestration_mutation as mutation_module
    import routes.api_v1.orchestration_mutation_openapi as openapi_module

    contract = mutation_contract()
    schema = mutation_payload_schema()

    assert schema['properties']['format']['enum'] == [contract['format']]
    assert schema['properties']['action']['enum'] == contract['actions']
    assert schema['properties']['reason']['enum'] == contract['reasons']
    assert schema['required'] == [
        spec['name'] for spec in contract['payloadFields'].values()
    ]
    assert schema['properties'] == {
        spec['name']: schema['properties'][spec['name']]
        for spec in contract['payloadFields'].values()
    }
    assert contract['httpStatusByReason'][MUTATION_ACCEPTED] == 200
    for field in (
        contract['reconcileField'],
        contract['targetExistsField'],
        contract['resourceTerminalField'],
    ):
        assert field in schema['required']
    assert contract['legacyTargetFields'] == ['run_id', 'requestId']
    assert contract['legacyStatusFields'] == ['run_status', 'status']
    assert openapi_module.mutation_payload_schema is \
        mutation_module.mutation_payload_schema
    assert 'def mutation_payload_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_mutation_response_narrows_action_reason_and_compatibility_fields():
    import lib.orchestration_mutation as mutation_module
    import routes.api_v1.orchestration_mutation_openapi as openapi_module

    schema = mutation_response_schema(
        MUTATION_ACTION_ABORT_RUN,
        [MUTATION_TERMINAL, MUTATION_CONFLICT],
        {'run_id': {'type': 'string'}},
    )
    mutation = schema['properties']['mutation']['properties']

    assert schema['properties']['ok']['const'] is False
    assert {'ok', 'mutation', 'run_id', 'error'} <= set(schema['required'])
    assert mutation['action']['enum'] == [MUTATION_ACTION_ABORT_RUN]
    assert mutation['reason']['enum'] == [MUTATION_TERMINAL, MUTATION_CONFLICT]

    accepted = mutation_response_schema(
        MUTATION_ACTION_ABORT_RUN, [MUTATION_ACCEPTED])
    assert accepted['properties']['ok']['const'] is True
    assert 'error' not in accepted['required']
    assert openapi_module.mutation_response_schema is \
        mutation_module.mutation_response_schema
    assert 'def mutation_response_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_mutation_route_registry_documents_each_canonical_http_branch():
    registry = mutation_route_response_registry()
    assert set(registry) == set(mutation_endpoint_contracts()) == {
        'human-approve', 'human-input', 'task-abort',
        'task-remove', 'run-abort',
    }
    assert set(registry['human-approve']) == {
        '200', '400', '401', '403', '404', '500',
    }
    assert set(registry['task-abort']) == {
        '200', '401', '403', '404', '409', '500',
    }
    assert set(registry['task-remove']) == {
        '200', '401', '403', '404', '409', '500',
    }
    assert 'oneOf' in registry['task-abort']['500']['content'][
        'application/json']['schema']
    with pytest.raises(ValueError, match='unknown orchestration mutation'):
        mutation_route_responses('retry')


def test_mutation_endpoint_contract_projects_legacy_fields_once():
    from lib.orchestration_mutation import OrchestrationMutationResult

    result = OrchestrationMutationResult(
        False, MUTATION_TERMINAL,
        run_status='done', action=MUTATION_ACTION_ABORT_RUN,
        target_id='run-1',
    )
    assert mutation_endpoint_compatibility('task-abort', result) == {
        'run_id': 'run-1', 'status': 'done', 'run_status': 'done',
    }
    assert mutation_endpoint_compatibility('run-abort', result) == {
        'status': 'done', 'note': 'already finished',
    }

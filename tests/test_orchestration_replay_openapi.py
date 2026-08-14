"""Contract tests for shared live/durable replay OpenAPI projections."""

from __future__ import annotations

import pytest

from lib.agent_core.task_runtime import TaskRuntime
from lib.orchestration.events import runtime_event_contract
from lib.task_replay import task_replay_contract
from routes.api_v1.orchestration_replay_openapi import (
    orchestration_live_replay_response_schema,
    orchestration_live_replay_responses,
    orchestration_replay_event_schema,
    task_replay_response_schema,
    task_replay_route_responses,
)


pytestmark = pytest.mark.unit


def test_shared_replay_schema_uses_protocol_field_and_status_registries():
    import lib.task_replay as replay_module
    import routes.api_v1.orchestration_replay_openapi as openapi_module

    contract = task_replay_contract()
    success = task_replay_response_schema()
    missing = task_replay_response_schema(missing=True)

    assert success['properties']['format']['enum'] == [contract['format']]
    assert contract['eventsField'] in success['required']
    assert contract['terminalField'] in success['required']
    assert success['required'] == contract['pageFields']
    cursor = contract['cursor']
    assert success['properties'][cursor['field']]['required'] == [
        cursor['requestedField'], cursor['nextField'], cursor['resetField'],
    ]
    assert missing['properties']['error']['enum'] == [
        contract['notFoundReason'],
    ]
    assert openapi_module.task_replay_response_schema is \
        replay_module.task_replay_response_schema
    assert 'def task_replay_response_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()

    responses = task_replay_route_responses(success, missing)
    statuses = contract['httpStatuses']
    assert set(responses) == {
        str(statuses['success']), str(statuses['notFound']),
        '401', '403', str(statuses['failure']),
    }


def test_replay_event_schema_combines_owned_registries_without_closing_type():
    import lib.task_replay as replay_module
    import routes.api_v1.orchestration_replay_openapi as openapi_module

    schema = orchestration_replay_event_schema()
    replay = task_replay_contract()
    type_schema = schema['properties'][replay['eventTypeField']]
    known = type_schema['x-knownValues']

    assert schema['required'] == replay['eventRequiredFields']
    assert schema['properties'][replay['eventSequenceField']] == {
        'type': 'integer', 'minimum': 0,
    }
    assert schema['additionalProperties'] is True
    assert schema['x-eventSchema'] == runtime_event_contract()['schema']
    assert set(runtime_event_contract()['types']) <= set(known)
    assert set(task_replay_contract()['terminalEventTypes']) <= set(known)
    assert type_schema['x-unknownValuePolicy'] == 'allow'
    assert 'enum' not in type_schema
    assert openapi_module.orchestration_replay_event_schema is \
        replay_module.task_replay_event_schema
    assert 'def orchestration_replay_event_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_live_replay_schema_covers_task_runtime_clocks_and_terminal_extras():
    import lib.task_replay as replay_module
    import routes.api_v1.orchestration_replay_openapi as openapi_module

    runtime = TaskRuntime('openapi-sample')
    task = runtime.create()
    running = runtime.poll(task['id'])
    runtime.finish(task['id'], result={'final': 'ready'})
    terminal = runtime.poll(task['id'])

    schema = orchestration_live_replay_response_schema()
    assert {'taskId', 'createdAt', 'updatedAt'} <= set(schema['required'])
    assert set(schema['required']) <= set(running)
    assert set(schema['required']) <= set(terminal)
    for field in (
        'requestId', 'finishedAt', 'result', 'error',
        'artifact_quality', 'model',
    ):
        assert field in schema['properties']
    assert openapi_module.orchestration_live_replay_response_schema is \
        replay_module.live_task_replay_response_schema
    assert 'def orchestration_live_replay_response_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_live_replay_response_registry_preserves_versioned_missing_page():
    contract = task_replay_contract()
    responses = orchestration_live_replay_responses()
    missing = responses[str(contract['httpStatuses']['notFound'])][
        'content']['application/json']['schema']

    assert missing == orchestration_live_replay_response_schema(missing=True)
    assert 'message' not in missing['required']

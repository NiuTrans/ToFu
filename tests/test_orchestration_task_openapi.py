"""Contract tests for durable-run OpenAPI projections."""

from __future__ import annotations

import pytest

from lib.orchestration.durable_run_wire_contract import (
    durable_run_contract,
    project_durable_run_snapshot,
)
from lib.orchestration.run_status import run_status_contract
from lib.orchestration.run_store_codec import row_to_run_header
from lib.task_replay import task_replay_contract
from routes.api_v1.orchestration_task_openapi import (
    durable_replay_response_schema,
    durable_run_list_response_schema,
    durable_run_read_response_schema,
    durable_run_schema,
    durable_task_route_response_registry,
    durable_task_route_responses,
)


pytestmark = pytest.mark.unit


class _Row(dict):
    pass


def _row() -> _Row:
    return _Row({
        'id': 'run-1',
        'orch_id': 'flow-1',
        'name': 'Flow',
        'status': 'running',
        'final': '',
        'error': '',
        'created_by': 'key-1',
        'created_at': 1,
        'updated_at': 2,
        'finished_at': 0,
        'definition': '{"name":"Flow","nodes":[],"edges":[]}',
        'input': 'hello',
    })


def test_durable_run_schema_uses_codec_and_contract_field_registries():
    import lib.orchestration.durable_run_wire_contract as contract_module
    import routes.api_v1.orchestration_task_openapi as openapi_module

    contract = durable_run_contract()
    listed = row_to_run_header(_row(), include_definition=False)
    detail = row_to_run_header(_row(), include_definition=True)

    assert list(listed) == contract['listFields']
    assert list(detail) == contract['readFields']
    assert durable_run_schema()['required'] == contract['listFields']
    assert durable_run_schema(detail=True)['required'] == \
        contract['readFields']
    assert durable_run_schema()['properties']['status']['enum'] == \
        run_status_contract()['statuses']
    assert contract['outcomeField'] in durable_run_schema()['properties']
    assert openapi_module.durable_run_schema is \
        contract_module.durable_run_schema
    assert 'def durable_run_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_durable_projection_rejects_partial_values_and_drops_storage_extras():
    contract = durable_run_contract()
    values = {field: field for field in contract['readFields']}
    values['storage_only'] = 'drop'

    listed = project_durable_run_snapshot(values, detail=False)
    detail = project_durable_run_snapshot(values, detail=True)

    assert list(listed) == contract['listFields']
    assert list(detail) == contract['readFields']
    assert 'storage_only' not in listed and 'storage_only' not in detail
    values.pop('status')
    with pytest.raises(KeyError, match='status'):
        project_durable_run_snapshot(values, detail=False)


def test_durable_read_envelopes_reuse_the_same_run_schemas():
    import lib.orchestration.durable_run_wire_contract as contract_module
    import routes.api_v1.orchestration_task_openapi as openapi_module

    listed = durable_run_list_response_schema()
    detail = durable_run_read_response_schema()
    envelope = durable_run_contract()['listEnvelope']

    assert listed['required'] == [
        'ok', envelope['itemsField'], envelope['pageField'],
    ]
    assert listed['properties'][envelope['itemsField']]['items'] == \
        durable_run_schema()
    assert listed['properties'][envelope['pageField']]['required'] == \
        envelope['pageFields']
    assert listed['properties'][envelope['pageField']]['properties'][
        envelope['limitField']]['maximum'] == envelope['maxLimit']
    assert detail['required'] == ['ok', 'run']
    assert detail['properties']['run'] == durable_run_schema(detail=True)
    for schema_name in (
        'durable_run_list_response_schema',
        'durable_run_read_response_schema',
        'durable_replay_response_schema',
    ):
        assert getattr(openapi_module, schema_name) is getattr(
            contract_module, schema_name)
        assert f'def {schema_name}' not in open(
            openapi_module.__file__, encoding='utf-8').read()


def test_replay_schemas_execute_format_fields_and_missing_semantics():
    contract = task_replay_contract()
    success = durable_replay_response_schema()
    missing = durable_replay_response_schema(missing=True)

    assert success['properties']['format']['enum'] == [contract['format']]
    assert contract['eventsField'] in success['required']
    assert contract['terminalField'] in success['required']
    assert contract['terminalSnapshot']['field'] in success['properties']
    assert missing['properties']['ok']['const'] is False
    assert missing['properties']['done']['const'] is True
    assert missing['properties']['error']['enum'] == [
        contract['notFoundReason'],
    ]
    assert {'error', 'message'} <= set(missing['required'])


def test_durable_task_route_response_registry_covers_transport_statuses():
    registry = durable_task_route_response_registry()
    assert set(registry) == {'list', 'read', 'replay'}
    assert set(registry['list']) == {'200', '400', '401', '403', '500'}
    assert set(registry['read']) == {'200', '401', '403', '404', '500'}
    assert set(registry['replay']) == {'200', '401', '403', '404', '500'}
    replay_404 = registry['replay']['404']['content'][
        'application/json']['schema']
    assert replay_404 == durable_replay_response_schema(missing=True)
    with pytest.raises(ValueError, match='unknown durable task operation'):
        durable_task_route_responses('resume')

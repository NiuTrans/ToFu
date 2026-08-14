"""Contract tests for shared ephemeral/durable run request preparation."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.request_parser import BadRequest
from lib.orchestration.definition_service import ResolvedDefinition
import routes.api_v1.orchestration_run_http as run_http
import routes.api_v1.orchestration_run_openapi as run_openapi
import routes.api_v1.orchestration_runtime_start_http as start_http
import routes.api_v1.orchestration_definition_request_http as request_http


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _definition(name: str = 'Shared run') -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': name,
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {'id': 'w', 'type': 'role', 'role': 'worker',
             'params': {'objective': 'Do the work'}},
            {'id': 'z', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'z'}],
    }


def test_shared_run_request_resolves_canonicalizes_and_types_inputs():
    source = _definition()
    body = {
        'definition': source,
        'input': '  proceed  ',
        'id': 'ignored-selector',
        'originId': '  flow-1  ',
    }
    seen = []

    prepared, failure = run_http.prepare_run_request(
        body,
        resolve_definition=lambda value: seen.append(value) or
        ResolvedDefinition(source, 'inline'),
    )

    assert failure is None
    assert prepared is not None
    assert seen == [body]
    assert prepared.definition == source
    assert prepared.definition is not source
    assert prepared.inspection['ok'] is True
    assert prepared.definition_source == 'inline'
    assert prepared.input_text == 'proceed'
    # Inline selection wins over a simultaneously supplied stored id, while
    # its explicit origin remains available to durable-run provenance.
    assert prepared.orchestration_id == 'flow-1'

    unlinked, failure = run_http.prepare_run_request(
        {'definition': source, 'id': 'selector-is-not-lineage'},
        resolve_definition=lambda _body: ResolvedDefinition(source, 'inline'),
    )
    assert failure is None
    assert unlinked is not None and unlinked.orchestration_id == ''

    stored, failure = run_http.prepare_run_request(
        {'id': ' flow-1 ', 'input': 'go'},
        resolve_definition=lambda _body: ResolvedDefinition(
            source, 'stored:flow-1', 'flow-1'),
    )
    assert failure is None
    assert stored is not None and stored.orchestration_id == 'flow-1'


def test_shared_run_request_maps_missing_and_invalid_definitions_once(
        monkeypatch):
    monkeypatch.setattr(
        request_http,
        'api_bad_request',
        lambda message, **extras: ({'message': message, **extras}, 400),
    )
    monkeypatch.setattr(
        run_http,
        'invalid_definition_response',
        lambda inspection: ({'inspection': inspection,
                             'errors': inspection['errors']}, 400),
    )
    prepared, missing = run_http.prepare_run_request(
        {}, resolve_definition=lambda _body: ResolvedDefinition(None),
    )
    assert prepared is None
    payload, status = missing
    assert status == 400
    assert payload['message'] == 'definition or id is required'

    invalid = _definition(name='')
    prepared, rejected = run_http.prepare_run_request(
        {'definition': invalid},
        resolve_definition=lambda _body: ResolvedDefinition(invalid, 'inline'),
    )
    assert prepared is None
    payload, status = rejected
    assert status == 400
    assert payload['errors']
    assert payload['inspection']['ok'] is False


def test_shared_run_request_enforces_one_input_contract():
    with pytest.raises(BadRequest, match='input must be a string') as error:
        run_http.prepare_run_request(
            {'definition': _definition(), 'input': 42},
            resolve_definition=lambda body: ResolvedDefinition(
                body['definition'], 'inline'),
        )
    assert error.value.field == 'input'


def test_run_start_response_fields_are_shared_and_detached(monkeypatch):
    inspection = {
        'ok': True,
        'warnings': ['review'],
        'errors': [],
        'contract': {'format': 'contract-v1'},
    }
    prepared = run_http.PreparedRunRequest(
        definition=_definition(),
        inspection=inspection,
        definition_source='stored',
        input_text='go',
        orchestration_id='flow-1',
    )

    fields = run_http.run_start_response_fields(
        prepared, 'task-1', kind='ephemeral')

    assert fields == {
        'task_id': 'task-1',
        'start': {
            'format': 'tofu.orchestration.runtime-start/v1',
            'kind': 'ephemeral',
            'id': 'task-1',
        },
        'definitionSource': 'stored',
        'inspection': inspection,
        'warnings': ['review'],
        'contract': {'format': 'contract-v1'},
    }
    assert fields['inspection'] is not inspection
    inspection['warnings'].append('later mutation')
    assert fields['warnings'] == ['review']
    assert fields['inspection']['warnings'] == ['review']

    durable = run_http.run_start_response_fields(
        prepared, 'run-1', kind='durable')
    assert durable['run_id'] == 'run-1'
    assert 'task_id' not in durable
    assert durable['start'] == {
        'format': 'tofu.orchestration.runtime-start/v1',
        'kind': 'durable',
        'id': 'run-1',
    }
    monkeypatch.setattr(
        run_http, 'api_ok', lambda **values: ('ok', values))
    monkeypatch.setattr(
        run_http, 'api_created', lambda **values: ('created', values))
    current_fields = run_http.run_start_response_fields(
        prepared, 'task-1', kind='ephemeral')
    assert run_http.run_start_response(
        prepared, 'task-1', kind='ephemeral') == ('ok', current_fields)
    assert run_http.run_start_response(
        prepared, 'run-1', kind='durable') == ('created', durable)


def test_runtime_start_request_uses_one_service_and_projection_seam(monkeypatch):
    prepared = run_http.PreparedRunRequest(
        definition=_definition(),
        inspection={'ok': True, 'warnings': [], 'errors': []},
        definition_source='stored:flow-1',
        input_text='go',
        orchestration_id='flow-1',
    )
    starts = []

    class RuntimeStarts:
        def start(self, kind, definition, **options):
            starts.append((kind, definition, options))
            return 'task-1' if kind == 'ephemeral' else 'run-1'

    monkeypatch.setattr(
        start_http,
        'prepare_run_request',
        lambda body, **_options: (prepared, None),
    )
    monkeypatch.setattr(
        start_http,
        'run_start_response',
        lambda value, runtime_id, *, kind: {
            'prepared': value,
            'id': runtime_id,
            'kind': kind,
        },
    )
    monkeypatch.setattr(
        start_http,
        'orchestration_service_response',
        lambda context, operation, project: (context, project(operation())),
    )
    service = RuntimeStarts()

    ephemeral = start_http.runtime_start_request_response(
        'start.live',
        'ephemeral',
        {'definition': {}},
        resolve_definition=lambda _body: None,
        runtime_start_service=lambda: service,
    )
    durable = start_http.runtime_start_request_response(
        'start.durable',
        'durable',
        {'id': 'flow-1'},
        resolve_definition=lambda _body: None,
        runtime_start_service=lambda: service,
        created_by='key-1',
    )

    assert ephemeral == ('start.live', {
        'prepared': prepared,
        'id': 'task-1',
        'kind': 'ephemeral',
    })
    assert durable == ('start.durable', {
        'prepared': prepared,
        'id': 'run-1',
        'kind': 'durable',
    })
    assert starts == [
        ('ephemeral', prepared.definition, {
            'input_text': 'go',
            'orchestration_id': 'flow-1',
            'created_by': '',
        }),
        ('durable', prepared.definition, {
            'input_text': 'go',
            'orchestration_id': 'flow-1',
            'created_by': 'key-1',
        }),
    ]

    failure = object()
    monkeypatch.setattr(
        start_http,
        'prepare_run_request',
        lambda body, **_options: (None, failure),
    )
    assert start_http.runtime_start_request_response(
        'start.rejected',
        'ephemeral',
        {},
        resolve_definition=lambda _body: None,
        runtime_start_service=lambda: service,
    ) is failure
    assert len(starts) == 2


def test_run_start_openapi_schema_uses_the_same_identity_contract():
    from lib.orchestration.inspection_wire_contract import (
        inspection_response_schema,
    )
    from lib.orchestration.runtime_wire_contracts import (
        run_start_response_schema as owned_run_start_response_schema,
        runtime_start_identity_schema,
    )

    ephemeral = run_openapi.run_start_response_schema('ephemeral')
    durable = run_openapi.run_start_response_schema('durable')

    assert ephemeral['properties']['start']['properties']['kind'] == {
        'type': 'string', 'enum': ['ephemeral'],
    }
    assert durable['properties']['start']['properties']['kind'] == {
        'type': 'string', 'enum': ['durable'],
    }
    assert ephemeral['properties']['start'] == \
        runtime_start_identity_schema('ephemeral')
    assert durable['properties']['start'] == \
        runtime_start_identity_schema('durable')
    assert ephemeral['properties']['inspection'] == \
        inspection_response_schema()
    assert run_openapi.run_start_response_schema is \
        owned_run_start_response_schema
    assert 'def run_start_response_schema' not in open(
        run_openapi.__file__, encoding='utf-8').read()
    assert 'task_id' in ephemeral['required']
    assert 'run_id' in durable['required']
    responses = run_openapi.run_start_responses('ephemeral')
    assert responses['200'] == {
        'description': 'Ephemeral orchestration run started',
        'content': {'application/json': {'schema': ephemeral}},
    }
    assert set(responses) == {'200', '400', '401', '403', '500'}
    assert set(run_openapi.run_start_responses('durable')) == {
        '201', '400', '401', '403', '500',
    }
    ephemeral_error = responses['500']['content']['application/json']['schema']
    durable_error = run_openapi.run_start_responses('durable')[
        '500']['content']['application/json']['schema']
    assert ephemeral_error == {
        '$ref': '#/components/schemas/ErrorEnvelope',
    }
    assert durable_error['allOf'][0] == ephemeral_error
    assert durable_error['allOf'][1]['properties']['run_id'] == {
        'type': 'string', 'minLength': 1,
    }
    with pytest.raises(ValueError, match='unknown runtime start kind'):
        run_openapi.run_start_response_schema('future')
    with pytest.raises(ValueError, match='unknown runtime start kind'):
        run_openapi.run_start_responses('future')


def test_both_run_adapters_use_the_shared_preparation_boundary():
    runtime = (ROOT / 'routes/api_v1/orchestration_runtime_routes.py').read_text()
    durable = (ROOT / 'routes/api_v1/orchestration_task_routes.py').read_text()
    shared = (ROOT / 'routes/api_v1/orchestration_run_http.py').read_text()
    start = (ROOT / 'routes/api_v1/'
             'orchestration_runtime_start_http.py').read_text()
    ingress = (ROOT / 'routes/api_v1/'
               'orchestration_definition_request_http.py').read_text()
    selection = (ROOT / 'lib/orchestration/'
                 'definition_selection_contract.py').read_text()

    assert runtime.count('runtime_start_request_response(') == 1
    assert durable.count('runtime_start_request_response(') == 1
    assert 'prepare_run_request(' not in runtime + durable
    assert 'run_start_response(' not in runtime + durable
    assert 'run_start_response_fields(' not in runtime + durable
    assert start.count('prepare_run_request(') == 1
    assert start.count('run_start_response(') == 1
    assert start.count('runtime_start_service().start(') == 1
    assert shared.count('def run_start_response(') == 1
    assert shared.count('def run_start_response_fields(') == 1
    assert 'definitionSource=prepared.definition_source' not in (
        runtime + durable)
    assert 'prepare_definition(' not in runtime + durable
    assert "optional_str(body, 'input'" not in runtime + durable
    assert shared.count('prepare_definition(') == 1
    assert shared.count('resolve_definition_request(') == 1
    assert 'resolve_definition(body)' not in shared
    assert ingress.count('resolve_definition(body)') == 1
    assert 'definition_selection_contract()' in ingress
    assert 'definition_selection_input(body)' in shared
    assert "body, 'input'" not in shared
    assert 'max_len=contract[\'inputMaxLength\']' in selection
    assert shared.count('invalid_definition_response(') == 1
    assert 'api/v1/orchestrations' not in shared
    assert len(shared.splitlines()) < 110
    assert len(start.splitlines()) < 80
    assert len(selection.splitlines()) < 90

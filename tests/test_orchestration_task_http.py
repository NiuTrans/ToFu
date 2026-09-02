"""Contract tests for durable-run HTTP ingress and read projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration.run_status import RUN_STATUS_ORDER
from lib.orchestration.durable_run_field_registry import (
    durable_run_list_envelope_contract,
)
from lib.orchestration.http_endpoint_contract import (
    orchestration_http_endpoint,
)
from lib.task_replay import (
    TASK_REPLAY_FORMAT,
    TaskReplayPage,
    task_replay_request_contract,
)
import routes.api_v1.orchestration_task_http as task_http
import routes.api_v1.orchestration_task_list_http as list_http
from routes.task_http import task_replay_cursor, task_replay_parameters


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_durable_query_parameters_publish_the_executable_contract():
    envelope = durable_run_list_envelope_contract()
    assert orchestration_http_endpoint('task-list').query_fields == (
        'status', 'orch_id', 'limit',
    )
    assert list_http.DURABLE_RUN_PAGE_SIZE == envelope['defaultLimit']
    assert list_http.DURABLE_RUN_PAGE_MAX == envelope['maxLimit']
    assert task_http.durable_replay_parameters is task_replay_parameters
    assert task_http.durable_replay_cursor is task_replay_cursor
    replay_request = task_replay_request_contract()
    assert orchestration_http_endpoint('run-poll').query_fields == (
        replay_request['queryField'],
    )
    assert orchestration_http_endpoint('task-events').query_fields == (
        replay_request['queryField'],
    )
    list_parameters = {
        parameter['name']: parameter
        for parameter in list_http.durable_run_list_parameters()
    }
    assert set(list_parameters) == {'status', 'orch_id', 'limit'}
    assert list_parameters['status']['schema'] == {
        'type': 'string', 'enum': list(RUN_STATUS_ORDER),
    }
    assert list_parameters['orch_id']['schema'] == {'type': 'string'}
    assert list_parameters['limit']['schema'] == {
        'type': 'integer', 'minimum': 1, 'maximum': 150, 'default': 50,
    }
    assert task_http.durable_replay_parameters() == [{
        'name': replay_request['queryField'],
        'in': 'query',
        'schema': {
            'type': 'integer',
            'minimum': replay_request['minimum'],
            'default': replay_request['default'],
        },
        'description': replay_request['description'],
    }]


def test_durable_list_query_trims_filters_and_rejects_unknown_status(
        monkeypatch):
    prepared, failure = list_http.prepare_durable_run_list_query({
        'status': '  running  ', 'orch_id': '  flow-1  ',
    })
    assert failure is None
    assert prepared == list_http.PreparedDurableRunListQuery(
        status='running', orchestration_id='flow-1', limit=50,
    )
    assert prepared.probe_limit == 51

    prepared, failure = list_http.prepare_durable_run_list_query({
        'limit': '100',
    })
    assert failure is None
    assert prepared.limit == 100
    assert prepared.probe_limit == 101

    monkeypatch.setattr(
        list_http,
        'api_bad_request',
        lambda message, **extras: ({'message': message, **extras}, 400),
    )
    prepared, failure = list_http.prepare_durable_run_list_query({
        'status': 'future',
    })
    assert prepared is None
    payload, status = failure
    assert status == 400
    assert payload == {
        'message': 'Invalid orchestration run status',
        'statuses': list(RUN_STATUS_ORDER),
    }

    prepared, failure = list_http.prepare_durable_run_list_query({
        'limit': '151',
    })
    assert prepared is None
    payload, status = failure
    assert status == 400
    assert payload == {
        'message': 'Invalid orchestration run list limit',
        'minimum': 1,
        'maximum': 150,
    }


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, 0), ('', 0), ('12', 12), ('-3', 0), ('future', 0)],
)
def test_durable_replay_cursor_normalizes_query_values(value, expected):
    assert task_http.durable_replay_cursor({'cursor': value}) == expected


def test_durable_read_responses_share_one_projection_boundary(monkeypatch):
    monkeypatch.setattr(
        task_http, 'api_ok',
        lambda data=None, **extras: ({**(data or {}), **extras}, 200),
    )
    monkeypatch.setattr(
        list_http, 'api_ok',
        lambda data=None, **extras: ({**(data or {}), **extras}, 200),
    )
    monkeypatch.setattr(
        task_http, 'api_not_found', lambda message: ({'message': message}, 404),
    )
    runs = [{'id': 'run-1'}, {'id': 'run-2'}]
    assert list_http.durable_run_list_response(runs, 1) == ({
        'runs': runs[:1],
        'page': {'limit': 1, 'has_more': True, 'next_limit': 51},
    }, 200)
    assert task_http.durable_run_entry_response(runs[0]) == (
        {'run': runs[0]}, 200,
    )
    assert task_http.durable_run_entry_response(None) == (
        {'message': 'Run not found'}, 404,
    )


def test_durable_list_response_executes_published_envelope_mapping(monkeypatch):
    monkeypatch.setattr(list_http, '_DURABLE_RUN_LIST_ENVELOPE', {
        'itemsField': 'records', 'pageField': 'paging',
        'limitField': 'window', 'hasMoreField': 'remaining',
        'nextLimitField': 'next_window', 'pageStep': 25, 'maxLimit': 100,
    })
    monkeypatch.setattr(
        list_http, 'api_ok',
        lambda data=None, **extras: ({**(data or {}), **extras}, 200),
    )

    assert list_http.durable_run_list_response([{'id': 'one'}, {'id': 'two'}], 1) == ({
        'records': [{'id': 'one'}],
        'paging': {'window': 1, 'remaining': True, 'next_window': 26},
    }, 200)


def test_durable_replay_response_preserves_success_and_missing_wire_shapes(
        monkeypatch):
    monkeypatch.setattr(
        task_http, 'task_replay_response',
        lambda payload: (
            payload, 404 if payload.get('error') == 'not_found' else 200),
    )
    replay = TaskReplayPage(
        events=[{'seq': 2, 'type': 'step'}],
        next_cursor=3,
        run_status='running',
        done=False,
        requested_cursor=2,
    )
    payload, status = task_http.durable_replay_response(replay, 2)
    assert status == 200
    assert payload['format'] == TASK_REPLAY_FORMAT
    assert payload['events'] == [{'seq': 2, 'type': 'step'}]
    assert payload['cursor'] == {'requested': 2, 'next': 3, 'reset': False}

    payload, status = task_http.durable_replay_response(None, 9)
    assert status == 404
    assert payload['format'] == TASK_REPLAY_FORMAT
    assert payload['error'] == 'not_found'
    assert payload['message'] == 'Run not found'
    assert payload['cursor'] == {'requested': 9, 'next': 9, 'reset': False}


def test_task_http_does_not_publish_a_parallel_service_call_wrapper():
    assert not hasattr(task_http, 'durable_run_service_call')


def test_durable_routes_delegate_query_and_read_contracts_to_http_boundary():
    routes = (ROOT / 'routes/api_v1/'
              'orchestration_task_routes.py').read_text()
    shared = (ROOT / 'routes/api_v1/'
              'orchestration_task_http.py').read_text()
    list_shared = (ROOT / 'routes/api_v1/'
                   'orchestration_task_list_http.py').read_text()

    for name in (
        'prepare_durable_run_list_query',
        'durable_run_list_response',
    ):
        assert routes.count(f'{name}(') == 1
        assert f'def {name}(' in list_shared
        assert f'def {name}(' not in shared
    for name in (
        'durable_run_entry_response',
        'durable_replay_response',
    ):
        assert routes.count(f'{name}(') == 1
        assert f'def {name}(' in shared
    for name in ('durable_replay_cursor', 'durable_replay_parameters'):
        assert routes.count(f'{name}(') == 1
        assert f'{name} = task_replay_' in shared
        assert f'def {name}(' not in shared
    for implementation_detail in (
        'RUN_STATUS_ORDER',
        'is_run_status',
        'safe_replay_cursor',
        'missing_replay_page',
        'task_replay_http_status',
        'api_not_found',
        'api_payload',
    ):
        assert implementation_detail not in routes
    assert routes.count('parameters=_RUN_LIST_PARAMETERS') == 1
    assert routes.count('parameters=_REPLAY_PARAMETERS') == 1
    assert 'limit=query.probe_limit' in routes
    assert 'query.limit + 1' not in routes

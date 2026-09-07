"""Canonical durable orchestration run-status protocol."""

from __future__ import annotations

import os

import pytest

from lib.orchestration.run_status import (
    INITIAL_RUN_STATUS,
    RUN_STATUS_CATEGORIES,
    RUN_STATUS_ORDER,
    VALID_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
    run_status_contract,
    run_status_contract_schema,
)


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))


def test_run_status_contract_is_ordered_detached_and_self_consistent():
    contract = run_status_contract()
    assert contract['initial'] == INITIAL_RUN_STATUS == 'pending'
    assert contract['statuses'] == list(RUN_STATUS_ORDER)
    assert set(contract['statuses']) == VALID_RUN_STATUSES
    assert contract['terminal'] == ['done', 'error', 'aborted']
    assert set(contract['terminal']) == TERMINAL_RUN_STATUSES
    assert contract['categories'] == RUN_STATUS_CATEGORIES
    assert set(contract['categories']) == VALID_RUN_STATUSES
    assert all(is_terminal_run_status(status) for status in contract['terminal'])
    assert not is_terminal_run_status('running')
    assert not is_terminal_run_status('future-status')
    assert all(is_run_status(status) for status in RUN_STATUS_ORDER)
    assert not is_run_status('future-status')
    assert not is_run_status(None)

    contract['terminal'].append('client-mutation')
    contract['categories']['running'] = 'client-mutation'
    assert 'client-mutation' not in run_status_contract()['terminal']
    assert run_status_contract()['categories']['running'] == 'active'


def test_run_status_openapi_schema_is_derived_and_detached():
    contract = run_status_contract()
    schema = run_status_contract_schema()
    properties = schema['properties']

    assert schema['required'] == [
        'schema', 'initial', 'statuses', 'terminal', 'categories',
    ]
    assert properties['schema']['enum'] == [contract['schema']]
    assert properties['statuses']['items']['enum'] == contract['statuses']
    assert properties['terminal']['items']['enum'] == contract['terminal']
    assert properties['categories']['required'] == contract['statuses']
    assert {
        status: spec['enum'][0]
        for status, spec in properties['categories']['properties'].items()
    } == contract['categories']

    properties['statuses']['items']['enum'].append('client-status')
    assert 'client-status' not in run_status_contract_schema()[
        'properties']['statuses']['items']['enum']


def test_persistence_and_http_replay_use_canonical_terminal_predicate():
    run_persistence = ''.join(open(os.path.join(ROOT, path),
                                   encoding='utf-8').read() for path in (
        'lib/storage_sidecar/operations_pkg/_runs.py',
    ))
    route = open(os.path.join(
        ROOT, 'routes/api_v1/orchestrations.py'), encoding='utf-8').read()
    task_routes = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_task_routes.py'),
        encoding='utf-8',
    ).read()
    task_http = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_task_http.py'),
        encoding='utf-8',
    ).read()
    shared_task_http = open(os.path.join(
        ROOT, 'routes/task_http.py'), encoding='utf-8').read()
    mutation_routes = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_mutation_routes.py'),
        encoding='utf-8',
    ).read()
    generic_task_routes = open(os.path.join(
        ROOT, 'routes/_task_routes.py'), encoding='utf-8').read()
    mutation = open(os.path.join(
        ROOT, 'lib/orchestration/mutation_operations.py'),
        encoding='utf-8').read()
    mutation_service = open(os.path.join(
        ROOT, 'lib/orchestration/runtime_mutation_service.py'),
        encoding='utf-8').read()
    outcome = open(os.path.join(
        ROOT, 'lib/orchestration/outcome_projection.py'),
        encoding='utf-8').read()
    outcome_contract = open(os.path.join(
        ROOT, 'lib/orchestration/outcome_contract.py'),
        encoding='utf-8').read()

    assert 'is_terminal_run_status(status)' in run_persistence
    assert 'is_run_status(status)' in run_persistence
    assert 'TERMINAL_RUN_STATUSES' in run_persistence
    assert 'Terminal orchestration status requires an explicit transition' in \
        run_persistence
    assert "frozenset({'done', 'error', 'aborted'})" not in run_persistence
    assert "status in ('done', 'error', 'aborted')" not in (
        task_routes + mutation_routes)
    assert 'run_service().replay(run_id, cursor)' in task_routes
    assert 'task_replay_http_status(payload)' not in task_routes
    assert 'task_replay_http_status(payload)' not in task_http
    assert 'task_replay_http_status(payload)' in shared_task_http
    assert 'task_replay_response(resp)' in generic_task_routes
    assert "resp.get('error') == 'not_found'" not in generic_task_routes
    assert 'runtime_mutation_service(owner_user_id).abort(task_id)' in \
        mutation_routes
    assert 'self._runtime, task_id, self.owner_user_id' in mutation_service
    assert 'is_terminal_run_status(status)' in mutation
    assert 'is_terminal_run_status(status)' in outcome
    assert "status in {'done', 'error', 'aborted'}" not in outcome
    assert "run_status_contract()['terminal']" in outcome_contract
    assert 'register_orchestration_task_routes(' in route

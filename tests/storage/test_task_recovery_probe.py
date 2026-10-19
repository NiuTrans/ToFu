"""The startup orphan probe reads the root task status on each SQL backend."""

import pytest

from tests.storage.test_storage_authority_sql_contract import storage as storage


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_task_recovery_probe_requires_a_root_live_task_status(storage, status):
    client = storage.client
    assert client.query('task_results.recovery_pending', {}) == {'pending': False}
    for index, value in enumerate([
        {'status': 'done', 'nested': {'status': status}},
        {'content': {'status': status}},
        {'status': [status]},
        {'status': None},
        [{'status': status}],
    ]):
        client.command('record.put', {
            'namespace': 'task_results', 'key': f'not-live-{index}', 'value': value,
        }, f'probe-not-live-{index}')
    client.command('record.put', {
        'namespace': 'other-results', 'key': 'foreign', 'value': {'status': status},
    }, 'probe-other-namespace')
    assert client.query('task_results.recovery_pending', {}) == {'pending': False}
    client.command('record.put', {
        'namespace': 'task_results', 'key': 'live', 'value': {'status': status},
    }, 'probe-live')
    assert client.query('task_results.recovery_pending', {}) == {'pending': True}
    client.command('record.put', {
        'namespace': 'task_results', 'key': 'live', 'value': {'status': 'interrupted'},
    }, 'probe-settled')
    assert client.query('task_results.recovery_pending', {}) == {'pending': False}

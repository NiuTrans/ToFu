"""SQL/native inspector summaries retain complete large task trees."""
from uuid import uuid4

import pytest

from tests.tofudb.test_tofudb_differential import (
    legacy_driver as legacy_driver, tofudb_binary, tofudb_daemon as tofudb_daemon,  # noqa: F401
)

pytestmark = [pytest.mark.unit, pytest.mark.serial]
pytest_plugins = ('tests._chat_sidecar',)


def test_inspector_summary_complete_task_tree_backend_parity(legacy_driver, tofudb_daemon):
    prefix = 'inspector-demand-' + uuid4().hex
    roots = [f'{prefix}-{i:04d}' for i in range(129)]
    children = [f'{roots[0]}#agent:{i:04d}' for i in range(1101)]
    identities = sorted(roots + children)
    snapshots = []
    for driver in (legacy_driver, tofudb_daemon):
        appended = driver.execute('event.append_batch', {'events': [
            {'task_id': task_id, 'sequence': 0,
             'event': {'type': 'messages_snapshot', 'kind': 'request'}}
            for task_id in identities]}, uuid4().hex)
        assert appended.ok, (appended.error_code, appended.error_message)
        outcome = driver.execute('event.inspector_summary',
            {'task_ids': roots + [roots[0], children[0]]}, None)
        assert outcome.ok, (outcome.error_code, outcome.error_message)
        records = outcome.value['records']
        assert [row['task_id'] for row in records] == identities
        assert all(row['event_count'] == row['request_count'] == 1 for row in records)
        snapshots.append([{key: value for key, value in row.items()
                           if key != 'first_event_at_ms'} for row in records])
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize('task_ids', [[], [None], ['']])
def test_inspector_invalid_root_shapes_remain_denied(legacy_driver, tofudb_daemon, task_ids):
    for driver in (legacy_driver, tofudb_daemon):
        outcome = driver.execute('event.inspector_summary', {'task_ids': task_ids}, None)
        assert not outcome.ok
        assert outcome.error_code == 'database_protocol_error'


def test_complete_type_catalog_prefix_pages_and_inspector_backend_parity(legacy_driver, tofudb_daemon):
    task = 'type-demand-' + uuid4().hex
    events = [{'task_id': task, 'sequence': i * 7 + 11,
               'event': {'type': f'endpoint_{1100-i:04d}'}} for i in range(1101)]
    expected = [(item['sequence'], item['event']) for item in events]
    for driver in (legacy_driver, tofudb_daemon):
        appended = driver.execute('event.append_batch', {'events': events}, uuid4().hex)
        assert appended.ok, (appended.error_code, appended.error_message)
        seen = []
        after = None
        while True:
            payload = {'task_id': task, 'limit': 197, 'types': ['endpoint_0000'],
                       'type_prefixes': ['endpoint_', 'endpoint_00'], 'payload_types': []}
            if after is not None:
                payload['after_sequence'] = after
            outcome = driver.execute('event.list', payload, None)
            assert outcome.ok, (outcome.error_code, outcome.error_message)
            if not outcome.value:
                break
            assert len(outcome.value) <= 197
            seen.extend((row['sequence'], row['event']) for row in outcome.value)
            after = seen[-1][0]
        assert seen == expected
        summary = driver.execute('event.inspector_summary', {'task_ids': [task]}, None)
        assert summary.ok, (summary.error_code, summary.error_message)
        assert summary.value['records'][0]['event_count'] == 1101

"""The Coordinator task board is one validated, binding-scoped, idempotent head."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from lib.conversations.coordinator_progress import CoordinatorTaskProgress, POLICY
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Store:
    def __init__(self):
        self.rows = {}; self.lock = threading.Lock(); self.lost_ack = False; self.puts = 0

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        assert operation == 'record.get'
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': payload['expected_version'] + 1}
            self.puts += 1
            if self.lost_ack:
                self.lost_ack = False
                raise StorageError('database_unavailable', 'ACK lost', retryable=True)
            return {'version': payload['expected_version'] + 1}


@pytest.fixture
def board():
    store = Store(); notices = []
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    clock = iter(range(1000, 10_000, 10))

    def factory(owner=7, path='/project'):
        return CoordinatorTaskProgress(owner, path, client=store, bindings=bindings,
                                       notify=lambda: notices.append('push'), clock=lambda: next(clock))
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    request = {'summary': 'The folder fix is verified; the trip research is underway.', 'tasks': [
        {'title': 'Fix folder creation in the sidebar', 'status': 'completed', 'progress': 'Merged and verified.'},
        {'title': 'Research New Year homestays', 'status': 'in_progress', 'progress': 'Comparing four platforms.'},
        {'title': 'Summarize the WeChat article', 'status': 'pending'},
    ]}
    return SimpleNamespace(store=store, binding=binding, factory=factory, scope=scope,
                           request=request, notices=notices)


def test_board_round_trips_for_the_current_binding_only(board):
    b = board
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': 'coordinator',
                                  'bindingRevision': 1, 'board': None}
    published = b.factory().publish(b.scope, b.request)
    assert published['changed'] and published['board']['revision'] == 1
    state = b.factory().read()
    assert state['board']['summary'] == b.request['summary']
    assert state['board']['tasks'] == b.request['tasks'] and state['board']['updatedAt'] > 0
    assert b.notices == ['push']
    # A new Coordinator binding starts with an empty board; the old one never leaks.
    b.binding['revision'] = 2
    assert b.factory().read()['board'] is None
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)
    newer = dict(b.scope, bindingRevision=2)
    assert b.factory().publish(newer, {'summary': 'Fresh start.', 'tasks': []})['board']['revision'] == 1
    # An older binding cannot overwrite the newer team's board even if it is current again.
    b.binding['revision'] = 1
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)


def test_unchanged_board_is_a_quiet_noop_and_revisions_advance(board):
    b = board
    b.factory().publish(b.scope, b.request)
    again = b.factory().publish(b.scope, deepcopy(b.request))
    assert not again['changed'] and again['board']['revision'] == 1
    assert b.store.puts == 1 and b.notices == ['push']
    changed = deepcopy(b.request); changed['tasks'][1]['status'] = 'completed'
    assert b.factory().publish(b.scope, changed)['board']['revision'] == 2
    assert b.factory().read()['board']['tasks'][1]['status'] == 'completed'


def test_lost_acknowledgement_replays_without_a_second_revision(board):
    b = board
    b.store.lost_ack = True
    published = b.factory().publish(b.scope, b.request)
    assert published['board']['revision'] == 1 and b.store.puts == 1


@pytest.mark.parametrize('request_change', [
    {'summary': ''},
    {'summary': 'two\nlines'},
    {'tasks': [{'title': 'x', 'status': 'blocked'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'owner': 'worker-1'}]},
    {'tasks': [{'title': 'x' * 121, 'status': 'pending'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'progress': ' '}]},
    {'tasks': [{'title': str(index), 'status': 'pending'} for index in range(25)]},
])
def test_invalid_boards_are_refused_with_the_failing_field(board, request_change):
    b = board
    with pytest.raises(ValueError, match='Invalid task board'):
        b.factory().publish(b.scope, {**b.request, **request_change})
    assert b.store.puts == 0


def test_unbound_project_reads_empty_and_corrupt_heads_fail_closed(board):
    b = board
    b.factory().publish(b.scope, b.request)
    key = next(iter(b.store.rows))
    assert key[0] == POLICY['namespace']
    b.store.rows[key]['value']['scope']['owner'] = 8
    with pytest.raises(StorageError):
        b.factory().read()
    b.binding.update(availability='unbound', conversationId='', revision=3)
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': '',
                                  'bindingRevision': 3, 'board': None}


def test_coordinator_tool_publishes_through_the_bound_scope(board, monkeypatch):
    import json
    from lib.conversations import coordinator_progress, coordinator_tools
    b = board
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1}
    monkeypatch.setattr(coordinator_tools, '_scope', lambda task: scope)
    original = coordinator_progress.CoordinatorTaskProgress.__init__
    monkeypatch.setattr(coordinator_progress.CoordinatorTaskProgress, '__init__',
                        lambda self, owner, path: original(
                            self, owner, path, client=b.store,
                            bindings=SimpleNamespace(read=lambda _: deepcopy(b.binding)),
                            notify=lambda: None))
    result = json.loads(coordinator_tools.execute_coordinator_tool({}, 'update_task_progress', b.request))
    assert result['revision'] == 1 and result['task_count'] == 3 and result['changed'] is True
    refused = json.loads(coordinator_tools.execute_coordinator_tool(
        {}, 'update_task_progress', {'summary': 'ok', 'tasks': [{'title': 'x', 'status': 'waiting_on_user'}]}))
    assert 'Invalid task board at tasks/0/status' in refused['error'] and refused['execution_started'] is False

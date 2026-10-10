"""Completion recovery replays the entire durable request after lost ACKs."""
import json
import threading

import pytest

from lib import message_queue
from lib.storage.errors import StorageError
from lib.tasks_pkg import background_commands as commands
from lib.tasks_pkg.handlers._background_command import _queue_completion

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('restart', [False, True])
def test_lost_ack_recovery_freezes_timestamp_config_and_result(monkeypatch, tmp_path, restart):
    monkeypatch.setattr(commands, '_registry', {})
    monkeypatch.setattr(commands, '_records_root', lambda: str(tmp_path))
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _: None)
    monkeypatch.setattr(commands, 'wake_completed_background_batch', lambda *_: None)
    command_id = 'bg_lost_ack'
    (tmp_path / command_id).mkdir()
    done = threading.Event()
    done.set()
    commands._registry[command_id] = {
        'conv_id': 'conv', 'user_id': 7, 'done': done,
        'state': {'command': 'echo result', 'result': 'original', 'delivery_status': 'failed'},
    }
    calls = []
    class Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, json.loads(json.dumps(payload)), command_id))
            if len(calls) <= 3:
                raise StorageError('database_timeout', 'committed; acknowledgement lost')
            assert calls[-1] == calls[0]
            return {'queueId': payload['queue_id']}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **_: Client())
    assert not _queue_completion(task={'convId': 'conv', '_userId': 7},
        config={'model': 'first', 'options': {'x': [1]}}, command_id=command_id,
        command='echo result', result='original')
    assert len(calls) == 3
    assert 'completion_envelope' in commands._registry[command_id]
    if restart:
        commands._registry.clear()
        assert _queue_completion(task={'convId': 'conv', '_userId': 7},
            config={'model': 'changed'}, command_id=command_id,
            command='echo result', result='reconstructed differently')
    else:
        assert commands.retry_failed_background_deliveries() == 1
        assert commands.conversation_results_complete(conv_id='conv', user_id=7)
        assert 'completion_envelope' not in commands._registry[command_id]
    assert len(calls) == 4


def test_receipt_reader_follows_byte_limited_pages(monkeypatch):
    calls = []
    def page(conv_id, *, user_id, page_size, cursor):
        calls.append(cursor)
        index = len(calls)
        return {'items': [{'queueId': f'q{index}', 'kind': 'background_receipt',
                          'payload': {'_backgroundCommand': f'bg{index}', 'text': 'complete'}}],
                'nextCursor': {'priority': 80, 'position': index} if index < 3 else None}
    monkeypatch.setattr(message_queue, 'background_receipt_page', page)
    items = message_queue.pending_background_command_injections('conv', user_id=7)
    assert [item['queueId'] for item in items] == ['q1', 'q2', 'q3']
    assert calls == [None, {'priority': 80, 'position': 1}, {'priority': 80, 'position': 2}]


def test_legacy_checkpoint_envelope_repairs_only_after_permanent_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, '_registry', {})
    monkeypatch.setattr(commands, '_records_root', lambda: str(tmp_path))
    command_id = 'bg_legacy_checkpoint'
    folder = tmp_path / command_id
    folder.mkdir()
    frozen = {'command_id': command_id, 'conv_id': 'conv', 'user_id': 7,
              'message': {'text': 'original result', '_backgroundCommand': command_id,
                          'timestamp': 123, '_backgroundCommandCompletion': {'commandId': command_id, 'status': 'completed'}},
              'config': {'model': 'original-model', 'projectPath': '/project',
                         'checkpointToolRounds': ['x' * 1100000], 'contentPrefix': 'old'}}
    path = folder / 'delivery.json'
    original = json.dumps(frozen)
    path.write_text(original)
    calls = []
    class Client:
        def command(self, operation, payload, command_id):
            calls.append(json.loads(json.dumps(payload)))
            if 'checkpointToolRounds' in payload['config']:
                raise StorageError('database_protocol_error', 'queue core refused')
            return {'queueId': payload['queue_id']}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **_: Client())
    assert _queue_completion(task={'convId': 'conv', '_userId': 7}, config={'model': 'changed'},
                           command_id=command_id, command='echo done', result='reconstructed')
    assert len(calls) == 2
    assert calls[0]['message'] == calls[1]['message'] == frozen['message']
    assert calls[0]['queue_id'] == calls[1]['queue_id']
    assert calls[1]['config'] == {'model': 'original-model', 'projectPath': '/project'}
    assert path.read_text() == original  # Do not overwrite lost-ACK evidence.


def test_new_completion_does_not_freeze_previous_attempt_buffers(monkeypatch):
    monkeypatch.setattr(commands, '_registry', {})
    calls = []
    class Client:
        def command(self, operation, payload, command_id):
            calls.append(payload)
            return {'queueId': payload['queue_id']}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **_: Client())
    config = {'model': 'm', 'projectPath': '/project', 'thinkingEnabled': True,
              'checkpointToolRounds': ['x' * 1100000], 'checkpointImages': ['old'],
              'resumePrefill': 'previous', 'excludeLast': True, 'contentPrefix': 'old',
              '_liveCache': {'large': 'old'}, 'assistantMsgId': 'previous'}
    assert _queue_completion(task={'convId': 'conv', '_userId': 7}, config=config,
                           command_id='bg_new_clean', command='echo done', result='done')
    assert len(calls) == 1
    assert calls[0]['config'] == {'model': 'm', 'projectPath': '/project', 'thinkingEnabled': True}
    assert len(json.dumps(calls[0])) < 2000


def test_retry_backoff_preserves_original_and_does_not_hammer_storage(monkeypatch):
    monkeypatch.setattr(commands, '_registry', {})
    monkeypatch.setattr(commands, '_posix_durable', lambda: False)
    clock = [100.0]
    monkeypatch.setattr(commands.time, 'monotonic', lambda: clock[0])
    done = threading.Event(); done.set()
    state = {'delivery_status': 'failed', 'command': 'echo', 'result': 'original'}
    commands._registry['bg_retry'] = {'conv_id': 'conv', 'user_id': 7, 'done': done, 'state': state}
    calls = []
    def publish(**kwargs):
        calls.append(kwargs)
        return len(calls) == 3
    monkeypatch.setattr('lib.tasks_pkg.handlers._background_command._queue_completion', publish)
    monkeypatch.setattr(commands, 'wake_completed_background_batch', lambda *_: None)
    assert commands.retry_failed_background_deliveries() == 0
    for _ in range(100):
        assert commands.retry_failed_background_deliveries() == 0
    assert len(calls) == 1
    assert state['result'] == 'original'
    clock[0] = 102.0
    assert commands.retry_failed_background_deliveries() == 0
    assert state['delivery_retry_at'] == 106.0
    clock[0] = 106.0
    assert commands.retry_failed_background_deliveries() == 1
    assert state['delivery_status'] == 'queued'
    assert len(calls) == 3
    assert all(call['result'] == 'original' for call in calls)


def test_legacy_lost_ack_replays_original_without_repair(monkeypatch):
    from lib.background_command_delivery import publish_completion_envelope
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _: None)
    frozen = {'message': {'_backgroundCommand': 'bg_legacy_ack', 'timestamp': 123},
              'config': {'model': 'original', 'checkpointToolRounds': ['old']}}
    calls = []
    def enqueue(*args, **kwargs):
        calls.append((json.loads(json.dumps(args)), kwargs.copy()))
        if len(calls) == 1:
            raise StorageError('database_timeout', 'committed; lost ACK')
        return {'queueId': 'original'}
    assert publish_completion_envelope(enqueue, 'conv', frozen,
        kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=7) == {'queueId': 'original'}
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[1][0][2] == frozen['config']


def test_legacy_repair_cannot_bypass_committed_receipt_conflict():
    from lib.background_command_delivery import publish_completion_envelope
    calls = []
    frozen = {'message': {'_backgroundCommand': 'bg_legacy_conflict', 'timestamp': 123},
              'config': {'checkpointToolRounds': ['old']}}
    def enqueue(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise StorageError('database_protocol_error', 'permanent refusal')
        raise StorageError('database_conflict', 'already committed different request')
    with pytest.raises(StorageError, match='already committed'):
        publish_completion_envelope(enqueue, 'conv', frozen,
            kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=7)
    assert len(calls) == 2
    assert calls[0][1] is calls[1][1]


def test_retry_work_budget_leaves_later_results_for_next_tick(monkeypatch):
    monkeypatch.setattr(commands, '_registry', {})
    monkeypatch.setattr(commands, '_posix_durable', lambda: False)
    clock = [100.0]
    monkeypatch.setattr(commands.time, 'monotonic', lambda: clock[0])
    done = threading.Event(); done.set()
    for name in ('bg_a', 'bg_b'):
        commands._registry[name] = {'conv_id': 'conv', 'user_id': 7, 'done': done,
            'state': {'delivery_status': 'failed', 'command': 'echo', 'result': name}}
    calls = []
    def publish(**kwargs):
        calls.append(kwargs['command_id']); clock[0] += 1.1
        return True
    monkeypatch.setattr('lib.tasks_pkg.handlers._background_command._queue_completion', publish)
    monkeypatch.setattr(commands, 'wake_completed_background_batch', lambda *_: None)
    assert commands.retry_failed_background_deliveries() == 1
    assert calls == ['bg_a']
    assert commands._registry['bg_b']['state']['delivery_status'] == 'failed'
    assert commands.retry_failed_background_deliveries() == 1
    assert calls == ['bg_a', 'bg_b']


def test_completion_config_restores_source_roots_and_readonly_descendants():
    from copy import deepcopy
    from lib.background_command_delivery import completion_delivery_config
    config = {
        'model': 'gpt-6.1-sol', 'projectPath': '/isolated/first/src',
        'canonicalProjectPath': '/project/src',
        'projectPaths': ['/isolated/second', '/isolated/first/src', 'remote:host:/code'],
        'canonicalProjectPaths': ['/other', '/project/src', 'remote:host:/code'],
        'readOnlyPaths': ['/isolated/second/private', '/isolated/first/src/docs',
                          '/isolated/second-peer', '/external'],
        'workspaceRuntimeRoots': ['/project/data'],
        '_workspaceDelivery': {'repositories': []}, 'checkpointToolRounds': ['old'],
        'assistantMsgId': 'old', 'thinkingDepth': 'max',
    }
    before = deepcopy(config)
    cleaned = completion_delivery_config(config)
    assert cleaned == {
        'model': 'gpt-6.1-sol', 'projectPath': '/project/src',
        'projectPaths': ['/other', '/project/src', 'remote:host:/code'],
        'readOnlyPaths': ['/other/private', '/project/src/docs', '/isolated/second-peer', '/external'],
        'thinkingDepth': 'max',
    }
    assert config == before
    assert completion_delivery_config(cleaned) == cleaned


def test_completion_without_admission_metadata_keeps_explicit_linked_root():
    from lib.background_command_delivery import completion_delivery_config
    config = {'projectPath': '/explicit-worktree', 'readOnlyPaths': ['/explicit-worktree/docs']}
    assert completion_delivery_config(config) == config

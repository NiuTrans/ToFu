"""Domain-facade contracts for turn-native conversations.

Transaction semantics are exercised against the real authority in
``test_storage_sidecar_contract.py``. This suite pins the public facade and
its pure projection/settlement behavior without constructing a second store.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _new_conversation_id(prefix='turn-facade'):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _create(conversation_id, *, command_id='create', user_id=1):
    from lib.turn_lifecycle import create_turn_pair
    return create_turn_pair(
        conversation_id,
        command_id=f'{command_id}-{conversation_id}',
        input_projection={'content': 'hello'},
        config={'model': 'gpt-4o'},
        user_id=user_id,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Turn facade',
            'settings': {'model': 'gpt-4o'},
        },
    )



def test_facade_rejects_missing_owner_before_storage_access(monkeypatch):
    import lib.turn_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        '_turn_client',
        lambda **_kwargs: pytest.fail('invalid owner reached storage'),
    )

    with pytest.raises(ValueError, match='numeric user_id'):
        lifecycle.get_attempt('attempt', user_id=None)
    with pytest.raises(ValueError, match='positive user_id'):
        lifecycle.list_turns('conversation', user_id=0)
    with pytest.raises(ValueError, match='numeric user_id'):
        lifecycle.read_events('attempt', user_id='not-an-owner')


def test_attempt_claim_retries_ambiguous_timeout_with_stable_owner(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError

    calls = []

    class Client:
        def command(self, operation, payload, command_id, **kwargs):
            calls.append((operation, dict(payload), command_id, dict(kwargs)))
            if len(calls) == 1:
                raise StorageError(
                    'database_timeout', 'ambiguous claim acknowledgement', True, 0
                )
            return True

    monkeypatch.setattr(lifecycle, '_turn_client', lambda **_kwargs: Client())
    monkeypatch.setattr(lifecycle.time, 'sleep', lambda _delay: None)

    assert lifecycle.claim_attempt_start('attempt-retry', user_id=7) is True
    assert len(calls) == 2
    assert calls[0][1]['dispatch_owner_id'] == calls[1][1]['dispatch_owner_id']
    assert calls[0][1]['dispatch_owner_id']
    assert all(call[3]['deadline'] == 2.0 for call in calls)


def test_dispatchable_recovery_starts_only_explicit_executor_attempts(
    chat_sidecar, monkeypatch,
):
    import lib.conversation_sync.command_service as command_service_module
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync.dispatch_contract import (
        ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY,
    )
    from lib.turn_lifecycle import create_turn_pair, get_attempt

    marked_conversation = _new_conversation_id('dispatchable')
    external_conversation = _new_conversation_id('external')
    starts = []

    def start_task(conv_id, config, _data, abort_after, on_registered):
        task_id = f'recovered-{conv_id}'
        starts.append({
            'taskId': task_id,
            'config': dict(config),
            'abortAfter': abort_after,
        })
        on_registered(task_id)
        return task_id, None

    service = ConversationTurnCommandService(
        build_user_message=lambda *args: {},
        was_aborted_after=lambda *args: False,
        start_task=start_task,
    )
    original_claim = command_service_module.claim_attempt_start
    monkeypatch.setattr(
        command_service_module, 'claim_attempt_start', lambda *_args, **_kwargs: False,
    )
    request_started_at = 1_234.567
    marked = service.create_turn(
        marked_conversation,
        7,
        {
            'commandId': f'create-{marked_conversation}',
            'inputTurn': {'content': 'recover me'},
            'config': {
                'model': 'gpt-4o',
                # Public callers cannot forge the cancellation watermark.
                ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY: 1,
            },
            'conversation': {'allowCreate': True},
        },
        request_started_at=request_started_at,
    ).value
    assert marked['attempt']['taskId'] == ''
    monkeypatch.setattr(
        command_service_module, 'claim_attempt_start', original_claim,
    )
    external = create_turn_pair(
        external_conversation,
        command_id=f'create-{external_conversation}',
        input_projection={'content': 'persist only'},
        config={'model': 'gpt-4o'},
        user_id=7,
        conversation_defaults={'allowCreate': True},
    )
    stats = service.recover_dispatchable_attempts(
        created_before_ms=int(marked['attempt']['createdAt']) + 1,
        limit=8,
    )

    assert stats == {'examined': 1, 'recovered': 1, 'settledFailed': 0}
    assert len(starts) == 1
    assert starts[0]['taskId'] == f'recovered-{marked_conversation}'
    assert starts[0]['config']['model'] == 'gpt-4o'
    assert (
        ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY
        not in starts[0]['config']
    )
    assert starts[0]['abortAfter'] == request_started_at
    assert get_attempt(
        marked['attempt']['attemptId'], user_id=7,
    )['taskId'] == starts[0]['taskId']
    assert get_attempt(
        external['attempt']['attemptId'], user_id=7,
    )['taskId'] == ''


def test_turn_pair_is_owner_scoped_atomic_and_idempotent(chat_sidecar):
    from lib.turn_lifecycle import (
        LifecycleNotFound,
        bind_task,
        claim_attempt_start,
        create_turn_pair,
        get_turn,
        list_turns,
        read_events,
    )

    conversation_id = _new_conversation_id()
    first = _create(conversation_id, user_id=7)
    second = create_turn_pair(
        conversation_id,
        command_id=f'create-{conversation_id}',
        input_projection={'content': 'mutated retry body'},
        config={'model': 'different'},
        user_id=7,
    )

    assert second['idempotentReplay'] is True
    assert second['turn']['turnId'] == first['turn']['turnId']
    assert second['attempt']['attemptId'] == first['attempt']['attemptId']
    snapshot = list_turns(conversation_id, user_id=7)
    assert [(turn['actor'], turn['ordinal']) for turn in snapshot['turns']] == [
        ('human', 0), ('assistant', 1),
    ]
    events = read_events(first['attempt']['attemptId'], user_id=7)
    assert events[0]['turnId'] == first['turn']['turnId']
    assert claim_attempt_start(
        first['attempt']['attemptId'], user_id=7) is True
    # Same-process replay recovers an ambiguous claim acknowledgement.
    assert claim_attempt_start(
        first['attempt']['attemptId'], user_id=7) is True
    from lib.storage import get_storage_client
    assert get_storage_client(write=True).command(
        'turn.attempt.claim', {
            'attempt_id': first['attempt']['attemptId'],
            'user_id': 7,
            'dispatch_owner_id': 'different-process-owner',
        },
        f"foreign-claim-{first['attempt']['attemptId']}",
    ) is False
    bind_task(first['attempt']['attemptId'], 'claimed-task', user_id=7)
    assert claim_attempt_start(
        first['attempt']['attemptId'], user_id=7) is False
    with pytest.raises(LifecycleNotFound):
        get_turn(
            conversation_id, first['turn']['turnId'], user_id=8)


def test_queued_turn_pair_keeps_identity_and_cancels_atomically(chat_sidecar):
    from lib.message_queue import get_queue
    from lib.turn_lifecycle import (
        LifecycleNotFound,
        cancel_queued_turn_pair,
        create_turn_pair,
        list_turns,
    )

    conversation_id = _new_conversation_id('queued-pair')
    _create(conversation_id, user_id=7)
    queue_id = f'queue-{uuid.uuid4().hex}'
    command_id = f'queued-command-{uuid.uuid4().hex}'
    queued = create_turn_pair(
        conversation_id,
        command_id=command_id,
        input_projection={'content': 'send this next'},
        config={'model': 'gpt-4o'},
        user_id=7,
        queue_binding={
            'queueId': queue_id,
            'kind': 'real',
            'priority': 100,
            'message': {'text': 'send this next'},
        },
    )

    assert queued['queued'] is True
    assert queued['_needsStart'] is False
    assert queued['submittedTurn']['presentationId'] == f'{command_id}:input'
    assert queued['turn']['presentationId'] == f'{command_id}:output'
    assert queued['attempt']['queueBinding'] == {
        'queueId': queue_id, 'state': 'pending',
    }
    queue_item = get_queue(conversation_id, user_id=7)[0]
    assert queue_item['inputTurnId'] == queued['submittedTurn']['turnId']
    assert queue_item['outputTurnId'] == queued['turn']['turnId']
    assert queue_item['attemptId'] == queued['attempt']['attemptId']

    with pytest.raises(LifecycleNotFound):
        cancel_queued_turn_pair(conversation_id, queue_id, user_id=8)
    cancelled = cancel_queued_turn_pair(conversation_id, queue_id, user_id=7)
    assert cancelled['cancelled'] is True
    assert cancelled['inputTurn']['projection']['content'] == 'send this next'
    assert set(cancelled['deletedTurnIds']) == {
        queued['submittedTurn']['turnId'], queued['turn']['turnId'],
    }
    assert get_queue(conversation_id, user_id=7) == []
    remaining = list_turns(conversation_id, user_id=7)['turns']
    assert [turn['actor'] for turn in remaining] == ['human', 'assistant']


def test_queue_activation_reuses_the_pending_attempt(chat_sidecar):
    from lib.message_queue import get_queue
    from lib.turn_lifecycle import (
        activate_queued_turn_pair,
        bind_task,
        claim_attempt_start,
        create_turn_pair,
        record_task_event,
    )

    conversation_id = _new_conversation_id('queue-activate')
    active = _create(conversation_id, user_id=7)
    bind_task(active['attempt']['attemptId'], 'settle-before-activate', user_id=7)
    assert record_task_event({
        '_attemptId': active['attempt']['attemptId'],
        '_userId': 7,
        'id': 'settle-before-activate',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'done',
        'thinking': '',
        'toolRounds': [],
        'config': {'model': 'gpt-4o'},
    }, {'type': 'done', 'finishReason': 'stop'})
    queue_id = f'queue-{uuid.uuid4().hex}'
    queued = create_turn_pair(
        conversation_id,
        command_id=f'activate-{uuid.uuid4().hex}',
        input_projection={'content': 'same pair'},
        config={'model': 'gpt-4o'},
        user_id=7,
        queue_binding={'queueId': queue_id, 'message': {'text': 'same pair'}},
    )

    activated = activate_queued_turn_pair(
        conversation_id, queue_id, user_id=7,
    )
    assert activated['turn']['turnId'] == queued['turn']['turnId']
    assert activated['submittedTurn']['turnId'] == queued['submittedTurn']['turnId']
    assert activated['attempt']['attemptId'] == queued['attempt']['attemptId']
    assert 'queueBinding' not in activated['attempt']
    assert activated['_needsStart'] is True
    assert get_queue(conversation_id, user_id=7) == []
    assert claim_attempt_start(activated['attempt']['attemptId'], user_id=7) is True


def test_terminal_frame_overflow_falls_back_to_slim_settlement(
        chat_sidecar, monkeypatch):
    """A terminal frame rejected by the wire cap must still settle slim."""
    import threading

    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import bind_task, get_turn, record_task_event

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'facade-task', user_id=1)
    real_client = lifecycle._turn_client(write=True)

    class OverflowOnFullClient:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return getattr(real_client, name)

        def command(self, operation, payload, command_id, **kwargs):
            self.calls.append(dict(payload))
            if not payload.get('slim'):
                raise StorageError(
                    'database_protocol_error',
                    'Storage frame exceeds the size limit')
            return real_client.command(
                operation, payload, command_id, **kwargs)

    fake = OverflowOnFullClient()
    monkeypatch.setattr(lifecycle, '_turn_client', lambda **_kwargs: fake)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'facade-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
        'abort_event': threading.Event(),
    }
    task_event = {
        'task_id': 'facade-task',
        'sequence': 1,
        'event': {'type': 'done', 'finishReason': 'stop'},
    }

    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'},
        task_event=task_event) == 'carried'

    assert len(fake.calls) == 2
    assert fake.calls[0].get('slim') is not True
    assert fake.calls[1]['slim'] is True
    assert fake.calls[1]['terminal'] is True
    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['status'] == 'completed'
    assert settled['projection']['content'] == 'final answer'
    assert settled['settlement']
    assert not task.get('aborted')


def test_unwritable_even_slim_frame_signals_cooperative_abort(
        chat_sidecar, monkeypatch):
    """When even the text-only frame is rejected, the worker must stop."""
    import threading

    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import bind_task, record_task_event

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, 'facade-task', user_id=1)

    real_client = lifecycle._turn_client(write=True)

    class AlwaysOverflowClient:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def command(self, operation, payload, command_id, **kwargs):
            raise StorageError(
                'database_protocol_error',
                'Storage frame exceeds the size limit')

    monkeypatch.setattr(
        lifecycle, '_turn_client', lambda **_kwargs: AlwaysOverflowClient())
    abort_event = threading.Event()
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'facade-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
        'abort_event': abort_event,
    }
    task_event = {
        'task_id': 'facade-task',
        'sequence': 1,
        'event': {'type': 'done', 'finishReason': 'stop'},
    }

    with pytest.raises(StorageError, match='frame exceeds the size limit'):
        record_task_event(
            task, {'type': 'done', 'finishReason': 'stop'},
            task_event=task_event)

    assert task['aborted'] is True
    assert task['_abort_reason'] == 'storage_frame_overflow'
    assert abort_event.is_set()


def test_authority_integrity_failure_signals_cooperative_abort(
        chat_sidecar, monkeypatch):
    """A deterministic authority fault must not burn model rounds forever."""
    import threading

    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import bind_task, record_task_event

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, 'integrity-task', user_id=1)
    real_client = lifecycle._turn_client(write=True)

    class IntegrityFailureClient:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def command(self, operation, payload, command_id, **kwargs):
            raise StorageError(
                'database_integrity',
                'Turn projection checkpoint revision is inconsistent',
            )

    monkeypatch.setattr(
        lifecycle, '_turn_client', lambda **_kwargs: IntegrityFailureClient())
    abort_event = threading.Event()
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'integrity-task',
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
        'abort_event': abort_event,
    }

    with pytest.raises(StorageError, match='checkpoint revision'):
        record_task_event(
            task,
            {'type': 'phase', 'phase': 'preparing'},
            task_event={
                'task_id': 'integrity-task',
                'sequence': 1,
                'event': {'type': 'phase', 'phase': 'preparing'},
            },
        )

    assert task['aborted'] is True
    assert task['_abort_reason'] == 'storage_authority_integrity'
    assert abort_event.is_set()


def test_nonterminal_wire_frame_overflow_also_retries_slim(
        chat_sidecar, monkeypatch):
    """The 64 MiB wire-cap error shape gets the same slim retry as the
    sidecar payload cap (previously only storage_payload_too_large did)."""
    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import bind_task, get_turn, record_task_event

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = f'facade-tool-task-{conversation_id}'
    bind_task(attempt_id, task_id, user_id=1)
    real_client = lifecycle._turn_client(write=True)

    class OverflowOnFullClient:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return getattr(real_client, name)

        def command(self, operation, payload, command_id, **kwargs):
            self.calls.append(dict(payload))
            if not payload.get('slim'):
                raise StorageError(
                    'database_protocol_error',
                    'Storage frame exceeds the size limit')
            return real_client.command(
                operation, payload, command_id, **kwargs)

    fake = OverflowOnFullClient()
    monkeypatch.setattr(lifecycle, '_turn_client', lambda **_kwargs: fake)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': task_id,
        'status': 'running',
        'content': 'partial',
        'thinking': '',
        'toolRounds': [{
            'roundNum': 1,
            'toolCallId': 'call-1',
            'toolName': 'run_command',
            'toolArgs': {'command': 'echo hi'},
            'status': 'running',
        }],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    task_event = {
        'task_id': task_id,
        'sequence': 1,
        'event': {'type': 'tool_start', 'toolName': 'run_command'},
    }

    assert record_task_event(
        task, {'type': 'tool_start', 'toolName': 'run_command'},
        task_event=task_event)

    assert len(fake.calls) == 2
    assert 'projection_patch' in fake.calls[0]
    assert 'projection' not in fake.calls[0]
    assert 'projection' not in fake.calls[0]['event_payload']
    assert fake.calls[1]['slim'] is True
    assert 'projection_patch' not in fake.calls[1]
    assert 'projection' not in fake.calls[1]
    assert task['_turnProjectionOversizeCount'] == 1
    live = get_turn(conversation_id, turn_id, user_id=1)
    assert live['projection']['content'] == 'partial'


def test_live_projection_state_avoids_repeated_full_turn_reads(
        chat_sidecar, monkeypatch):
    import lib.turn_lifecycle as lifecycle
    from lib.turn_lifecycle import bind_task, record_task_event

    conversation_id = _new_conversation_id('projection-state')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'projection-state-task'
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_turnId': turn_id,
        '_userId': 1,
        'convId': conversation_id,
        'id': task_id,
        'status': 'running',
        'content': 'first',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'config': {'model': 'gpt-4o'},
    }
    reads = {'attempt': 0, 'turn': 0}
    real_get_attempt = lifecycle.get_attempt
    real_get_turn = lifecycle.get_turn

    def counted_attempt(*args, **kwargs):
        reads['attempt'] += 1
        return real_get_attempt(*args, **kwargs)

    def counted_turn(*args, **kwargs):
        reads['turn'] += 1
        return real_get_turn(*args, **kwargs)

    monkeypatch.setattr(lifecycle, 'get_attempt', counted_attempt)
    monkeypatch.setattr(lifecycle, 'get_turn', counted_turn)

    assert record_task_event(task, {'type': 'phase', 'phase': 'working'}) is True
    task['content'] = 'first second'
    assert record_task_event(task, {'type': 'tool_start'}) is True

    assert reads == {'attempt': 1, 'turn': 1}
    state = task['_turnProjectionState']
    assert state['turnId'] == turn_id
    assert state['projection']['content'] == 'first second'


def test_live_projection_state_rebases_once_without_losing_external_fields(
        chat_sidecar):
    from lib.turn_lifecycle import (
        bind_task,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('projection-rebase')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'projection-rebase-task'
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_turnId': turn_id,
        '_userId': 1,
        'convId': conversation_id,
        'id': task_id,
        'status': 'running',
        'content': 'before',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'phase', 'phase': 'working'}) is True

    external_todo = {
        'items': [{'id': 'external', 'content': 'preserve me'}],
    }
    concurrent_task = {**task, 'todoState': external_todo}
    concurrent_task.pop('_turnProjectionState', None)
    concurrent_task.pop('_turnProjectionStateLock', None)
    assert record_task_event(
        concurrent_task, {'type': 'tool_start', 'toolName': 'todo_write'},
    ) is True

    task['content'] = 'after external update'
    assert record_task_event(task, {'type': 'tool_start'}) is True

    after = get_turn(conversation_id, turn_id, user_id=1)
    assert after['projection']['content'] == 'after external update'
    assert after['projection']['todoState'] == external_todo
    assert task['_turnProjectionState']['projectionRevision'] == (
        after['projectionRevision'])


def test_live_projection_state_lock_serializes_parallel_event_folds(
        monkeypatch):
    import threading
    import time

    import lib.turn_lifecycle as lifecycle

    task = {}
    start = threading.Barrier(3)
    counters = {'active': 0, 'peak': 0}
    counters_lock = threading.Lock()
    results = []

    def observed_fold(*_args, **_kwargs):
        with counters_lock:
            counters['active'] += 1
            counters['peak'] = max(counters['peak'], counters['active'])
        time.sleep(0.03)
        with counters_lock:
            counters['active'] -= 1
        return True

    def worker(event_kind):
        start.wait()
        results.append(lifecycle.record_task_event(
            task, {'type': event_kind}))

    monkeypatch.setattr(lifecycle, '_record_task_event_locked', observed_fold)
    threads = [
        threading.Thread(target=worker, args=('tool_start',)),
        threading.Thread(target=worker, args=('tool_result',)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True]
    assert counters == {'active': 0, 'peak': 1}

def test_terminal_projection_and_attempt_cas_share_one_turn(chat_sidecar):
    from lib.turn_lifecycle import (
        LifecycleConflict,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'facade-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'facade-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['status'] == 'completed'
    assert settled['projection']['content'] == 'final answer'

    with pytest.raises(LifecycleConflict) as stale:
        create_attempt(
            conversation_id, turn_id,
            command_id=f'stale-{conversation_id}',
            operation='regenerate',
            expected_projection_revision=settled['projectionRevision'] - 1,
            user_id=1,
        )
    assert stale.value.code == 'stale_projection'

    regenerated = create_attempt(
        conversation_id, turn_id,
        command_id=f'regenerate-{conversation_id}',
        operation='regenerate',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    assert regenerated['turn']['turnId'] == turn_id
    assert regenerated['attempt']['attemptId'] != attempt_id

    task.update(status='running', content='stale overwrite')
    assert record_task_event(
        task, {'type': 'delta', 'content': 'stale overwrite'}) is False
    assert get_turn(conversation_id, turn_id, user_id=1)['projection']['content'] == ''


def test_checkpoint_resume_freezes_previous_round_execution_identity(chat_sidecar):
    """Attempt switch backfills ownership and filters transport artifacts."""
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
        update_turn_projection,
    )
    from lib.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD

    conversation_id = _new_conversation_id('round-owner')
    created = _create(conversation_id)
    old_attempt_id = created['attempt']['attemptId']
    old_task_id = 'round-owner-task'
    turn_id = created['turn']['turnId']
    bind_task(old_attempt_id, old_task_id, user_id=1)
    task = {
        '_attemptId': old_attempt_id,
        '_userId': 1,
        'id': old_task_id,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [
            {
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'discarded-before-restart',
                'toolName': 'search_tools',
                'toolArgs': {'query': 'discarded'},
                'toolContent': None, 'status': 'aborted',
                'results': [{'badge': 'superseded', 'fetched': False,
                             'fetchedChars': 0}],
                SUPERSEDED_PROVIDER_ATTEMPT_FIELD: True,
            },
            {
                'roundNum': 2,
                'llmRound': 0,
                'toolCallId': 'call-before-restart',
                'toolName': 'search_tools',
                'toolArgs': {'query': 'artifact'},
                'toolContent': 'provider failed after one match',
                # Failure is a verdict, not absence of the execution receipt.
                'status': 'error',
                'assistantContent': 'I will search.',
            },
        ],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(old_attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert any(
        option['operation'] == 'checkpoint_resume'
        for option in settled['settlement']['resumeOptions']
    )
    checkpoint_anchor = next(
        option['anchor']
        for option in settled['settlement']['resumeOptions']
        if option['operation'] == 'checkpoint_resume'
    )
    assert checkpoint_anchor['keptToolRounds'] == 2
    assert checkpoint_anchor['replayableToolRounds'] == 1
    assert checkpoint_anchor['retainedToolRoundPositions'] == [1]
    from lib.storage import StorageError
    with pytest.raises(StorageError) as malformed_anchor:
        create_attempt(
            conversation_id,
            turn_id,
            command_id=f'malformed-anchor-{conversation_id}',
            operation='checkpoint_resume',
            expected_projection_revision=settled['projectionRevision'],
            resume_anchor=[],  # type: ignore[arg-type]
            user_id=1,
        )
    assert malformed_anchor.value.code == 'database_protocol_error'
    # Simulate a row written before execution identity became public. The
    # generic projection editor cannot infer ownership; create_attempt can.
    legacy_projection = dict(settled['projection'])
    legacy_projection['toolRounds'] = [
        {key: value for key, value in round_record.items()
         if key not in {'attemptId', 'taskId'}}
        for round_record in legacy_projection['toolRounds']
    ]
    legacy_projection['segments'] = [
        {key: value for key, value in segment.items()
         if key not in {'attemptId', 'taskId'}}
        for segment in legacy_projection['segments']
    ]
    update_turn_projection(
        conversation_id,
        turn_id,
        projection=legacy_projection,
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    legacy = get_turn(conversation_id, turn_id, user_id=1)

    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=legacy['projectionRevision'],
        user_id=1,
    )
    retained_rounds = resumed['turn']['projection']['toolRounds']
    assert [item['toolCallId'] for item in retained_rounds] == [
        'call-before-restart',
    ]
    round_record = retained_rounds[0]
    assert round_record['attemptId'] == old_attempt_id
    assert round_record['taskId'] == old_task_id
    assert round_record['status'] == 'error'
    # checkpoint_resume intentionally rebuilds segments from the retained
    # rounds on the successor's first projection fold.
    assert resumed['turn']['projection']['segments'] == []
    assert resumed['attempt']['attemptId'] != old_attempt_id


def _running_task(attempt_id, task_id, *, model='gpt-4o', **overrides):
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': task_id,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [{
            'roundNum': 1, 'llmRound': 0,
            'toolCallId': 'call-1', 'toolName': 'run_command',
            'toolArgs': {'command': 'pwd'}, 'toolContent': '/repo',
            'status': 'done',
        }],
        'model': model,
        'config': {'model': model},
    }
    task.update(overrides)
    return task


def test_checkpoint_resume_preserves_interrupted_tail_as_rolled_back(
        chat_sidecar):
    """The rewound content/thinking tail survives as a rolledBack block.

    Root-cause fix: the checkpoint anchor used to seed the successor
    projection with the interrupted tail, displaying text the resumed model
    never generated and then wiping it. Terminal lanes now restart empty and
    the tail moves into ``projection.rolledBack``.
    """
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('rolled-back')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'rolled-back-task'
    bind_task(attempt_id, task_id, user_id=1)
    task = _running_task(
        attempt_id, task_id,
        content='Nearly finished prose',
        thinking='interrupted reasoning tail',
    )
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    options = {
        option['operation']: option
        for option in settled['settlement']['resumeOptions']
    }
    # Prefill-capable model with a prose tail: lossless continue leads.
    assert 'continue' in options
    checkpoint = options['checkpoint_resume']['anchor']
    assert checkpoint['content'] == ''
    assert checkpoint['thinking'] == ''

    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    projection = resumed['turn']['projection']
    assert projection['content'] == ''
    assert projection['thinking'] == ''
    assert [item['toolCallId'] for item in projection['toolRounds']] == [
        'call-1']
    assert [item['blockId'] for item in projection['rolledBack']] == [
        f'rolled-back:{attempt_id}']
    entry = projection['rolledBack'][0]
    assert entry['content'] == 'Nearly finished prose'
    assert entry['thinking'] == 'interrupted reasoning tail'
    assert entry['attemptId'] == attempt_id
    assert entry['at'] > 0


def test_replay_only_continue_preserves_thinking_tail_as_rolled_back(
        chat_sidecar):
    """Empty-prose interrupted turn: continue needs no prefill capability.

    The replayed checkpoint prefix is the wire continuity; only the thinking
    tail rolls back (the resumed model re-thinks), preserved as rolledBack.
    """
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('replay-only')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'replay-only-task'
    bind_task(attempt_id, task_id, user_id=1)
    # claude-opus-4-7 rejects assistant prefill: a continue offer here proves
    # the replay-only path is capability-independent.
    task = _running_task(
        attempt_id, task_id,
        model='claude-opus-4-7',
        thinking='reasoning before the interruption',
    )
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    options = {
        option['operation']: option
        for option in settled['settlement']['resumeOptions']
    }
    assert options['continue']['anchor']['type'] == 'replay_only'

    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='continue',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    projection = resumed['turn']['projection']
    assert projection['content'] == ''
    assert projection['thinking'] == ''
    assert projection['rolledBack'][0]['thinking'] == (
        'reasoning before the interruption')
    # A replay-only continue rewrites nothing else: rounds stay displayed.
    assert [item['toolCallId'] for item in projection['toolRounds']] == [
        'call-1']


def test_lossless_continue_keeps_terminal_lanes_seamless(chat_sidecar):
    """A prefill continue rewrites nothing: no rolledBack lane appears."""
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('seamless')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'seamless-task'
    bind_task(attempt_id, task_id, user_id=1)
    task = _running_task(
        attempt_id, task_id,
        content='partial answer',
        thinking='interrupted reasoning tail',
    )
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='continue',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    projection = resumed['turn']['projection']
    assert projection['content'] == 'partial answer'
    assert projection['thinking'] == 'interrupted reasoning tail'
    assert 'rolledBack' not in projection


def test_rolled_back_normalization_is_fail_closed():
    from lib.turn_projection_patch import normalize_projection_document

    normalized = normalize_projection_document({
        'content': '',
        'rolledBack': [
            'not-an-object',
            {'blockId': 'rolled-back:x'},  # no lane text → dropped
            {'blockId': 'b1', 'content': 'kept', 'unknown': 'stripped',
             'at': 'not-an-int'},
            {'thinking': 'kept too', 'attemptId': 7},
        ],
    })
    assert [item['blockId'] for item in normalized['rolledBack']] == [
        'b1', 'rolled-back']
    first, second = normalized['rolledBack']
    assert first == {'blockId': 'b1', 'content': 'kept'}
    assert second == {
        'blockId': 'rolled-back', 'thinking': 'kept too', 'attemptId': '7'}
    assert 'rolledBack' not in normalize_projection_document({
        'content': '', 'rolledBack': [{'blockId': 'empty'}]})


def test_settlement_offers_answer_guidance_for_unanswered_ask_human_tail(
        chat_sidecar):
    """A turn that died inside ask_human offers the late-answer resume.

    The offered operation is accepted with a bounded durable answer config;
    the projection itself is not rewritten — the answer becomes an executor
    resume authority, not a settlement fabrication.
    """
    from lib.storage import StorageError
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('hg-answer')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    task_id = 'hg-answer-task'
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': task_id,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [
            {
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'call-search',
                'toolName': 'search_tools',
                'toolArgs': {'query': 'scope'},
                'toolContent': 'one match',
                'status': 'done',
            },
            {
                'roundNum': 2, 'llmRound': 1,
                'toolCallId': 'call-ask',
                'toolName': 'ask_human',
                'toolArgs': {'question': 'Which scope?'},
                'toolContent': None,
                'status': 'awaiting_human',
                'guidanceId': 'hg-1',
                'guidanceQuestion': 'Which scope?',
                'guidanceType': 'free_text',
            },
        ],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    options = settled['settlement']['resumeOptions']
    answer = next(
        (option for option in options if option['operation'] == 'answer_guidance'),
        None,
    )
    assert answer is not None
    anchor = answer['anchor']
    assert anchor['type'] == 'human_guidance'
    assert anchor['guidanceId'] == 'hg-1'
    assert anchor['toolCallId'] == 'call-ask'
    assert anchor['question'] == 'Which scope?'
    assert anchor['responseType'] == 'free_text'
    assert anchor['roundPosition'] == 1
    # The replayable prefix before the question still offers checkpoint_resume.
    assert any(option['operation'] == 'checkpoint_resume' for option in options)

    answer_config = {
        '_humanGuidanceAnswer': {
            'guidanceId': 'hg-1',
            'toolCallId': 'call-ask',
            'response': '仅服务商侧改造',
        },
    }
    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'answer-{conversation_id}',
        operation='answer_guidance',
        expected_projection_revision=settled['projectionRevision'],
        config=answer_config,
        user_id=1,
    )
    assert resumed['attempt']['attemptId'] != attempt_id
    projection_rounds = resumed['turn']['projection']['toolRounds']
    assert projection_rounds[1]['status'] == 'awaiting_human'

    settled_again = get_turn(conversation_id, turn_id, user_id=1)
    with pytest.raises(StorageError) as missing_answer:
        create_attempt(
            conversation_id,
            turn_id,
            command_id=f'answer-missing-{conversation_id}',
            operation='answer_guidance',
            expected_projection_revision=settled_again['projectionRevision'],
            config={},
            user_id=1,
        )
    assert missing_answer.value.code == 'database_protocol_error'
    with pytest.raises(StorageError) as misplaced_answer:
        create_attempt(
            conversation_id,
            turn_id,
            command_id=f'answer-misplaced-{conversation_id}',
            operation='continue',
            expected_projection_revision=settled_again['projectionRevision'],
            config=answer_config,
            user_id=1,
        )
    assert misplaced_answer.value.code == 'database_protocol_error'


def test_checkpoint_resume_retains_display_rows_and_amputates_in_flight_tail(
        chat_sidecar):
    """Display carriers survive a checkpoint; the result-less tail does not."""
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('display-keep')
    created = _create(conversation_id)
    old_attempt_id = created['attempt']['attemptId']
    old_task_id = 'display-keep-task'
    turn_id = created['turn']['turnId']
    bind_task(old_attempt_id, old_task_id, user_id=1)
    task = {
        '_attemptId': old_attempt_id,
        '_userId': 1,
        'id': old_task_id,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [
            {
                # Program-shell/display carrier: never dispatched, no id.
                'roundNum': 1, 'llmRound': 0,
                'toolName': 'execute_tools',
                'toolArgs': {'calls': []},
                'status': 'done',
            },
            {
                'roundNum': 2, 'llmRound': 0,
                'toolCallId': 'call-kept',
                'toolName': 'run_command',
                'toolArgs': {'command': 'pwd'},
                'toolContent': '/repo',
                'status': 'done',
            },
            {
                # Interrupted mid-dispatch: identity but no result.
                'roundNum': 3, 'llmRound': 1,
                'toolCallId': 'call-in-flight',
                'toolName': 'run_command',
                'toolArgs': {'command': 'sleep 60'},
                'toolContent': None,
                'status': 'running',
            },
        ],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(old_attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    checkpoint_anchor = next(
        option['anchor']
        for option in settled['settlement']['resumeOptions']
        if option['operation'] == 'checkpoint_resume'
    )
    assert checkpoint_anchor['keptToolRounds'] == 2
    assert checkpoint_anchor['replayableToolRounds'] == 1
    assert checkpoint_anchor['retainedToolRoundPositions'] == [0, 1]

    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    retained_rounds = resumed['turn']['projection']['toolRounds']
    assert [item.get('toolCallId') for item in retained_rounds] == [
        None, 'call-kept',
    ]
    assert resumed['attempt']['attemptId'] != old_attempt_id


def test_checkpoint_resume_bootstrap_event_is_exempt_from_streaming_cap(chat_sidecar):
    """A large retained history must not make checkpoint_resume unwritable.

    The seq=1 bootstrap patch carries the whole rebuilt toolRounds lane; for a
    turn with megabytes of retained tool output it exceeds the 4 MiB
    non-terminal transport cap. Like the terminal settlement, this one-shot
    replay record is exempt — otherwise resume fails with 413 while
    regenerate keeps working (mtd46qic0iy98e, 2026-08-29).
    """
    import json

    from lib.storage_sidecar.operations_pkg._turns import (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES,
    )
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        read_events,
        record_task_event,
        update_turn_projection,
    )
    from lib.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD

    conversation_id = _new_conversation_id('resume-oversize')
    created = _create(conversation_id)
    old_attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(old_attempt_id, 'resume-oversize-task', user_id=1)
    half = _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES // 2 + 4096
    big_a = 'a' * half
    big_b = 'b' * half

    def _round(round_num, call_id, content):
        return {
            'roundNum': round_num, 'llmRound': 0,
            'toolCallId': call_id,
            'toolName': 'read_files',
            'toolArgs': {'path': f'{call_id}.log'},
            'toolContent': content,
            'status': 'error',
        }

    task = {
        '_attemptId': old_attempt_id,
        '_userId': 1,
        'id': 'resume-oversize-task',
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [
            {
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'superseded-round',
                'toolName': 'search_tools',
                'toolArgs': {'query': 'discarded'},
                'toolContent': None, 'status': 'aborted',
                'results': [{'badge': 'superseded', 'fetched': False,
                             'fetchedChars': 0}],
                SUPERSEDED_PROVIDER_ATTEMPT_FIELD: True,
            },
            _round(2, 'retained-big-round-a', 'small a'),
            _round(3, 'retained-big-round-b', 'small b'),
        ],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'tool_result'}) is True
    assert abort_attempt(old_attempt_id, user_id=1)['status'] == 'abort_signaled'

    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert any(
        option['operation'] == 'checkpoint_resume'
        for option in settled['settlement']['resumeOptions']
    )

    # Grow the retained history past the streaming cap through the generic
    # projection editor (no attempt event on that seam): this mirrors an old
    # large turn whose authority accumulated below per-frame limits.
    inflated = dict(settled['projection'])
    inflated['toolRounds'] = [
        {
            **round_record,
            'toolContent': (
                big_a
                if round_record.get('toolCallId') == 'retained-big-round-a'
                else big_b
                if round_record.get('toolCallId') == 'retained-big-round-b'
                else round_record.get('toolContent')
            ),
        }
        for round_record in inflated['toolRounds']
    ]
    update_turn_projection(
        conversation_id,
        turn_id,
        projection=inflated,
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    grown = get_turn(conversation_id, turn_id, user_id=1)

    # The retained lane differs from the stored one (superseded round
    # filtered), so the bootstrap patch inlines the full >4 MiB lane.
    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=grown['projectionRevision'],
        user_id=1,
    )
    retained = resumed['turn']['projection']['toolRounds']
    assert [item['toolCallId'] for item in retained] == [
        'retained-big-round-a', 'retained-big-round-b',
    ]
    assert [item['toolContent'] for item in retained] == [big_a, big_b]

    bootstrap = read_events(resumed['attempt']['attemptId'], user_id=1)
    assert bootstrap[0]['type'] == 'status_changed'
    assert len(json.dumps(bootstrap[0], default=str)) > (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES)


def test_checkpoint_resume_preserves_ambiguous_legacy_attempt_history(chat_sidecar):
    """Several unstamped legacy attempts must not all become the latest one."""
    from lib.turn_lifecycle import (
        abort_attempt,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
        update_turn_projection,
    )

    conversation_id = _new_conversation_id('ambiguous-round-owner')
    created = _create(conversation_id)
    turn_id = created['turn']['turnId']

    def task_for(attempt_id, task_id, rounds, checkpoint=()):
        return {
            '_attemptId': attempt_id,
            '_userId': 1,
            'id': task_id,
            'status': 'running',
            'content': '',
            'thinking': '',
            '_checkpointToolRounds': list(checkpoint),
            'toolRounds': list(rounds),
            'model': 'gpt-4o',
            'config': {'model': 'gpt-4o'},
        }

    first_attempt_id = created['attempt']['attemptId']
    bind_task(first_attempt_id, 'legacy-task-one', user_id=1)
    first_round = {
        'roundNum': 1, 'llmRound': 0, 'toolCallId': 'legacy-call-one',
        'toolName': 'search_tools', 'toolArgs': {}, 'toolContent': 'one',
        'status': 'done', 'assistantContent': 'first attempt',
    }
    assert record_task_event(
        task_for(first_attempt_id, 'legacy-task-one', [first_round]),
        {'type': 'tool_result'},
    ) is True
    assert abort_attempt(first_attempt_id, user_id=1)['status'] == 'abort_signaled'
    first_settled = get_turn(conversation_id, turn_id, user_id=1)

    second = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'second-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=first_settled['projectionRevision'],
        user_id=1,
    )
    second_attempt_id = second['attempt']['attemptId']
    bind_task(second_attempt_id, 'legacy-task-two', user_id=1)
    second_round = {
        'roundNum': 2, 'llmRound': 0, 'toolCallId': 'legacy-call-two',
        'toolName': 'read_tool_artifact', 'toolArgs': {}, 'toolContent': 'two',
        'status': 'done', 'assistantContent': 'second attempt',
    }
    assert record_task_event(
        task_for(
            second_attempt_id,
            'legacy-task-two',
            [second_round],
            second['turn']['projection']['toolRounds'],
        ),
        {'type': 'tool_result'},
    ) is True
    assert abort_attempt(second_attempt_id, user_id=1)['status'] == 'abort_signaled'
    second_settled = get_turn(conversation_id, turn_id, user_id=1)

    # Simulate an old persisted projection whose two attempt boundaries were
    # never public. No exact owner can be reconstructed from this payload.
    legacy_projection = dict(second_settled['projection'])
    legacy_projection['toolRounds'] = [
        {key: value for key, value in round_record.items()
         if key not in {'attemptId', 'taskId'}}
        for round_record in legacy_projection['toolRounds']
    ]
    update_turn_projection(
        conversation_id,
        turn_id,
        projection=legacy_projection,
        expected_projection_revision=second_settled['projectionRevision'],
        user_id=1,
    )
    legacy = get_turn(conversation_id, turn_id, user_id=1)

    third = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'third-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=legacy['projectionRevision'],
        user_id=1,
    )
    retained = third['turn']['projection']['toolRounds']
    assert [item['toolCallId'] for item in retained] == [
        'legacy-call-one', 'legacy-call-two',
    ]
    assert all('attemptId' not in item and 'taskId' not in item
               for item in retained)


@pytest.mark.parametrize(
    ('task_error', 'raw_event', 'expected_message'),
    [
        ('provider socket closed', {'type': 'error'}, 'provider socket closed'),
        (None, {'type': 'error', 'error': {
            'message': 'executor exited before reply',
            'kind': 'executor_failure',
        }}, 'executor exited before reply'),
    ],
)
def test_failed_settlement_always_has_actionable_error(
    task_error, raw_event, expected_message,
):
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'error',
        'error': task_error,
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    status, settlement = _settlement(
        task, raw_event, {'content': 'partial', 'toolRounds': []})
    assert status == 'failed'
    error = settlement['error']
    assert error['kind']
    assert expected_message in (error['detail'] or error['raw'])
    assert isinstance(error['retryable'], bool)
    assert error['hint']


@pytest.mark.parametrize('finish_reason', ['premature_close', 'abnormal_stop'])
def test_provider_stream_failure_never_settles_as_completed(finish_reason):
    """Missing terminal stream frames are provider failures even when the
    executor preserved prose and emitted a nominal ``done`` event."""
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'done',
        'finishReason': finish_reason,
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    status, settlement = _settlement(
        task,
        {'type': 'done', 'finishReason': finish_reason},
        {'content': 'preserved partial', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == 'provider_stream_error'
    assert settlement['providerFinishReason'] == finish_reason
    assert settlement['error']['kind'] == finish_reason
    assert any(option['operation'] == 'continue'
               for option in settlement['resumeOptions'])


def test_malformed_stream_prefix_and_verdict_commit_together(chat_sidecar):
    """The authority persists the exact parsed prefix and the failed stream
    verdict atomically; storage must neither truncate text nor relabel it as a
    completed provider response."""
    from lib.turn_lifecycle import bind_task, get_turn, record_task_event

    conversation_id = _new_conversation_id('malformed-stream')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'malformed-stream-task', user_id=1)
    prefix = 'The diagnosis is solid. Let me pull the exact pending question'
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'malformed-stream-task',
        'status': 'done',
        'finishReason': 'premature_close',
        'streamState': 'malformed_stream',
        'content': prefix,
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }

    assert record_task_event(task, {
        'type': 'done',
        'finishReason': 'premature_close',
        'streamState': 'malformed_stream',
    }) is True

    turn = get_turn(conversation_id, turn_id, user_id=1)
    assert turn['status'] == 'failed'
    assert turn['projection']['content'] == prefix
    assert turn['settlement']['streamState'] == 'malformed_stream'
    assert turn['settlement']['evidence'] == 'provider_stream_failure'
    assert turn['settlement']['error']['kind'] == 'premature_close'


def test_projection_sanitizes_transport_only_round_fields():
    from lib.turn_lifecycle import _task_projection

    projected = _task_projection({
        "id": "task-a",
        'content': 'answer',
        'thinking': '',
        'toolRounds': [{'toolName': 'read_files', 'status': 'done'}],
        'apiRounds': [{
            'model': 'gpt-4o',
            'usage': {
                'input_tokens': 10,
                '_wire_fp': 'not durable',
                '_wire_bytes': {'huge': 'not durable'},
            },
        }],
        'config': {},
    }, {})
    assert projected['content'] == 'answer'
    assert projected['toolRounds'][0]['toolName'] == 'read_files'
    usage = projected['apiRounds'][0]['usage']
    assert usage['input_tokens'] == 10
    assert not any(key.startswith('_wire_') for key in usage)


def test_projection_exposes_file_changes_as_one_stable_content_block():
    from lib.turn_lifecycle import _task_projection

    files = [{"path": "src/app.ts", "action": "modified", "root": "web"}]
    projected = _task_projection({
        "id": "task-a",
        "content": "done",
        "modifiedFiles": 1,
        "modifiedFileList": files,
        "config": {},
    }, {})

    assert projected["fileChanges"] == {
        "blockId": "file-changes",
        "taskId": "task-a",
        "count": 1,
        "state": "applied",
        "files": files,
    }
    assert projected["fileChanges"]["files"] is not files
    assert projected["fileChanges"]["files"][0] is not files[0]

    undone = {**projected, "fileChanges": {
        **projected["fileChanges"], "state": "undone", "commandId": "cmd-a",
    }}
    late = _task_projection({
        "id": "task-a",
        "content": "done",
        "modifiedFiles": 1,
        "modifiedFileList": files,
        "_preferencesLearned": [{"kind": "added"}],
        "config": {},
    }, undone)
    assert late["fileChanges"]["state"] == "undone"
    assert late["fileChanges"]["commandId"] == "cmd-a"


def test_commit_round_file_changes_fold_into_a_settled_turn(chat_sidecar):
    """The async commit round derives the file list AFTER the terminal
    settlement, so the done-time projection lacks it and the authority
    rightly refuses the late ``round_committed`` frame.  The dedicated CAS
    seam must still land the list on the durable projection — otherwise the
    turn-native UI never renders the files-changed card (2026-08-26
    regression)."""
    from lib.turn_lifecycle import (
        apply_commit_round_file_changes,
        bind_task,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('commit-round-fold')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'commit-round-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'commit-round-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['status'] == 'completed'
    assert 'fileChanges' not in settled['projection']

    files = [{'path': 'src/app.ts', 'action': 'written'}]
    # The post-settlement event frame stays refused — only the CAS seam folds.
    assert record_task_event(
        task, {'type': 'round_committed', 'modifiedFileList': files}) is False

    result = apply_commit_round_file_changes(
        conversation_id, turn_id,
        files=files, modified_count=1, task_id='commit-round-task', user_id=1)
    assert result is not None

    folded = get_turn(conversation_id, turn_id, user_id=1)
    assert folded['projectionRevision'] == settled['projectionRevision'] + 1
    assert folded['projection']['content'] == 'final answer'
    assert folded['projection']['modifiedFileList'] == files
    assert folded['projection']['fileChanges'] == {
        'blockId': 'file-changes',
        'taskId': 'commit-round-task',
        'count': 1,
        'state': 'applied',
        'files': files,
    }

    # Idempotent: the identical fold (e.g. a duplicate commit-round retry) is
    # a no-op that does not burn a projection revision.
    assert apply_commit_round_file_changes(
        conversation_id, turn_id,
        files=files, modified_count=1, task_id='commit-round-task',
        user_id=1) is None
    assert (get_turn(conversation_id, turn_id, user_id=1)['projectionRevision']
            == folded['projectionRevision'])


def test_projection_merges_provenance_sidecars_behind_one_stable_block():
    from lib.turn_lifecycle import _task_projection

    learned = [{"kind": "added", "summary": "Prefer focused tests"}]
    first = _task_projection({
        "content": "answer",
        "_memoryPrefetch": {"phase": "done", "selected": 2},
        "_preferencesApplied": {"chars": 30, "items": ["concise"]},
        "config": {},
    }, {})
    second = _task_projection({
        "content": "answer",
        "_preferencesLearned": learned,
        "config": {},
    }, first)

    assert second["provenance"] == {
        "blockId": "provenance",
        "memoryPrefetch": {"phase": "done", "selected": 2},
        "preferencesApplied": {"chars": 30, "items": ["concise"]},
        "preferencesLearned": learned,
    }
    assert second["provenance"]["preferencesLearned"] is not learned


def test_projection_folds_mcp_delta_and_path_change_provenance():
    """The MCP-wire delta and project-path-change transition chips ride the
    same task-sidecar → turn-provenance lane as memory prefetch."""
    from lib.turn_lifecycle import _task_projection

    projected = _task_projection({
        "content": "answer",
        "_mcpToolsDelta": {
            "added": ["mcp__docs__write"], "removed": [], "total": 1},
        "_projectPathChange": {"from": "/a", "to": "/b"},
        "config": {},
    }, {})

    assert projected["provenance"]["mcpToolsDelta"] == {
        "added": ["mcp__docs__write"], "removed": [], "total": 1}
    assert projected["provenance"]["projectPathChange"] == {
        "from": "/a", "to": "/b"}
    # Steady-state task (no sidecars) must not resurrect the chips.
    steady = _task_projection({"content": "answer", "config": {}}, {})
    assert "mcpToolsDelta" not in (steady.get("provenance") or {})
    assert "projectPathChange" not in (steady.get("provenance") or {})

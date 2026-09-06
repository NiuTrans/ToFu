"""Contracts for the shared chat-task error terminal boundary."""

import time

import pytest

from lib.tasks_pkg.manager._terminal import (
    finalize_chat_task_aborted,
    finalize_chat_task_error,
    reject_unstarted_chat_task,
    stamp_chat_task_terminal,
)


pytestmark = pytest.mark.unit


def test_error_terminal_updates_state_event_persistence_and_busy_projection():
    events = []
    persisted = []
    notified = []
    task = {
        'id': 'terminal-task-0001',
        'status': 'running',
        'flow_mode': True,
        '_flow_phase': 'working',
        'model': 'test-model',
    }
    envelope = {'kind': 'bad_request', 'message': 'missing flow'}

    event = finalize_chat_task_error(
        task,
        envelope,
        flow_reason='definition_unavailable',
        append_event_fn=lambda owner, item: events.append((owner, item)),
        persist_task_result_fn=lambda owner: persisted.append(owner),
        notify_terminal_fn=lambda owner: notified.append(owner),
    )

    assert task['status'] == 'error'
    assert task['finishReason'] == 'error'
    assert task['_flow_phase'] == 'done'
    assert task['_flow_stop_reason'] == 'definition_unavailable'
    assert task['finished_at'] > 0
    assert event['type'] == 'done'
    assert event['finishReason'] == 'error'
    assert event['error'] is envelope
    assert events == [(task, event)]
    assert persisted == [task]
    assert notified == [task]


def test_error_terminal_attempts_persistence_when_event_delivery_fails():
    persisted = []
    notified = []

    def fail_event(_task, _event):
        raise RuntimeError('push unavailable')

    finalize_chat_task_error(
        {'id': 'terminal-task-0002', 'status': 'running'},
        {'kind': 'internal', 'message': 'failure'},
        append_event_fn=fail_event,
        persist_task_result_fn=lambda owner: persisted.append(owner),
        notify_terminal_fn=lambda owner: notified.append(owner),
    )

    assert len(persisted) == 1
    assert notified == persisted


def test_terminal_stamp_is_idempotent_and_rejects_outcome_rewrites():
    task = {'id': 'terminal-task-0003', 'status': 'running'}

    assert stamp_chat_task_terminal(
        task, status='done', finish_reason='stop',
        flow_reason='verified_complete',
    ) is True
    finished_at = task['finished_at']
    assert stamp_chat_task_terminal(
        task, status='done', finish_reason='stop',
        flow_reason='verified_complete',
    ) is False
    assert stamp_chat_task_terminal(
        task, status='error', finish_reason='error', flow_reason='fatal',
    ) is False

    assert task['status'] == 'done'
    assert task['finishReason'] == 'stop'
    assert task['_flow_stop_reason'] == 'verified_complete'
    assert task['finished_at'] == finished_at


def test_normal_orchestrator_finalizer_stamps_before_persist(monkeypatch):
    """Successful root chat persists only after its immutable finish clock."""
    from lib.tasks_pkg.orchestrator import _finalize

    class PersistReached(RuntimeError):
        pass

    observed = []
    real_stamp = _finalize.stamp_chat_task_terminal

    def observe_stamp(task, **kwargs):
        observed.append('stamp')
        return real_stamp(task, **kwargs)

    def observe_persist(task, *, _defer_heavy_release=False):
        assert _defer_heavy_release is True
        assert task['finished_at'] > 0
        observed.append('persist')
        raise PersistReached

    monkeypatch.setattr(_finalize, 'stamp_chat_task_terminal', observe_stamp)
    monkeypatch.setattr(_finalize, 'persist_task_result', observe_persist)
    monkeypatch.setattr(_finalize, '_maybe_auto_retry_turn', lambda *_a: False)
    monkeypatch.setattr(
        _finalize, '_settle_post_loop_finish_reason',
        lambda *_a, **_k: 'stop',
    )
    monkeypatch.setattr(
        _finalize, '_finalize_dangling_tool_rounds', lambda *_a: None)
    monkeypatch.setattr(
        _finalize, '_maybe_append_sources_footer', lambda *_a: None)
    monkeypatch.setattr(
        _finalize, '_salvage_undelivered_steer', lambda *_a: None)
    monkeypatch.setattr(
        _finalize, 'cleanup_stale_cache_states', lambda **_k: None)
    monkeypatch.setattr(
        _finalize, '_check_suspicious_completion', lambda *_a, **_k: [])
    monkeypatch.setattr(
        _finalize, '_maybe_preserve_accumulated_on_suspicion',
        lambda *_a: None,
    )
    monkeypatch.setattr(
        _finalize, '_build_done_event_base',
        lambda *_a, **_k: {'type': 'done', 'finishReason': 'stop'},
    )
    import lib
    monkeypatch.setattr(lib, 'ARTIFACTS_ENABLED', False)

    task = {
        'id': 'terminal-normal-finalizer',
        'convId': '',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'content': 'answer',
        'thinking': '',
        'error': None,
        'toolRounds': [],
        'config': {},
        'created_at': time.time() - 7_200,
    }
    with pytest.raises(PersistReached):
        _finalize._finalize_and_emit_done(
            task,
            model='model',
            preset='medium',
            thinking_depth=None,
            cfg={},
            last_finish_reason='stop',
            last_usage={},
            last_stream_result=None,
            accumulated_usage={},
            api_rounds=[],
            tool_call_happened=False,
            messages=[],
            original_messages=[],
            all_search_results_text='',
            max_tokens=128,
            thinking_enabled=False,
            temperature=1.0,
            _loop_exit_reason='stop',
            _abort_detected_phase='',
            project_path='',
            project_enabled=False,
            round_num=0,
            assistant_msg={'role': 'assistant', 'content': 'answer'},
        )

    assert observed == ['stamp', 'persist']
    assert task['status'] == 'done'
    assert task['finishReason'] == 'stop'
    assert task['finished_at'] > 0


def test_long_task_gets_a_full_terminal_ttl_after_success():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('terminal-clock', ttl=600, push_channel='')
    task = runtime.create(user_id=1, task_id='terminal-clock-task')
    task['created_at'] = time.time() - 7_200

    assert stamp_chat_task_terminal(
        task, status='done', finish_reason='stop') is True
    assert runtime.cleanup_stale() == 0
    assert runtime.get(task['id']) is task

    task['finished_at'] = time.time() - 601
    assert runtime.cleanup_stale() == 1
    assert runtime.get(task['id']) is None


def test_interrupted_task_is_terminal_for_runtime_lifecycle_and_cleanup():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('interrupted-terminal', ttl=60, push_channel='')
    task = runtime.create(user_id=1, task_id='interrupted-terminal-task')
    task['status'] = 'interrupted'
    task['finished_at'] = time.time() - 61

    assert runtime.abort(task['id']) is False
    assert runtime.finish(task['id'], result='must-not-rewrite') is False
    assert runtime.cleanup_stale() == 1
    assert runtime.get(task['id']) is None


def test_interrupted_task_releases_heavy_terminal_state():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state

    task = {
        'id': 'interrupted-heavy-state',
        'status': 'interrupted',
        'messages': [{'role': 'user', 'content': 'large context'}],
        '_flow_turns': [{'large': 'snapshot'}],
        '_tool_result_cache': {'receipt': {'content': 'large result'}},
        '_unchanged_tool_result_receipts': {'digest': {'toolCallId': 'tc1'}},
        '_settled_tool_results': {'tc1': 'large settled result'},
        '_tool_call_id_receipts': {
            'tc1': {'signature': 'sig', 'name': 'read_files', 'status': 'done'}},
    }

    assert _release_heavy_task_state(task) == 6
    assert task['messages'] is None
    assert task['_flow_turns'] is None
    assert task['_tool_result_cache'] is None
    assert task['_unchanged_tool_result_receipts'] is None
    assert task['_settled_tool_results'] is None
    assert task['_tool_call_id_receipts'] is None


def test_error_finalizer_emits_only_once():
    events = []
    persisted = []
    task = {'id': 'terminal-task-0004', 'status': 'running'}
    kwargs = {
        'append_event_fn': lambda owner, item: events.append((owner, item)),
        'persist_task_result_fn': lambda owner: persisted.append(owner),
        'notify_terminal_fn': lambda _owner: None,
    }

    assert finalize_chat_task_error(
        task, {'kind': 'internal', 'message': 'first'}, **kwargs,
    ) is not None
    assert finalize_chat_task_error(
        task, {'kind': 'internal', 'message': 'duplicate'}, **kwargs,
    ) is None
    assert len(events) == 1
    assert persisted == [task]
    assert task['error']['message'] == 'first'


def test_pre_spawn_rejection_settles_persists_then_discards(monkeypatch):
    from lib.agent_core.execution_session import (
        ExecutionPhase,
        ExecutionSession,
        bind_model_route,
    )
    import lib.tasks_pkg.manager._events as event_module
    import lib.tasks_pkg.manager._persist as persist_module
    import lib.tasks_pkg.manager._registry as registry_module

    order = []
    task = {
        'id': 'pre-spawn-rejection-success',
        'convId': 'conv-rejected',
        'status': 'pending',
        '_executionSession': ExecutionSession(
            execution_id='pre-spawn-rejection-success',
            kind='chat',
            owner_user_id=1,
        ),
    }
    bind_model_route(
        task['_executionSession'], lambda: order.append('release'))
    monkeypatch.setattr(
        event_module, 'append_event',
        lambda _task, _event: order.append('event'),
    )
    monkeypatch.setattr(
        persist_module, 'persist_task_result',
        lambda _task: order.append('persist') or True,
    )
    monkeypatch.setattr(
        registry_module, 'notify_terminal_conversation_change',
        lambda _task: order.append('notify'),
    )
    monkeypatch.setattr(
        registry_module, 'discard_task',
        lambda task_id, conv_id=None: order.append(
            f'discard:{task_id}:{conv_id}'),
    )

    event = reject_unstarted_chat_task(
        task,
        RuntimeError('admission refused'),
        cause='task_admission_refused',
        conv_id='conv-rejected',
    )

    assert event['type'] == 'done'
    assert task['status'] == 'error'
    assert task['_executionSession'].phase is ExecutionPhase.FAILED
    assert order == [
        'release', 'event', 'persist', 'notify',
        'discard:pre-spawn-rejection-success:conv-rejected',
    ]
    assert '_terminalPersistencePending' not in task


def test_pre_spawn_rejection_retains_task_until_terminal_persist(monkeypatch):
    from lib.agent_core.execution_session import (
        ExecutionPhase,
        ExecutionSession,
        bind_model_route,
    )
    import lib.tasks_pkg.manager._events as event_module
    import lib.tasks_pkg.manager._persist as persist_module
    import lib.tasks_pkg.manager._registry as registry_module

    released = []
    discarded = []
    task = {
        'id': 'pre-spawn-rejection-pending',
        'convId': 'conv-rejected',
        'status': 'pending',
        '_executionSession': ExecutionSession(
            execution_id='pre-spawn-rejection-pending',
            kind='chat',
            owner_user_id=1,
        ),
    }
    bind_model_route(task['_executionSession'], lambda: released.append(True))
    monkeypatch.setattr(event_module, 'append_event', lambda *_args: None)
    monkeypatch.setattr(
        persist_module, 'persist_task_result', lambda _task: False)
    monkeypatch.setattr(
        registry_module, 'notify_terminal_conversation_change',
        lambda _task: None,
    )
    monkeypatch.setattr(
        registry_module, 'discard_task',
        lambda *_args, **_kwargs: discarded.append(True),
    )

    reject_unstarted_chat_task(
        task,
        RuntimeError('storage unavailable'),
        cause='task_spawn_failed',
        conv_id='conv-rejected',
    )

    assert released == [True]
    assert discarded == []
    assert task['status'] == 'error'
    assert task['_terminalPersistencePending'] is True
    assert task['_executionSession'].phase is ExecutionPhase.FAILED


def test_queued_abort_finalizer_settles_without_worker_entry():
    events = []
    persisted = []
    notified = []
    task = {'id': 'terminal-task-queued', 'status': 'pending'}

    event = finalize_chat_task_aborted(
        task,
        append_event_fn=lambda owner, item: events.append((owner, item)),
        persist_task_result_fn=lambda owner: persisted.append(owner),
        notify_terminal_fn=lambda owner: notified.append(owner),
    )

    assert task['status'] == 'done'
    assert task['aborted'] is True
    assert task['finishReason'] == 'aborted'
    assert event['type'] == 'done'
    assert event['finishReason'] == 'aborted'
    assert events == [(task, event)]
    assert persisted == [task]
    assert notified == [task]

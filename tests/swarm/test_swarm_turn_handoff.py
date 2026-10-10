"""A parent ending between inbox checks cannot lose terminal swarm evidence."""
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from lib import agent_inbox
from lib.swarm import integration
from lib.swarm.turn_handoff import prepare_swarm_turn_end, resume_swarm_after_turn

pytestmark = pytest.mark.unit


@pytest.fixture
def handoff(monkeypatch):
    key = 'handoff-' + uuid.uuid4().hex
    task = {'id': 'parent', 'convId': key, '_userId': 7, 'status': 'done'}
    session = SimpleNamespace(
        user_id=7, task_id='launch', is_terminated=True,
        abort=Mock(), on_settled=Mock(),
        get_status=Mock(return_value={'child': {'role': 'coder', 'status': 'running'}}))
    monkeypatch.setattr(integration, 'get_active_session', lambda k: session if k == key else None)
    remove = Mock(side_effect=AssertionError('handoff must not delete child evidence'))
    monkeypatch.setattr(integration, '_remove_session', remove)
    yield task, session
    agent_inbox.reset_for_test(key)


def enqueue(task, mode='swarm-update', status='failed'):
    agent_inbox.enqueue(task['convId'], 'retained evidence', priority='later',
                        mode=mode, agent_id='child', extra={'status': status})


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_natural_parent_end_preserves_unread_results_and_other_lanes(handoff, status):
    task, session = handoff
    enqueue(task, status=status)
    enqueue(task, mode='background-command')
    prepare_swarm_turn_end(task, user_id=7, finish_reason='stop')
    session.abort.assert_not_called()
    session.on_settled.assert_not_called()  # not before terminal persistence
    assert len(agent_inbox.pending_items(task['convId'])) == 2
    assert not agent_inbox.is_tombstoned(task['convId'])
    resume_swarm_after_turn(task, user_id=7)
    session.on_settled.assert_called_once_with()
    assert len(agent_inbox.pending_items(task['convId'])) == 2


def test_active_batch_is_detached_not_woken(handoff):
    task, session = handoff
    session.is_terminated = False
    enqueue(task)
    prepare_swarm_turn_end(task, user_id=7, finish_reason='incomplete')
    resume_swarm_after_turn(task, user_id=7)
    session.abort.assert_not_called()
    session.on_settled.assert_not_called()
    assert task['_waiting_on']['kind'] == 'swarm'
    assert task['_waiting_on']['agents'][0]['status'] == 'running'


def test_driver_settles_between_parent_prepare_and_commit(handoff):
    task, session = handoff
    session.is_terminated = False
    prepare_swarm_turn_end(task, user_id=7, finish_reason='incomplete')
    enqueue(task)
    session.is_terminated = True
    resume_swarm_after_turn(task, user_id=7)
    session.on_settled.assert_called_once_with()


def test_no_unread_swarm_result_never_wakes_for_another_lane(handoff):
    task, session = handoff
    enqueue(task, mode='background-command')
    resume_swarm_after_turn(task, user_id=7)
    session.on_settled.assert_not_called()
    assert agent_inbox.has_pending(task['convId'], modes=['background-command'])


def test_explicit_stop_signals_without_destroying_other_lane_or_session(handoff):
    task, session = handoff
    task['aborted'] = True
    enqueue(task, status='completed')
    enqueue(task, mode='background-command')
    enqueue(task, mode='peer-msg')
    prepare_swarm_turn_end(task, user_id=7, finish_reason='aborted')
    resume_swarm_after_turn(task, user_id=7)
    session.abort.assert_called_once_with()
    session.on_settled.assert_not_called()
    assert not agent_inbox.has_pending(task['convId'], modes=['swarm-update'])
    assert len(agent_inbox.pending_items(task['convId'])) == 2
    assert agent_inbox.is_tombstoned(task['convId'])


def test_foreign_owner_cannot_cancel_or_resume(handoff):
    task, session = handoff
    task['aborted'] = True
    enqueue(task)
    prepare_swarm_turn_end(task, user_id=8, finish_reason='aborted')
    task['aborted'] = False
    resume_swarm_after_turn(task, user_id=8)
    session.abort.assert_not_called()
    session.on_settled.assert_not_called()
    assert agent_inbox.has_pending(task['convId'], modes=['swarm-update'])


def test_read_terminal_results_are_not_relabelled_cancelled(handoff):
    task, session = handoff
    task['_waiting_on'] = {'kind': 'swarm'}
    prepare_swarm_turn_end(task, user_id=7, finish_reason='stop')
    resume_swarm_after_turn(task, user_id=7)
    assert '_waiting_on' not in task
    session.abort.assert_not_called()
    session.on_settled.assert_not_called()


@pytest.mark.parametrize('done_commits, diagnostics_commit', [
    (True, True), (True, False), (False, False)])
def test_real_finalizer_wakes_only_after_done_commit(
        handoff, monkeypatch, done_commits, diagnostics_commit):
    import time
    import lib
    from lib.tasks_pkg.orchestrator import _finalize

    task, session = handoff
    task.update(status='running', aborted=False, content='Waiting for the batch.',
                thinking='', error=None, toolRounds=[], config={},
                created_at=time.time() - 1)
    enqueue(task)
    observed = []
    session.on_settled.side_effect = lambda: observed.append('resume')
    monkeypatch.setattr(lib, 'ARTIFACTS_ENABLED', False)
    for name in ('_maybe_auto_retry_turn', '_finalize_dangling_tool_rounds',
                 '_maybe_append_sources_footer', '_salvage_undelivered_steer',
                 'cleanup_stale_cache_states',
                 '_maybe_preserve_accumulated_on_suspicion',
                 '_spawn_async_commit_round', '_spawn_async_profile_consolidation'):
        monkeypatch.setattr(_finalize, name, lambda *a, **kw: None)
    monkeypatch.setattr(_finalize, '_settle_post_loop_finish_reason',
                        lambda *a, **kw: 'stop')
    monkeypatch.setattr(_finalize, '_check_suspicious_completion', lambda *a, **kw: [])
    monkeypatch.setattr(_finalize, '_build_done_event_base',
                        lambda *a, **kw: {'type': 'done', 'finishReason': 'stop'})
    monkeypatch.setattr(_finalize, 'persist_task_result',
                        lambda *a, **kw: observed.append('persist') or diagnostics_commit)
    monkeypatch.setattr(_finalize, '_spawn_async_commit_round',
                        lambda *a, **kw: observed.append('commit-capture'))
    # The legacy autopilot turn-registration hook is retired; no surviving
    # owner remains to isolate (see test_legacy_autopilot_retirement).
    monkeypatch.setattr('lib.tasks_pkg.manager.notify_terminal_conversation_change', lambda *a: None)
    monkeypatch.setattr('lib.tasks_pkg.manager._persist._release_heavy_task_state',
                        lambda *a: observed.append('release'))
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime._live_successor_info',
                        lambda *a, **kw: ('', False))

    class CommitFailed(RuntimeError):
        pass

    def commit_done(current, event):
        assert current is task and event['type'] == 'done'
        assert observed == []
        assert agent_inbox.has_pending(task['convId'], modes=['swarm-update'])
        if not done_commits:
            raise CommitFailed('authority unavailable')
        observed.append('done')

    monkeypatch.setattr(_finalize, 'append_event', commit_done)

    def finalize():
        _finalize._finalize_and_emit_done(
            task, model='test', preset='medium', thinking_depth=None, cfg={},
            last_finish_reason='stop', last_usage={}, last_stream_result=None,
            accumulated_usage={}, api_rounds=[], tool_call_happened=False,
            messages=[], original_messages=[], all_search_results_text='',
            max_tokens=128, thinking_enabled=False, temperature=1.0,
            _loop_exit_reason='stop', _abort_detected_phase='', project_path='',
            project_enabled=False, round_num=0,
            assistant_msg={'role': 'assistant', 'content': task['content']})

    if done_commits:
        finalize()
        assert observed == (
            ['done', 'persist', 'resume', 'commit-capture', 'release']
            if diagnostics_commit else ['done', 'persist'])
    else:
        with pytest.raises(CommitFailed):
            finalize()
        assert observed == ['persist']
    session.abort.assert_not_called()
    assert agent_inbox.has_pending(task['convId'], modes=['swarm-update'])


@pytest.mark.parametrize('finish_reason', ['stop', 'end_turn', 'incomplete'])
def test_required_running_dependency_is_visible_after_clean_parent_end(handoff, finish_reason):
    task, session = handoff
    session.is_terminated = False
    prepare_swarm_turn_end(task, user_id=7, finish_reason=finish_reason)
    assert task['_waiting_on']['agents'][0]['state'] == 'running'
    session.on_settled.assert_not_called()


def test_settled_unread_dependency_is_awaiting_integration(handoff):
    task, session = handoff
    session.get_status.return_value = {'child': {'role': 'researcher', 'status': 'completed'}}
    enqueue(task, status='completed')
    prepare_swarm_turn_end(task, user_id=7, finish_reason='stop')
    assert task['_waiting_on']['agents'][0]['state'] == 'awaiting_integration'
    assert agent_inbox.has_pending(task['convId'], modes=['swarm-update'])


def test_optional_running_child_does_not_block_delivery(handoff):
    task, session = handoff
    session.is_terminated = False
    session.get_status.return_value = {'child': {
        'role': 'researcher', 'status': 'running', 'required_for_completion': False}}
    prepare_swarm_turn_end(task, user_id=7, finish_reason='stop')
    assert '_waiting_on' not in task
    session.abort.assert_not_called()


def test_required_settlement_can_resume_while_optional_child_runs(handoff):
    task, session = handoff
    session.is_terminated = False
    session.delivery_dependencies_settled = True
    enqueue(task, status='completed')
    resume_swarm_after_turn(task, user_id=7)
    session.on_settled.assert_called_once_with()


def test_required_flag_survives_checkpoint_and_rejects_coercion():
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(id='optional', required_for_completion=False)
    assert SubTaskSpec.from_dict(spec.to_dict()).required_for_completion is False
    assert SubTaskSpec.from_dict({'id': 'legacy'}).required_for_completion is True
    with pytest.raises(ValueError):
        SubTaskSpec.from_dict({'required_for_completion': 'false'})


def test_required_completion_publishes_inbox_before_wake_without_optional_wait():
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.types import SubTaskSpec, SubAgentResult
    key = 'required-join-' + uuid.uuid4().hex
    required = SubTaskSpec(id='needed', objective='work')
    optional = SubTaskSpec(id='extra', objective='explore', required_for_completion=False)
    seen = []
    master = MasterOrchestrator('join-task', key, [required, optional], user_id=7,
        model='test', on_settled=lambda: seen.append(agent_inbox.has_pending(key, modes=['swarm-update'])))
    try:
        master._on_agent_complete_callback(required, SubAgentResult(status='completed', final_answer='Evidence'))
        assert seen == [True]
        assert master.delivery_dependencies_settled and not master.is_terminated
        assert master.get_status()['extra']['required_for_completion'] is False
        master._on_agent_complete_callback(required, SubAgentResult(status='completed', final_answer='Duplicate'))
        assert seen == [True]
    finally:
        agent_inbox.reset_for_test(key)



@pytest.mark.parametrize('status,terminated,expected', [
    ('stalled', False, 'running'), ('unknown', True, 'unavailable')])
def test_non_success_dependency_states_do_not_disappear(handoff, status, terminated, expected):
    task, session = handoff
    session.is_terminated = terminated
    session.get_status.return_value = {'child': {'role': 'researcher', 'status': status}}
    prepare_swarm_turn_end(task, user_id=7, finish_reason='stop')
    assert task['_waiting_on']['agents'][0]['state'] == expected

"""Coordinated restart recovery is accepted Continue, never cancelled replay."""
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.swarm import restart_recovery as recovery
from lib.swarm.integration import _rehydrate
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec

pytestmark = pytest.mark.unit


@pytest.fixture
def authority(monkeypatch):
    from lib.identity import PrincipalContext
    task = {'id': 'resume-task', 'convId': 'conv', 'model': 'current-model',
        '_userId': 7, '_principal': PrincipalContext.user(subject_id='owner', owner_user_id=7),
        'config': {'_turnId': 'turn', '_attemptId': 'resume', '_turnOperation': 'continue'}}
    row = {'swarm_key': 'conv', 'conv_id': 'conv', 'task_id': 'original-task',
        'status': 'running', 'config': {'user_id': 7, 'model': 'old-model',
            'parent_cfg': {'_swarmParentTurnId': 'turn', '_swarmParentAttemptId': 'original',
                '_swarmParentTaskId': 'original-task', '_swarmToolNames': ['allowed']}},
        'specs': [{'id': 'child', 'role': 'coder', 'objective': 'work'}],
        'agents': [{'agent_id': 'child', 'status': 'running', 'messages': []}]}
    source = {'turnId': 'turn', 'taskId': 'original-task', 'status': 'interrupted',
              'settlement': {'cause': 'server_restart'}}
    latest = {'turnId': 'turn', 'currentAttemptId': 'resume', 'status': 'running',
              'laneId': 'main', 'ordinal': 2200}
    def attempt(attempt_id, **kwargs):
        if kwargs.get('include_execution_config'):
            return {'attemptId': task['config']['_attemptId'], 'conversationId': task['convId'],
                'turnId': task['config']['_turnId'], 'taskId': task['id'],
                'operation': task['config']['_turnOperation'], 'status': 'running',
                'config': {'_resumeSource': {
                    'attemptId': row['config']['parent_cfg']['_swarmParentAttemptId'],
                    'taskId': row['config']['parent_cfg']['_swarmParentTaskId'],
                    'status': source['status'], 'cause': (source.get('settlement') or {}).get('cause')}}}
        return source
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', attempt)
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda *a, **kw: latest)
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {'turns': []})
    return row, task, source, latest


def test_restart_continue_requires_exact_accepted_lineage(authority):
    row, task, source, latest = authority
    assert recovery.continue_recovery_allowed(row, task, user_id=7)


@pytest.mark.parametrize('mutation', [
    'user_stop', 'superseded', 'wrong_owner', 'wrong_turn', 'wrong_source',
    'wrong_source_task', 'new_turn', 'new_attempt', 'current_stop', 'terminated',
    'no_restart_provenance', 'already_aborted',
])
def test_recovery_fails_closed_for_stop_and_foreign_authority(authority, mutation):
    row, task, source, latest = authority
    if mutation == 'user_stop': source['settlement']['cause'] = 'user_abort'
    elif mutation == 'superseded': source['status'] = 'cancelled'
    elif mutation == 'wrong_owner': row['config']['user_id'] = 8
    elif mutation == 'wrong_turn': row['config']['parent_cfg']['_swarmParentTurnId'] = 'other'
    elif mutation == 'wrong_source': source['turnId'] = 'other'
    elif mutation == 'wrong_source_task': source['taskId'] = 'other'
    elif mutation == 'new_turn': latest['turnId'] = 'other'
    elif mutation == 'new_attempt': latest['currentAttemptId'] = 'other'
    elif mutation == 'current_stop': latest['status'] = 'cancelled'
    elif mutation == 'terminated': row['status'] = 'terminated'
    elif mutation == 'no_restart_provenance': source['settlement'] = {}
    elif mutation == 'already_aborted': task['aborted'] = True
    assert not recovery.continue_recovery_allowed(row, task, user_id=7)


def test_authority_failure_is_visible_not_fresh_execution(authority, monkeypatch):
    row, task, *_ = authority
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(RuntimeError, match='offline'):
        recovery.continue_recovery_allowed(row, task, user_id=7)


@pytest.mark.parametrize('messages,phase,expected', [
    ([], 'ready', ''),
    ([{'role': 'assistant', 'tool_calls': [{'id': 'write'}]}], 'ready', 'restart_tool_outcome_unknown'),
    ([{'role': 'assistant', 'tool_calls': [{'id': 'write'}]},
      {'role': 'tool', 'tool_call_id': 'write', 'content': 'receipt'}], 'ready', ''),
    ([{'role': 'tool', 'tool_call_id': 'write', 'content': 'receipt'}], 'tools_in_flight', 'restart_tool_outcome_unknown'),
    (['bad'], 'ready', 'restart_checkpoint_invalid'),
])
def test_unacknowledged_tools_are_never_blindly_replayed(messages, phase, expected):
    assert recovery.checkpoint_recovery_error({'messages': messages,
        'result': {'checkpoint_phase': phase}}) == expected


@pytest.mark.parametrize('messages', [
    [{'role': 'assistant', 'tool_calls': [None]}],
    [{'role': 'assistant', 'tool_calls': [{'function': {'name': 'write'}}]}],
    [{'role': 'assistant', 'tool_calls': 'malformed'}],
    [{'role': 'assistant', 'tool_calls': [{'id': 'same'}, {'id': 'same'}]},
     {'role': 'tool', 'tool_call_id': 'same', 'content': 'one receipt'}],
])
def test_malformed_tool_identity_cannot_prove_a_safe_checkpoint(messages):
    assert recovery.checkpoint_recovery_error({'messages': messages,
        'result': {'checkpoint_phase': 'ready'}}) == 'restart_checkpoint_invalid'


def test_bound_restart_session_never_launches_from_newer_successful_turn(authority, monkeypatch):
    row, *_ = authority
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'status': 'completed'}]})
    assert not _rehydrate._recovery_allowed(row, user_id=7)


def test_continue_rebuilds_current_authority_and_keeps_run_id(authority, monkeypatch):
    row, task, *_ = authority
    published = {}
    session = SimpleNamespace(rehydrate_in_background=Mock())
    factory = Mock(return_value=session)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: published.get(key))
    monkeypatch.setattr(_rehydrate, '_set_session', lambda key, value, **kw: published.setdefault(key, value))
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', Mock(side_effect=AssertionError('stale catalog')))
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    saved = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_session', saved)
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    tools = [{'type': 'function', 'function': {'name': 'allowed'}},
             {'type': 'function', 'function': {'name': 'new_privilege'}}]
    assert _rehydrate._rehydrate_one(row, resume_task=task, tool_list=tools)
    cfg = factory.call_args.kwargs
    assert cfg['task_id'] == 'original-task'
    assert cfg['parent_task'] is task
    assert cfg['model'] == 'current-model'
    assert cfg['all_tools'] == tools[:1]
    assert cfg['parent_config']['_swarmParentAttemptId'] == 'resume'
    assert not _rehydrate._rehydrate_one(row, resume_task=task, tool_list=tools)
    factory.assert_called_once()
    session.rehydrate_in_background.assert_called_once()
    assert '_principalContext' not in saved.call_args.kwargs['config']['parent_cfg']


def test_suspend_preserves_live_checkpoint_and_does_not_publish_false_result(monkeypatch):
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7, specs=[spec])
    child = {'aborted': False}
    master._agents['child'] = SimpleNamespace(_child=child, request_abort=Mock())
    master._scheduler = SimpleNamespace(cancel_pending=Mock())
    publish = Mock()
    master._publish_completion_inbox = publish
    master.suspend_for_restart()
    assert master.is_restart_suspended
    assert child['_abort_reason'] == 'server_shutdown'
    master._on_agent_complete_callback(spec, SubAgentResult(status='interrupted'))
    publish.assert_not_called()
    master.abort()
    assert not master.is_restart_suspended
    assert child['_abort_reason'] == 'user_abort'


def test_restored_prerequisite_satisfies_dag_without_rerun(monkeypatch):
    done = SubTaskSpec(id='done', objective='first')
    waiting = SubTaskSpec(id='waiting', objective='second', depends_on=['done'])
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7, specs=[done, waiting])
    scheduler = SimpleNamespace(_completed={}, add_specs=Mock())
    monkeypatch.setattr(master, '_build_scheduler', lambda: scheduler)
    monkeypatch.setattr(master, '_start_driver', Mock())
    rows = [{'agent_id': 'done', 'status': 'completed', 'delivered': True,
             'result': {'status': 'completed', 'final_answer': 'durable'}},
            {'agent_id': 'waiting', 'status': 'running',
             'messages': [{'role': 'user', 'content': 'Continue the dependent work'}], 'rounds_used': 3}]
    master.rehydrate_in_background(rows)
    assert scheduler._completed['done'][1].final_answer == 'durable'
    assert scheduler.add_specs.call_args.args[0] == [waiting]
    assert master._resume_rounds['waiting'] == 3


def test_unknown_external_outcome_becomes_visible_failure_not_worker_launch(monkeypatch):
    spec = SubTaskSpec(id='write', objective='external mutation')
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7, specs=[spec])
    publish = Mock()
    monkeypatch.setattr(master, '_reenqueue_swarm_update', publish)
    monkeypatch.setattr(master, '_build_scheduler', lambda: SimpleNamespace(_completed={}))
    monkeypatch.setattr(master, '_start_driver', Mock())
    master.rehydrate_in_background([{'agent_id': 'write', 'status': 'running',
        'result': {'checkpoint_phase': 'tools_in_flight'}, 'messages': []}])
    assert master._results_by_id['write'][1].status == 'failed'
    assert 'restart_tool_outcome_unknown' in master._results_by_id['write'][1].error_message
    publish.assert_called_once()
    assert not master._resume_messages


def test_server_shutdown_verdict_carries_restart_provenance():
    from lib.turn_verdict import derive_task_verdict, TurnTerminationEvidence
    verdict = derive_task_verdict({'status': 'aborted', 'aborted': True,
                                  '_abort_reason': 'server_shutdown'})
    assert verdict.status.value == 'interrupted'
    assert verdict.cause == 'server_restart'
    assert verdict.evidence is TurnTerminationEvidence.SYSTEM_RECOVERY


def test_failed_resume_binding_does_not_publish_or_delete_evidence(authority, monkeypatch):
    row, task, *_ = authority
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    publish = Mock()
    monkeypatch.setattr(_rehydrate, '_set_session', publish)
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', Mock())
    monkeypatch.setattr('lib.swarm.persistence.save_session', Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(RuntimeError, match='offline'):
        _rehydrate._rehydrate_one(row, resume_task=task,
            tool_list=[{'type': 'function', 'function': {'name': 'allowed'}}])
    publish.assert_not_called()


def test_repeat_restart_uses_previous_continue_not_original_attempt(authority):
    row, task, source, latest = authority
    row['config']['parent_cfg']['_swarmParentAttemptId'] = 'resume'
    row['config']['parent_cfg']['_swarmParentTaskId'] = 'resume-task'
    source['taskId'] = 'resume-task'
    task['id'] = 'next-task'
    task['config']['_attemptId'] = 'next'
    latest['currentAttemptId'] = 'next'
    assert recovery.continue_recovery_allowed(row, task, user_id=7)


def test_unstarted_roster_member_runs_without_invented_checkpoint(monkeypatch):
    spec = SubTaskSpec(id='unstarted', objective='work')
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7, specs=[spec])
    scheduler = SimpleNamespace(_completed={}, add_specs=Mock())
    monkeypatch.setattr(master, '_build_scheduler', lambda: scheduler)
    monkeypatch.setattr(master, '_start_driver', Mock())
    master.rehydrate_in_background([])
    scheduler.add_specs.assert_called_once_with([spec])


def test_required_tool_intent_write_fails_loud_and_snapshot_write_is_best_effort(monkeypatch):
    from lib.swarm import persistence
    monkeypatch.setattr(persistence, '_command', Mock(side_effect=RuntimeError('offline')))
    persistence.save_agent('key', agent_id='child', role='coder', objective='work', status='running', messages=[])
    with pytest.raises(RuntimeError, match='offline'):
        persistence.save_agent('key', agent_id='child', role='coder', objective='work', status='running', messages=[], required=True)
    with pytest.raises(RuntimeError, match='offline'):
        persistence.save_session('key', conv_id='conv', task_id='task', specs=[], config={}, required=True)


def test_quiesce_does_not_overwrite_an_earlier_user_stop(monkeypatch):
    from lib.tasks_pkg.manager import _registry
    live = {'status': 'running', 'aborted': False}
    stopped = {'status': 'running', 'aborted': True, '_abort_reason': 'user_abort'}
    class Runtime:
        def update_matching(self, *, predicate, updater):
            matched = [row for row in [live, stopped] if predicate(row)]
            for row in matched:
                updater(row)
            return matched
    monkeypatch.setattr(_registry, 'chat_task_runtime', Runtime())
    suspend = Mock()
    monkeypatch.setattr('lib.swarm.integration._state.suspend_swarms_for_restart', suspend)
    assert _registry.quiesce_running_tasks() == 1
    assert stopped['_abort_reason'] == 'user_abort'
    assert live['_abort_reason'] == 'server_shutdown'
    suspend.assert_called_once()


def test_transient_load_failure_is_not_the_same_as_absent_roster(monkeypatch):
    from lib.swarm import persistence
    monkeypatch.setattr(persistence, '_storage', Mock(side_effect=RuntimeError('offline')))
    assert persistence.load_session('key') is None
    with pytest.raises(RuntimeError, match='offline'):
        persistence.load_session('key', required=True)


def test_suspended_probe_is_known_paused_not_terminal(monkeypatch):
    from lib.swarm.integration import _state
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7, specs=[spec])
    master.suspend_for_restart()
    monkeypatch.setattr(_state, '_get_session', lambda key: master)
    status = _state.get_swarm_status('original', user_id=7)
    assert status['awaitingContinue'] is True
    assert status['known'] is True and status['active'] is False
    assert status['terminated'] is False
    assert status['agents'][0]['status'] == 'interrupted'
    assert master._build_agent_snapshot()['settled'] is False


def test_suspended_driver_emits_pause_not_complete_or_autocontinue(monkeypatch):
    spec = SubTaskSpec(id='child', objective='work')
    frames = []
    settled = Mock()
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7,
        specs=[spec], on_progress=frames.append, on_settled=settled)
    class Scheduler:
        running_count = 0
        pending_count = 0
        def started_at_map(self): return {}
        def cancel_pending(self): pass
        def iter_completions(self): return iter(())
        def shutdown(self): pass
    master._scheduler = Scheduler()
    master.suspend_for_restart()
    terminate = Mock()
    monkeypatch.setattr('lib.swarm.persistence.mark_session_terminated', terminate)
    monkeypatch.setattr(master, '_persist_agent_snapshot', Mock())
    master._start_driver()
    master._driver_thread.join(3)
    assert not master._driver_thread.is_alive()
    assert [frame['phase'] for frame in frames if frame['type'] == 'swarm_phase'] == ['interrupted']
    terminate.assert_not_called()
    settled.assert_not_called()


def test_continue_binds_existing_child_budget_rollup_and_stop_check(authority, monkeypatch):
    row, task, *_ = authority
    task['config']['executionLimits'] = {'max_tokens': 1000}
    factory = Mock(return_value=SimpleNamespace(rehydrate_in_background=Mock()))
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_set_session', Mock())
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr('lib.swarm.persistence.save_session', Mock())
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    assert _rehydrate._rehydrate_one(row, resume_task=task,
        tool_list=[{'type': 'function', 'function': {'name': 'allowed'}}])
    options = factory.call_args.kwargs
    options['on_agent_usage']({'prompt_tokens': 11, 'completion_tokens': 3}, 2)
    assert task['_swarmChildUsage']['prompt_tokens'] == 11
    assert task['_swarmChildToolCalls'] == 2
    assert options['abort_check']() is False
    task['aborted'] = True
    assert options['abort_check']() is True


def test_long_history_uses_indexed_successor_not_a_history_cap(authority, monkeypatch):
    row, task, _, latest = authority
    latest['ordinal'] = 50000
    reader = Mock(return_value={'turns': []})
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', reader)
    assert recovery.continue_recovery_allowed(row, task, user_id=7)
    reader.assert_called_once_with('conv', user_id=7, lane_id='main',
                                   after_ordinal=50000, limit=1)
    reader.return_value = {'turns': [{'turnId': 'successor'}]}
    assert not recovery.continue_recovery_allowed(row, task, user_id=7)


def test_cold_status_distinguishes_restart_pause_from_stop(authority, monkeypatch):
    from lib.swarm.integration import _state
    row, _, source, latest = authority
    latest.update(currentAttemptId='original', status='interrupted', settlement=source['settlement'])
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda key: row)
    monkeypatch.setattr(_state, '_resolve_key', lambda key: key)
    status = _state._status_from_persistence('conv', user_id=7)
    assert status['known'] and status['awaitingContinue']
    assert not status['active'] and not status['terminated']
    assert status['agents'][0]['status'] == 'interrupted'
    assert row['agents'][0]['status'] == 'running'
    assert _state._status_from_persistence('conv', user_id=8) is None
    source['settlement']['cause'] = 'user_abort'
    status = _state._status_from_persistence('conv', user_id=7)
    assert not status['known'] and not status.get('awaitingContinue')
    row['status'] = 'terminated'
    assert _state._status_from_persistence('conv', user_id=7)['terminated']


def test_stop_and_restart_race_always_keeps_stop_terminal():
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7,
                               specs=[SubTaskSpec(id='child', objective='work')])
    child = {'aborted': False}
    master._agents['child'] = SimpleNamespace(_child=child, request_abort=Mock())
    master._scheduler = SimpleNamespace(cancel_pending=Mock())
    barrier = threading.Barrier(3)
    errors = []
    def call(action):
        try:
            barrier.wait(timeout=3)
            action()
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=call, args=(action,))
               for action in (master.suspend_for_restart, master.abort)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors
    assert master._aborted and not master.is_restart_suspended
    assert child['_abort_reason'] == 'user_abort'
    assert master._parent_task_proxy['_abort_reason'] == 'user_abort'
    master.suspend_for_restart()
    assert not master.is_restart_suspended


def test_already_stopped_parent_is_not_reclassified_as_restart():
    master = MasterOrchestrator(task_id='original', conv_id='conv', user_id=7,
                               specs=[SubTaskSpec(id='child', objective='work')])
    master._parent_task_proxy.update(aborted=True, _abort_reason='user_abort')
    master.suspend_for_restart()
    assert not master.is_restart_suspended
    assert master._parent_task_proxy['_abort_reason'] == 'user_abort'


def test_repeated_restart_advances_source_attempt_but_keeps_launch_identity(authority):
    row, task, source, latest = authority
    row['config']['parent_cfg'].update(_swarmParentAttemptId='resume',
                                      _swarmParentTaskId='resume-task')
    source.update(taskId='resume-task')
    latest['currentAttemptId'] = 'resume-again'
    task['id'] = 'resume-again-task'
    task['config']['_attemptId'] = 'resume-again'
    assert recovery.continue_recovery_allowed(row, task, user_id=7)
    assert row['task_id'] == 'original-task'


@pytest.mark.parametrize('tools', [[], [{'type': 'function', 'function': {'name': 'allowed'}}]])
def test_continue_keeps_declared_selection_and_detached_execution_policy(authority, monkeypatch, tmp_path, tools):
    from lib.identity import PrincipalContext
    from lib.tasks_pkg import workspace_delivery
    row, task, *_ = authority
    canonical, selected = tmp_path / 'canonical', tmp_path / 'selected'
    reference = tmp_path / 'reference'
    for path in (canonical, selected, reference):
        path.mkdir()
    selection = {'kind': 'fixture', 'id': 'current-selection'}
    principal = PrincipalContext.user(subject_id='current-owner', owner_user_id=7).to_payload()
    task.update(_principalContext=principal, _requestedModelRef=selection,
                _pinned_provider_id='expired-current-slot', _executable_tool_catalog=tools)
    task['config'].update(projectPath=str(selected), projectPaths=[str(selected), str(reference)],
        readOnlyPaths=[str(reference)], routing={'mode': 'current-policy'},
        preferredProviderId='current-provider', _pinned_provider_id='expired-current-slot',
        _toolExecutionScope='conversation', _toolDiscoveryPolicyByName={'allowed': {'mode': 'visible'}})
    task['_workspaceDelivery'] = {'repositories': [{'canonicalRoot': str(canonical),
        'workspacePath': str(selected), 'workId': task['id']}]}
    task['_workspaceRootPolicy'] = {'primaryPath': str(selected), 'roots': {
        'work': {'path': str(selected), 'access': 'rw'},
        'acknowledged-reference': {'path': str(reference), 'access': 'ro'}}}
    row['config']['parent_cfg'].update(_pinned_provider_id='old-disposed-slot')
    session = MasterOrchestrator
    created = []
    def create(**kwargs):
        master = session(**kwargs)
        created.append(master)
        return master
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', create)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', Mock())
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(session, 'rehydrate_in_background', Mock())
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    saved = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_session', saved)
    assert _rehydrate._rehydrate_one(row, resume_task=task, tool_list=tools)
    proxy = created[0]._parent_task_proxy
    assert proxy['_principalContext'] == principal
    assert proxy['config']['modelRef'] == selection
    assert proxy['config']['routing'] == {'mode': 'current-policy'}
    assert proxy['config']['preferredProviderId'] == 'current-provider'
    assert '_pinned_provider_id' not in proxy['config']
    assert proxy['config']['_toolExecutionScope'] == 'conversation'
    assert proxy['config']['_executableToolCatalog'] == tools
    assert created[0].project_path == str(selected)
    assert proxy['_workspaceRootPolicy'] == task['_workspaceRootPolicy']
    assert workspace_delivery.execution_repositories(proxy) == [{
        'canonicalRoot': str(canonical), 'workspacePath': str(selected)}]
    assert '_workspaceDelivery' not in proxy and '_principalContext' not in proxy['config']
    durable = saved.call_args.kwargs['config']
    assert durable['workspace_execution_context']['_workspaceRootPolicy'] == task['_workspaceRootPolicy']
    assert durable['principal_context'] == principal and durable['tool_catalog'] == tools
    assert durable['parent_cfg']['_swarmParentAttemptId'] == 'resume'
    assert durable['parent_cfg']['_swarmParentTaskId'] == task['id']


@pytest.mark.parametrize('access', ['rw', 'ro', 'removed'])
def test_continue_restores_original_checkout_not_new_parent_checkout(authority, monkeypatch, tmp_path, access):
    from lib.identity import PrincipalContext
    from lib.tasks_pkg import workspace_delivery
    row, task, *_ = authority
    canonical = tmp_path / 'canonical'
    original = tmp_path / 'interrupted-child'
    current = tmp_path / 'new-parent'
    for path in (canonical, original, current):
        path.mkdir()
    (original / 'evidence.txt').write_text('unfinished child edits\n')
    (current / 'evidence.txt').write_text('new parent input\n')
    principal = PrincipalContext.user(subject_id='continue-owner', owner_user_id=7).to_payload()
    old_policy = {'primaryPath': str(original), 'roots': {
        'acknowledged-work': {'path': str(original), 'access': 'rw'}}}
    row['config'].update(project_path=str(original), workspace_isolated=True,
        principal_context=principal, tool_catalog=[], workspace_execution_context={
            'schema': 1, '_workspaceExecution': {'repositories': [{
                'canonicalRoot': str(canonical), 'workspacePath': str(original)}]},
            '_workspaceRootPolicy': old_policy})
    task.update(_principalContext=principal, _tenant_id='current-tenant')
    task['config'].update(projectPath=str(current), projectPaths=[str(current)],
                          executionLimits={'max_tokens': 500, 'max_tool_calls': None, 'deadline': None})
    task['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(canonical), 'workspacePath': str(current), 'workId': task['id']}]}
    task['_workspaceRootPolicy'] = {'primaryPath': str(current), 'roots': {
        'new-label': {'path': str(current), 'access': 'ro' if access == 'ro' else 'rw'}}}
    if access == 'removed':
        task['_workspaceRootPolicy']['roots'] = {}
    monkeypatch.setattr(workspace_delivery, '_checkout',
                        lambda path: (str(original), str(canonical)) if str(path) == str(original) else None)
    created = []
    def create(**kwargs):
        master = MasterOrchestrator(**kwargs)
        created.append(master)
        return master
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', create)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', Mock())
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(MasterOrchestrator, 'rehydrate_in_background', Mock())
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    saved = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_session', saved)
    if access == 'removed':
        with pytest.raises(ValueError, match='no longer declared'):
            _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
        assert not created
        saved.assert_not_called()
        return
    assert _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    proxy = created[0]._parent_task_proxy
    assert created[0].project_path == str(original)
    assert proxy['_workspaceRootPolicy']['roots']['acknowledged-work']['access'] == access
    assert proxy['_tenant_id'] == 'current-tenant'
    assert proxy['config']['projectPaths'] == [str(original)]
    assert (original / 'evidence.txt').read_text() == 'unfinished child edits\n'
    assert (current / 'evidence.txt').read_text() == 'new parent input\n'
    assert '_workspaceDelivery' not in proxy
    assert saved.call_args.kwargs['config']['workspace_execution_context']['_workspaceRootPolicy'] == proxy['_workspaceRootPolicy']
    assert proxy['_executionBudget'] is task['_executionBudget']


@pytest.mark.parametrize('fault', ['capacity', 'driver'])
def test_failed_activation_retains_exact_retry_receipt_and_drains_before_unpublication(authority, monkeypatch, fault):
    from lib.swarm.integration import _state
    row, task, source, latest = authority
    durable = deepcopy(row)
    published, events = {}, []
    created = []
    def factory(**kwargs):
        master = MasterOrchestrator(**kwargs)
        created.append(master)
        return master
    def save(key, **kwargs):
        durable.update(deepcopy(kwargs))
        events.append('save')
    def register(key, session, **kwargs):
        if fault == 'capacity':
            raise _state.SwarmSessionConflict('capacity')
        published[key] = session
        events.append('publish')
    def remove(key, **kwargs):
        assert not created[-1]._scheduler or events[-1] == 'drain'
        published.pop(key, None)
        events.append('remove')
    def start(master, agents):
        master._scheduler = SimpleNamespace(cancel_pending=lambda: events.append('cancel'),
            shutdown=lambda **kwargs: events.append('drain') if kwargs['require_quiescence'] else None)
        master._agents['child'] = SimpleNamespace(_child={'aborted': False}, request_abort=Mock())
        raise RuntimeError('driver start')
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: published.get(key))
    monkeypatch.setattr(_rehydrate, '_set_session', register)
    monkeypatch.setattr(_rehydrate, '_remove_session', remove)
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(MasterOrchestrator, 'rehydrate_in_background', start)
    monkeypatch.setattr('lib.swarm.persistence.save_session', save)
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    with pytest.raises(RuntimeError, match='evidence retained'):
        _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    assert not published
    assert durable['config']['parent_cfg']['_swarmRecoveryFailedAttempt'] == {
        'attemptId': 'resume', 'taskId': 'resume-task'}
    if fault == 'driver':
        assert events.index('save') < events.index('publish') < events.index('drain') < events.index('remove')
        assert created[0]._agents['child']._child['_abort_reason'] == 'server_shutdown'
    source.update(taskId='resume-task', status='failed', settlement={'cause': 'execution_failed'})
    latest['currentAttemptId'] = 'retry'
    task['id'] = 'retry-task'
    task['config']['_attemptId'] = 'retry'
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda attempt_id, **kwargs: (
        {'attemptId': 'retry', 'conversationId': 'conv', 'turnId': 'turn', 'taskId': 'retry-task',
         'operation': 'continue', 'status': 'running', 'config': {'_resumeSource': {
             'attemptId': 'resume', 'taskId': 'resume-task', 'status': source['status'],
             'cause': source['settlement']['cause']}}}
        if kwargs.get('include_execution_config') else source))
    assert recovery.continue_recovery_allowed(durable, task, user_id=7)
    source.update(status='interrupted', settlement={'cause': 'user_abort'})
    assert not recovery.continue_recovery_allowed(durable, task, user_id=7)


def test_stop_during_required_recovery_binding_never_publishes_dead_session(authority, monkeypatch):
    row, task, *_ = authority
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    publish = Mock()
    monkeypatch.setattr(_rehydrate, '_set_session', publish)
    start = Mock()
    monkeypatch.setattr(MasterOrchestrator, 'rehydrate_in_background', start)
    monkeypatch.setattr('lib.swarm.persistence.save_session', lambda *args, **kwargs: task.update(aborted=True))
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    assert not _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    publish.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize('mutation', ['wrong_task', 'regenerate', 'stopped_predecessor', 'missing_receipt'])
def test_local_continue_flags_cannot_override_durable_successor_receipt(authority, monkeypatch, mutation):
    row, task, source, _ = authority
    accepted = {'attemptId': 'resume', 'conversationId': 'conv', 'turnId': 'turn',
        'taskId': 'resume-task', 'operation': 'continue', 'status': 'running',
        'config': {'_resumeSource': {'attemptId': 'original', 'taskId': 'original-task',
                                  'status': 'interrupted', 'cause': 'server_restart'}}}
    if mutation == 'wrong_task': accepted['taskId'] = 'foreign'
    elif mutation == 'regenerate': accepted['operation'] = 'regenerate'
    elif mutation == 'stopped_predecessor':
        accepted['config']['_resumeSource'] = {'attemptId': 'stopped', 'taskId': 'stopped-task',
                                             'status': 'interrupted', 'cause': 'user_abort'}
    elif mutation == 'missing_receipt': accepted['config'] = {}
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda attempt_id, **kwargs:
                        accepted if kwargs.get('include_execution_config') else source)
    assert not recovery.continue_recovery_allowed(row, task, user_id=7)


def test_recovery_capacity_reservation_is_unpublished_exclusive_and_released(monkeypatch):
    from lib.swarm.integration import _state
    monkeypatch.setattr(_state, '_active_sessions', {})
    monkeypatch.setattr(_state, '_session_reservations', set())
    monkeypatch.setattr(_state, '_session_timestamps', {})
    monkeypatch.setattr(_state, '_key_aliases', {})
    monkeypatch.setattr(_state, 'MAX_SESSIONS', 1)
    monkeypatch.setattr(_state, '_cleanup_stale_sessions', Mock())
    monkeypatch.setattr(_state, '_reconcile_cleanup_timer_locked', Mock())
    with pytest.raises(RuntimeError, match='storage refused'):
        with _state.reserve_session('recovering'):
            assert not _state._active_sessions
            _state._set_session('other', object())
            with pytest.raises(_state.SwarmSessionConflict, match='reserved'):
                _state._set_session('recovering', object())
            raise RuntimeError('storage refused')
    assert not _state._session_reservations
    session = object()
    with _state.reserve_session('recovering'):
        _state._set_session('recovering', session, reserved=True)
    assert _state._active_sessions['recovering'] is session
    assert 'other' in _state._active_sessions
    assert not _state._session_reservations

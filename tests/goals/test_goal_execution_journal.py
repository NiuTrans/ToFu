"""Chat Goal launch/effect archives preserve authority across process loss."""
from copy import deepcopy
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from lib.goal_runs.contract import goal_run_policy
from lib.goal_runs.execution_journal import (
    create_goal_execution_journal, read_goal_execution,
)
from lib.goal_runs.repository import SidecarGoalRunRepository
from lib.goal_runs.service import GoalRunLifecycleError
from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import (
    ExecutionJournal, RecoveryRefused, encode_agent_result,
)
from lib.orchestration.run_recovery import load_recovery_records, RunRecoveryError
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.storage import StorageSupervisor

pytestmark = pytest.mark.unit


def test_partial_completed_receipt_never_authorizes_replay(archive):
    from lib.goal_runs.journal_codec import CHUNK_EVENT
    class FailingRuns:
        def get_execution_snapshot(self, *args):
            return archive.runs.get_execution_snapshot(*args)
        def replay(self, *args):
            return archive.runs.replay(*args)
        def project_event(self, run_id, cursor, event):
            if event.get('type') == CHUNK_EVENT and event.get('index') == 1:
                return False
            return archive.runs.project_event(run_id, cursor, event)
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=FailingRuns())
    runner = journal.runner(
        lambda *args: {'output': 'x' * (2 << 20)},
        context_snapshot=lambda: {'messages': [], 'task_state': {}},
        context_restore=lambda snapshot: None)
    with pytest.raises(RecoveryRefused, match='not durably accepted'):
        runner({'id': 'a'}, {}, 0)
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        _read(archive)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    client = lambda **kwargs: supervisor.client
    task = {
        'id': 'executor', '_goalRunId': 'goal_executor', 'convId': 'conversation',
        '_userId': 41, '_tenant_id': 'tenant-a',
        '_turnId': 'output-turn', '_attemptId': 'attempt',
        'config': {'_goalObjective': 'full original objective'},
        'messages': [
            {'role': 'system', 'content': 'Original project instructions'},
            {'role': 'user', 'content': 'full original objective'},
        ],
        '_todos': [{'id': 'verify', 'content': 'Verify the full scope', 'status': 'pending'}],
    }
    nodes = [
        {'id': 'start', 'type': 'control', 'kind': 'start'},
        {'id': 'a', 'type': 'role', 'role': 'general', 'params': {'objective': 'a'}},
        {'id': 'b', 'type': 'role', 'role': 'general', 'params': {'objective': 'b'}},
        {'id': 'stop', 'type': 'control', 'kind': 'stop'},
    ]
    definition = {'schema': 'tofu.orchestration/v1', 'name': 'Goal archive', 'nodes': nodes,
                  'edges': [{'from': a['id'], 'to': b['id']}
                            for a, b in zip(nodes, nodes[1:])]}
    goal = SidecarGoalRunRepository(41, tenant_id='tenant-a', client=client)
    goal.start('goal_executor', conversation_id='conversation',
               objective='full original objective', definition=definition,
               policy=goal_run_policy())
    runs = OrchestrationRunService(SidecarOrchestrationRunStore(
        41, tenant_id='tenant-a', client=client))
    launch = OrchestrationChatFlowLaunchSpec(
        projection='autopilot', initial_phase='worker',
        initial_context='Complete startup context, distinct from the objective',
        system_prompt_base='Original project instructions',
        tools=({'type': 'function', 'function': {'name': 'inspect'}},),
        model='selected-model', project_path='/project', thinking_enabled=True,
        max_iterations=40,
    )
    try:
        yield SimpleNamespace(task=task, definition=definition, launch=launch,
                              runs=runs, goal=goal, client=supervisor.client)
    finally:
        supervisor.stop()


def _events(archive):
    events, cursor = [], 0
    while True:
        page = archive.runs.replay('goal_executor', cursor)
        events.extend(page.events)
        if page.caught_up:
            return events
        assert page.next_cursor > cursor
        cursor = page.next_cursor


def _read(archive, events=None, **kwargs):
    return read_goal_execution(
        _events(archive) if events is None else events,
        run=archive.runs.get_execution_snapshot('goal_executor'),
        owner_user_id=kwargs.get('owner_user_id', 41),
        tenant_id=kwargs.get('tenant_id', 'tenant-a'))


@pytest.mark.parametrize('wallet,reserved', [(None, 0), ('w', 0), ('w', 9)])
def test_launch_preserves_personal_zero_estimate_and_paid_fee_identity(archive, wallet, reserved):
    archive.task.update(_billingUserId=wallet, _billing_reservation_micro=reserved)
    create_goal_execution_journal(archive.task, archive.definition, archive.launch, runs=archive.runs)
    archive.task['_billingUserId'] = 'changed-after-launch'
    frozen, _ = _read(archive)
    assert frozen['fee_account'] == ({'mode': 'cost'} if wallet is None else
        {'mode': 'wallet', 'wallet_user_id': wallet, 'reserved_micro': reserved})


def test_launch_is_frozen_before_effects_and_survives_context_mutation(archive):
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    archive.task['messages'][0]['content'] = 'changed after launch'
    journal.call('definition', 'reference', 0, 'reference', lambda: {'nodes': []})
    frozen, records = _read(archive)
    assert frozen['messages'][0]['content'] == 'Original project instructions'
    assert frozen['objective'] == 'full original objective'
    assert frozen['launch']['initial_context'] == archive.launch.initial_context
    assert frozen['launch']['model'] == 'selected-model'
    assert frozen['launch']['tools'] == list(archive.launch.tools)
    assert frozen['turn_id'] == 'output-turn' and frozen['attempt_id'] == 'attempt'
    assert frozen['worker_state']['_todos'] == archive.task['_todos']
    assert [event['type'] for event in _events(archive)] == [
        'goal_run_started', 'execution_journal_start', 'execution_journal_ready',
        'execution_account_frontier',
        'execution_effect_started', 'execution_effect_completed']
    assert len(records) == 1
    with pytest.raises(GoalRunLifecycleError, match='already has a journal'):
        create_goal_execution_journal(
            archive.task, archive.definition, archive.launch, runs=archive.runs)


def test_launch_preserves_routing_and_execution_authority_without_runtime_handles(archive):
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    ref = {'creator_id': 'creator', 'model_id': 'selected-model'}
    catalog = [*archive.launch.tools,
               {'type': 'function', 'function': {'name': 'hidden_tool'}},
               {'type': 'function', 'function': {'name': 'ask_human'}}]
    archive.task.update(model='selected-model', _requestedModelRef=ref,
                        _routedSlotGroup=object(), _tool_env={'API_KEY': 'runtime-secret'})
    archive.task['config'].update({
        'modelRef': ref, 'preferredProviderId': 'provider-a',
        'routing': {'strategy': 'priority'}, '_pinned_provider_id': 'expired-slot',
        'toolPermissions': {'shell': 'deny'}, 'temperature': 0.2,
        '_executableToolCatalog': catalog, '_toolExecutionScope': 'conversation',
        '_toolScriptSafeByName': {'hidden_tool': False},
        '_attemptId': 'old-attempt', 'apiKey': 'not-inherited-secret',
    })
    expected = snapshot_conversation_child_policy(
        archive.task, model=archive.launch.model, tools=list(archive.launch.tools),
        project_path=archive.launch.project_path, thinking_enabled=True)
    create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    archive.task['config']['toolPermissions']['shell'] = 'allow'
    ref['model_id'] = 'changed-model'
    policy = _read(archive)[0]['invocation_policy']
    assert policy == expected
    assert policy['config']['preferredProviderId'] == 'provider-a'
    assert policy['config']['modelRef']['model_id'] == 'selected-model'
    assert policy['config']['toolPermissions'] == {'shell': 'deny'}
    assert not {'_pinned_provider_id', '_attemptId', 'apiKey'} & policy['config'].keys()
    names = [tool['function']['name'] for tool in
             policy['conversation_tool_policy']['_executableToolCatalog']]
    assert names == ['inspect', 'hidden_tool']
    import json
    assert 'secret' not in json.dumps(policy)


def test_old_launch_archive_cannot_grant_generic_or_goal_recovery(archive):
    from lib.orchestration.execution_journal import fingerprint
    create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    events = _events(archive)
    header = next(event for event in events if event['type'] == 'execution_journal_start')
    header['launch_metadata']['format'] = 'tofu.goal-execution-launch/v1'
    header['launch_metadata'].pop('invocation_policy')
    header['launch_metadata_hash'] = fingerprint(header['launch_metadata'])
    with pytest.raises(RecoveryRefused, match='launch is invalid'):
        _read(archive, events)
    run = archive.runs.get_execution_snapshot('goal_executor')
    run.update(status='error', error={'kind': 'worker_lost',
                                    'source': 'orchestration.startup_recovery'})
    runs = SimpleNamespace(get_execution_snapshot=lambda run_id: run,
                           replay=lambda *args: SimpleNamespace(
                               events=events, caught_up=True, cursor_reset=False))
    with pytest.raises(RunRecoveryError, match='Chat Goal recovery requires'):
        load_recovery_records(runs, 'goal_executor')


def test_committed_goal_effect_is_reused_after_a_real_engine_crash(archive):
    class Crash(BaseException):
        pass
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    calls = []
    def runner(node, *_):
        calls.append(node['id'])
        return {'output': node['id']}
    def on_event(event):
        if event['type'] == 'step_complete' and event.get('node_id') == 'a':
            raise Crash()
    with pytest.raises(Crash):
        FlowExecutor(archive.definition, agent_runner=runner, on_event=on_event,
                     worker_context_policy='conversation', execution_journal=journal).run(
                         initial_context=archive.launch.initial_context)
    frozen, records = _read(archive)
    assert len(records) == 1
    replay = ExecutionJournal(lambda event: None, records=records,
                              worker_context_policy='conversation')
    replay.begin(archive.definition, frozen['launch']['initial_context'])
    result = FlowExecutor(archive.definition, agent_runner=runner,
                          worker_context_policy='conversation', execution_journal=replay).run(
                              initial_context=frozen['launch']['initial_context'])
    assert result['ok']
    replay.verify_consumed()
    assert calls == ['a', 'b']


def test_interrupted_model_effect_is_still_ambiguous(archive):
    class Crash(BaseException):
        pass
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    with pytest.raises(Crash):
        journal.call('agent', 'a', 0, [], lambda: (_ for _ in ()).throw(Crash()))
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        _read(archive)


def test_terminal_goal_revokes_new_effect_admission(archive):
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    archive.goal.transition('goal_executor', status='cancelled', reason='human_stop')
    calls = []
    with pytest.raises(RecoveryRefused, match='not durably accepted'):
        journal.call('definition', 'x', 0, 'x', lambda: calls.append('executed'))
    assert calls == []


def test_launch_rejection_and_owner_fences_admit_no_journal(archive):
    for replacement in (
            {'convId': 'other-conversation'},
            {'config': {'_goalObjective': 'smaller objective'}},
            {'_goalRunId': 'unknown-run'}):
        with pytest.raises(GoalRunLifecycleError, match='active owned run'):
            create_goal_execution_journal(
                {**archive.task, **replacement}, archive.definition,
                archive.launch, runs=archive.runs)
    with pytest.raises(GoalRunLifecycleError, match='launch is invalid'):
        create_goal_execution_journal(
            archive.task, archive.definition,
            replace(archive.launch, tools=({'callback': lambda: None},)), runs=archive.runs)
    assert len(_events(archive)) == 1
    create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    with pytest.raises(RecoveryRefused, match='binding changed'):
        _read(archive, owner_user_id=42)
    with pytest.raises(RecoveryRefused, match='binding changed'):
        _read(archive, tenant_id='other-tenant')


def test_corrupt_launch_and_generic_studio_recovery_are_refused(archive):
    create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    events = deepcopy(_events(archive))
    events[1]['launch_metadata']['launch']['model'] = 'foreign-model'
    with pytest.raises(RecoveryRefused, match='metadata does not match'):
        _read(archive, events)
    archive.runs.update_status('goal_executor', 'error', error={
        'kind': 'worker_lost', 'source': 'orchestration.startup_recovery',
        'message': 'Process lost'})
    with pytest.raises(RunRecoveryError, match='conversation, model context'):
        load_recovery_records(archive.runs, 'goal_executor')


def test_contextless_agent_receipt_cannot_authorize_chat_recovery(archive):
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    journal.call('agent', 'a', 0, [], lambda: encode_agent_result({'output': 'done'}))
    with pytest.raises(RecoveryRefused, match='receipt has no worker context'):
        _read(archive)


def test_large_launch_and_effects_cross_bounded_pages_without_clipping(archive):
    import json
    archive.task['messages'][0]['content'] = '完整证据' * 200000
    journal = create_goal_execution_journal(
        archive.task, archive.definition, archive.launch, runs=archive.runs)
    for index in range(12):
        journal.call('definition', f'reference-{index}', 0, str(index),
                     lambda: {'data': 'x' * (1 << 20)})
    events, cursor, page_count = [], 0, 0
    while True:
        page = archive.runs.replay('goal_executor', cursor)
        page_count += 1
        assert len(json.dumps(page.events, ensure_ascii=False).encode()) < 8 << 20
        assert all(len(json.dumps(event, ensure_ascii=False).encode()) < 1 << 20
                   for event in page.events)
        events.extend(page.events)
        if page.caught_up:
            break
        assert page.next_cursor > cursor
        cursor = page.next_cursor
    assert page_count > 1
    metadata, records = _read(archive, events)
    assert metadata['messages'] == archive.task['messages']
    assert len(records) == 12
    assert all(len(record['result']['data']) == 1 << 20 for record in records.values())


def test_competing_launches_elect_only_one_effect_producer(archive):
    barrier = threading.Barrier(2)
    class RacingRuns:
        def get_execution_snapshot(self, run_id):
            return archive.runs.get_execution_snapshot(run_id)
        def replay(self, run_id, cursor):
            page = archive.runs.replay(run_id, cursor)
            barrier.wait(timeout=10)
            return page
        def project_event(self, *args):
            return archive.runs.project_event(*args)
    journals, errors = [], []
    def start():
        try:
            journals.append(create_goal_execution_journal(
                archive.task, archive.definition, archive.launch, runs=RacingRuns()))
        except RecoveryRefused as exc:
            errors.append(exc)
    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert len(journals) == 1 and len(errors) == 1
    calls = []
    journals[0].call('definition', 'one', 0, 'one', lambda: calls.append('one') or {})
    assert calls == ['one']
    assert len(_read(archive)[1]) == 1


def test_chat_runtime_installs_the_journal_before_real_flow_dispatch(archive, monkeypatch):
    from lib.orchestration import chat_flow_runtime, chat_completion
    calls = []
    def runner(node, *_):
        assert _events(archive)[1]['type'] == 'execution_journal_start'
        assert _events(archive)[-1]['type'] == 'execution_effect_started'
        calls.append(node['id'])
        return {'output': node['id']}
    original_kwargs = OrchestrationChatFlowLaunchSpec.execution_kwargs
    def execution_kwargs(launch, task, **kwargs):
        value = original_kwargs(launch, task, **kwargs)
        value['executor_options']['agent_runner'] = runner
        return value
    monkeypatch.setattr(OrchestrationChatFlowLaunchSpec, 'execution_kwargs', execution_kwargs)
    monkeypatch.setattr(chat_flow_runtime, 'build_orchestration_chat_flow_launch',
                        lambda *args, **kwargs: archive.launch)
    class Completion:
        iterations = 0
        def __init__(self, task, **kwargs):
            assert kwargs['outcome'].result['ok']
        def prepare(self):
            return SimpleNamespace(chat_status='completed', stop_reason='verified_complete')
        def finish(self):
            calls.append('finished')
    monkeypatch.setattr(chat_completion, 'OrchestrationChatFlowCompletion', Completion)
    from lib.orchestration import chat_event_sink
    monkeypatch.setattr(chat_event_sink, 'OrchestrationChatTaskEventSink',
                        lambda *args: lambda event: None)
    from lib.orchestration import chat_turn_persistence
    class TurnPersistence:
        def __init__(self, *args, **kwargs):
            self._messages = []
        def bind(self, messages):
            self._messages = messages
        def __call__(self, message):
            return True
    monkeypatch.setattr(chat_turn_persistence, 'OrchestrationChatTurnPersistence', TurnPersistence)
    noop = lambda *args, **kwargs: None
    ports = chat_flow_runtime.OrchestrationChatFlowRuntimePorts(
        append_event=noop, persist_task_result=noop, notify_terminal=noop,
        stamp_terminal=noop, store_turns=noop, sync_turns=noop,
        complete_autopilot=noop,
        create_execution_journal=lambda *args: create_goal_execution_journal(
            *args, runs=archive.runs),
    )
    chat_flow_runtime.execute_orchestration_chat_flow_task(
        archive.task, archive.definition, label='autopilot', max_iterations=40,
        definition_service=SimpleNamespace(get_definition=lambda ref: None), ports=ports)
    assert calls == ['a', 'b', 'finished']
    assert len(_read(archive)[1]) == 2

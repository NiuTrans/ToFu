"""Restart recovery uses real Goal, conversation and effect authorities."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import threading
import time

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority
from lib.goal_runs.contract import goal_run_id_for_task, GOAL_RUN_CREATED_BY
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.goal_runs.recovery import (
    GoalRecoveryPorts, load_goal_recovery_seed, resume_chat_goal, bind_goal_recovery_task,
)
from lib.goal_runs.repository import SidecarGoalRunRepository
from lib.goal_runs.service import GoalRunService, GoalRunServicePorts
from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
from lib.turn_lifecycle import (
    create_turn_pair, bind_task, get_attempt, recover_running_attempts, LifecycleConflict,
)

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture
def recovery(semantic_authority, monkeypatch, request):
    client, owner, engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    config = {'autopilot': True, 'model': 'fixture-model', '_goalObjective': 'full objective'}
    options = getattr(request, 'param', None)
    options = options if isinstance(options, dict) else {'isolation': options}
    if 'executionLimits' in options:
        config['executionLimits'] = deepcopy(options['executionLimits'])
    pair = create_turn_pair(
        'recover-conversation', command_id='source',
        input_projection={'role': 'user', 'content': 'full objective'},
        config=config, user_id=owner, conversation_defaults={'allowCreate': True})
    bind_task(pair['attempt']['attemptId'], 'source-task', user_id=owner)
    task = {'id': 'source-task', 'convId': 'recover-conversation', '_userId': owner,
            '_turnId': pair['turn']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            'config': config, 'messages': [{'role': 'user', 'content': 'full objective'}],
            '_todos': [{'id': 'full-scope', 'status': 'pending'}]}
    nodes = [{'id': 'start', 'type': 'control', 'kind': 'start'},
             {'id': 'a', 'type': 'role', 'role': 'general', 'params': {}},
             {'id': 'b', 'type': 'role', 'role': 'virtual_user', 'params': {}},
             {'id': 'stop', 'type': 'control', 'kind': 'stop'}]
    if options.get('isolation'):
        nodes[1]['params']['isolation'] = options['isolation']
    definition = {'schema': 'tofu.orchestration/v1', 'name': 'Recovery proof', 'nodes': nodes,
                  'edges': [{'from': left['id'], 'to': right['id']}
                            for left, right in zip(nodes, nodes[1:])]}
    goal = SidecarGoalRunRepository(owner, client=client)
    service = GoalRunService(ports=GoalRunServicePorts(lambda *args: goal))
    service.start(task, definition)
    runs = OrchestrationRunService(SidecarOrchestrationRunStore(owner, client=client))
    launch = OrchestrationChatFlowLaunchSpec(
        projection='autopilot', initial_phase='worker', initial_context='original model context',
        system_prompt_base='fixed policy', tools=(), model='fixture-model', project_path='',
        thinking_enabled=None, max_iterations=40)
    commands_started = []
    def start(conv, cfg, data, abort_after, registered, **kwargs):
        new = {'id': 'successor-task' if not commands_started else f'successor-task-{len(commands_started)}', 'convId': conv, '_userId': owner,
               '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
               'config': cfg, 'messages': [{'role': 'user', 'content': 'rendered continuation'}],
               'abort_event': threading.Event()}
        registered(new['id'])
        commands_started.append(new)
        return new['id'], None
    commands = ConversationTurnCommandService(
        build_user_message=lambda *args: None, was_aborted_after=lambda *args: False,
        start_task=start)
    repo = SidecarConversationSyncRepository(client_factory=client)
    ports = GoalRecoveryPorts(repo, goal, get_attempt, commands)
    def retire():
        recover_running_attempts()
        result = retire_interrupted_orchestration_runs_detailed(
            error={'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'}, client=client)
        assert result.retired == 1
        return result
    return SimpleNamespace(task=task, launch=launch, definition=definition,
                           service=service, goal=goal, runs=runs, ports=ports,
                           retire=retire, started=commands_started, owner=owner, client=client)


def test_completed_effect_is_restored_into_a_fresh_bound_goal_without_repeating_work(recovery):
    case = recovery
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    history = [{'role': 'user', 'content': 'full objective'},
               {'role': 'assistant', 'content': 'committed tool result and evidence'}]
    class Crash(BaseException):
        pass
    source_executor = None
    def source_agent(*args):
        source_executor._default_runner_adapter.restore_worker_context({
            'messages': history, 'task_state': {'_todos': case.task['_todos']}})
        return {'output': 'first result'}
    def interrupt_after_commit(event):
        if event['type'] == 'step_complete' and event.get('node_id') == 'a':
            raise Crash()
    source_executor = FlowExecutor(case.definition, agent_runner=source_agent,
                                   on_event=interrupt_after_commit,
                                   worker_context_policy='conversation', execution_journal=journal)
    with pytest.raises(Crash):
        source_executor.run(initial_context=case.launch.initial_context)
    case.retire()
    source = case.task['_goalRunId']
    successor = resume_chat_goal(case.runs, source, owner_user_id=case.owner, ports=case.ports)
    assert successor == goal_run_id_for_task('successor-task')
    assert len(case.started) == 1
    task = case.started[0]
    seed = bind_goal_recovery_task(task, runs=case.runs,
                                  conversation_repository=case.ports.conversation_repository,
                                  get_attempt=get_attempt)
    assert task['messages'] == case.task['messages']
    assert task['_turnId'] != case.task['_turnId']
    assert task['_attemptId'] != case.task['_attemptId']
    case.service.start(task, seed.definition)
    new_journal = create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
    calls = []
    executor = None
    def agent(node, *args):
        calls.append(node['id'])
        assert executor._default_runner_adapter.snapshot_worker_context()['messages'] == history
        return {'output': 'new verification'}
    executor = FlowExecutor(seed.definition, agent_runner=agent, parent_task=task,
                            worker_context_policy='conversation', execution_journal=new_journal)
    assert executor.run(initial_context=seed.launch().initial_context)['ok']
    assert calls == ['b']
    assert case.runs.get_execution_snapshot(source)['status'] == 'error'
    assert case.goal.latest_for_conversation(task['convId'])['runId'] == successor
    assert resume_chat_goal(case.runs, source, owner_user_id=case.owner,
                            ports=case.ports) == successor
    assert len(case.started) == 1


@pytest.mark.parametrize('damage', ['pending_effect', 'new_human', 'stopped', 'wrong_owner'])
def test_ambiguous_or_superseded_goal_never_creates_a_successor(recovery, damage):
    case = recovery
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    if damage == 'pending_effect':
        class Crash(BaseException):
            pass
        with pytest.raises(Crash):
            journal.runner(lambda *args: (_ for _ in ()).throw(Crash()),
                           context_snapshot=lambda: {}, context_restore=lambda state: None)(
                case.definition['nodes'][1], case.launch.initial_context, 0)
    case.retire()
    if damage == 'new_human':
        create_turn_pair(case.task['convId'], command_id='new-human',
                         input_projection={'content': 'new objective'}, config={}, user_id=case.owner)
    elif damage == 'stopped':
        case.ports = replace(case.ports, commands=ConversationTurnCommandService(
            build_user_message=lambda *args: None, was_aborted_after=lambda *args: True,
            start_task=lambda *args: pytest.fail('Stop must prevent dispatch')))
    owner = case.owner + 1 if damage == 'wrong_owner' else case.owner
    with pytest.raises(RecoveryRefused):
        resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=owner, ports=case.ports)
    assert case.started == []


@pytest.mark.parametrize('cause,allowed', [('server_restart', True), ('human_stop', False), ('test', False)])
def test_parent_guard_admits_only_the_exact_restart_interruption(recovery, cause, allowed):
    case = recovery
    case.client(write=True).command('turn.event.record', {
        'attempt_id': case.task['_attemptId'], 'user_id': case.owner,
        'terminal': True, 'status': 'interrupted', 'projection': {},
        'settlement': {'outcome': 'interrupted', 'cause': cause}}, 'settle-parent')
    def create():
        return create_turn_pair(case.task['convId'], command_id='guarded',
                                input_projection={'content': 'continue'}, config={}, user_id=case.owner,
                                input_actor='virtual_user', parent_turn_id=case.task['_turnId'],
                                require_parent_not_stopped=True)
    if allowed:
        assert create()['submittedTurn']['parentTurnId'] == case.task['_turnId']
    else:
        with pytest.raises(LifecycleConflict) as rejected:
            create()
        assert rejected.value.code == 'lane_advanced'


def test_boot_default_factory_uses_the_goal_command_and_never_starts_a_studio_worker(recovery, monkeypatch):
    from lib.orchestration.continuity import run_boot_continuity_pass
    import lib.orchestration.sidecar_run_store as store_module
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    retirement = case.retire()
    original = store_module.SidecarOrchestrationRunStore
    monkeypatch.setattr(store_module, 'SidecarOrchestrationRunStore',
                        lambda owner, tenant_id: original(owner, tenant_id=tenant_id, client=case.client))
    kwargs = dict(candidates=retirement.candidates, auto_created_by={GOAL_RUN_CREATED_BY},
                  runtime=object(), resume_for_created_by={
                      GOAL_RUN_CREATED_BY: lambda *args, **options: resume_chat_goal(
                          *args, **options, ports=case.ports)})
    assert run_boot_continuity_pass(**kwargs)['resumed'] == 1
    assert run_boot_continuity_pass(**kwargs)['resumed'] == 1
    assert len(case.started) == 1


def test_stop_after_original_acceptance_but_before_recovery_preparation_is_honored(recovery):
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    accepted_at = get_attempt(case.task['_attemptId'], user_id=case.owner)['createdAt'] / 1000.0
    seen = []
    def aborted(conv, owner, watermark):
        seen.append(watermark)
        return watermark <= accepted_at + 0.0005
    case.ports = replace(case.ports, commands=ConversationTurnCommandService(
        build_user_message=lambda *args: None, was_aborted_after=aborted,
        start_task=lambda *args: pytest.fail('An older Stop must prevent automatic recovery')))
    with pytest.raises(RecoveryRefused, match='did not bind'):
        resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)
    assert seen == [accepted_at]


def test_public_command_cannot_forge_the_internal_recovery_pointer(recovery):
    case = recovery
    case.retire()
    case.ports.commands.create_turn(
        case.task['convId'], case.owner,
        {'commandId': 'public-command', 'config': {
            'autopilot': True, 'model': 'fixture-model',
            '_goalRecoverySourceRunId': case.task['_goalRunId'],
            '_goalRecoveryAbortAfterTs': 1.0},
         'inputTurn': {'role': 'user', 'content': 'new objective'}},
        request_started_at=time.time())
    assert '_goalRecoverySourceRunId' not in case.started[0]['config']
    assert '_goalRecoveryAbortAfterTs' not in case.started[0]['config']
    assert bind_goal_recovery_task(case.started[0], runs=case.runs) is None


def test_recovered_entry_uses_archived_graph_and_launch_on_the_normal_goal_lifecycle(recovery, monkeypatch):
    from lib.orchestration import chat_flow_runner, chat_flow_runtime, chat_completion
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    successor = resume_chat_goal(case.runs, case.task['_goalRunId'],
                                owner_user_id=case.owner, ports=case.ports)
    task = case.started[0]
    original_bind = bind_goal_recovery_task
    monkeypatch.setattr('lib.goal_runs.recovery.bind_goal_recovery_task',
                        lambda task: original_bind(
                            task, runs=case.runs,
                            conversation_repository=case.ports.conversation_repository,
                            get_attempt=get_attempt))
    monkeypatch.setattr(chat_flow_runner, '_build_builtin',
                        lambda *args, **kwargs: pytest.fail('Recovery must preserve the archived graph'))
    monkeypatch.setattr(chat_flow_runtime, 'build_orchestration_chat_flow_launch',
                        lambda *args, **kwargs: pytest.fail('Recovery must preserve the archived launch'))
    launches, calls = [], []
    original_kwargs = OrchestrationChatFlowLaunchSpec.execution_kwargs
    def kwargs(launch, task, **options):
        launches.append(launch)
        result = original_kwargs(launch, task, **options)
        result['executor_options']['agent_runner'] = lambda node, *args: (
            calls.append(node['id']) or {'output': 'verified'})
        return result
    monkeypatch.setattr(OrchestrationChatFlowLaunchSpec, 'execution_kwargs', kwargs)
    class Completion:
        iterations = 0
        def __init__(self, task, **options):
            assert options['outcome'].result['ok']
        def prepare(self):
            return SimpleNamespace(category='success', stop_reason='verified_complete',
                                   chat_status='completed', as_dict=lambda: {'category': 'success'})
        def finish(self):
            case.client(write=True).command('turn.event.record', {
                'attempt_id': task['_attemptId'], 'user_id': case.owner,
                'terminal': True, 'status': 'completed', 'projection': {'content': 'verified'},
                'settlement': {'outcome': 'completed', 'cause': 'generation_complete'}}, 'finish-recovered')
    monkeypatch.setattr(chat_completion, 'OrchestrationChatFlowCompletion', Completion)
    noop = lambda *args, **options: None
    from lib.orchestration.chat_turn_sync import store_flow_turns_on_task, sync_flow_turns_to_conversation
    ports = chat_flow_runtime.OrchestrationChatFlowRuntimePorts(
        append_event=noop, persist_task_result=noop, notify_terminal=noop, stamp_terminal=noop,
        store_turns=store_flow_turns_on_task, sync_turns=sync_flow_turns_to_conversation,
        complete_autopilot=case.service.complete,
        create_execution_journal=lambda *args: create_goal_execution_journal(*args, runs=case.runs))
    monkeypatch.setattr(chat_flow_runner, '_execute_flow_as_chat_task',
                        lambda task, definition, **options: chat_flow_runtime.execute_orchestration_chat_flow_task(
                            task, definition, label=options['label'], max_iterations=options['max_iter'],
                            definition_service=SimpleNamespace(get_definition=lambda ref: None), ports=ports))
    chat_flow_runner.run_autopilot_via_flow(task)
    assert calls == ['a', 'b']
    assert launches == [case.launch]
    assert case.goal.latest_for_conversation(task['convId'])['runId'] == successor
    assert case.goal.latest_for_conversation(task['convId'])['status'] == 'completed'
    assert get_attempt(task['_attemptId'], user_id=case.owner)['status'] == 'completed'


@pytest.mark.parametrize('crash_at', ['preparation', 'dispatch'])
def test_production_goal_distinguishes_preparation_from_external_admission(recovery, monkeypatch, crash_at):
    import lib.tasks_pkg.child_invocation as children
    class ProcessCrash(BaseException):
        pass
    journal = create_goal_execution_journal(
        recovery.task, recovery.definition, recovery.launch, runs=recovery.runs)
    calls = []
    def prepare(*args, **kwargs):
        raise ProcessCrash()
    def execute(child, *, policy):
        calls.append(child['id'])
        raise ProcessCrash()
    if crash_at == 'preparation':
        monkeypatch.setattr(children, 'create_child_invocation', prepare)
    monkeypatch.setattr(children, 'execute_invocation', execute)
    engine = FlowExecutor(
        recovery.definition, parent_task=recovery.task,
        model=recovery.launch.model, model_routing_policy='selected',
        worker_context_policy='conversation', execution_journal=journal)
    with pytest.raises(ProcessCrash):
        engine.run(initial_context=recovery.launch.initial_context)
    recovery.retire()
    if crash_at == 'dispatch':
        assert len(calls) == 1
        with pytest.raises(RecoveryRefused, match='external effects are unknown'):
            load_goal_recovery_seed(recovery.runs, 'goal_source-task',
                                    owner_user_id=recovery.owner)
    else:
        assert not calls
        seed = load_goal_recovery_seed(recovery.runs, 'goal_source-task',
                                       owner_user_id=recovery.owner)
        assert seed.records == {}
        successor = resume_chat_goal(
            recovery.runs, seed.source_run_id, owner_user_id=recovery.owner,
            ports=recovery.ports)
        assert successor == 'goal_successor-task'
        assert len(recovery.started) == 1
        assert recovery.started[0]['config']['_goalObjective'] == 'full objective'


@pytest.mark.parametrize('damage', ['', 'pending_effect', 'new_human', 'stopped'])
def test_public_resume_facade_uses_goal_admission_and_preserves_refusal(recovery, monkeypatch, damage):
    from lib.orchestration.runtime_start_service import OrchestrationRuntimeStartService
    from lib.orchestration.errors import RunRecoveryError, RunServiceError
    case = recovery
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    if damage == 'pending_effect':
        class Crash(BaseException):
            pass
        with pytest.raises(Crash):
            journal.runner(lambda *args: (_ for _ in ()).throw(Crash()),
                context_snapshot=lambda: {}, context_restore=lambda state: None)(
                    case.definition['nodes'][1], case.launch.initial_context, 0)
    case.retire()
    if damage == 'new_human':
        create_turn_pair(case.task['convId'], command_id='later-human',
            input_projection={'content': 'different requirement'}, config={}, user_id=case.owner)
    if damage == 'stopped':
        case.ports = replace(case.ports, commands=ConversationTurnCommandService(
            build_user_message=lambda *args: None, was_aborted_after=lambda *args: True,
            start_task=lambda *args: pytest.fail('Stop must prevent dispatch')))
    monkeypatch.setattr(GoalRecoveryPorts, 'defaults', classmethod(lambda cls, *args: case.ports))
    monkeypatch.setattr('lib.orchestration.runtime_start_service.spawn_runtime_flow',
                        lambda *args, **kwargs: pytest.fail('Goal cannot spawn a Studio Flow'))
    facade = OrchestrationRuntimeStartService(object(), definition_service=lambda: None,
                                              run_service=lambda: case.runs)
    source = case.task['_goalRunId']
    if damage:
        with pytest.raises(RunRecoveryError) as error:
            facade.resume(source, owner_user_id=case.owner)
        assert not error.value.not_found
        assert not case.started
    else:
        successor = facade.resume(source, owner_user_id=case.owner)
        assert successor == goal_run_id_for_task(case.started[0]['id'])
        seed = bind_goal_recovery_task(case.started[0], runs=case.runs,
            conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
        case.service.start(case.started[0], seed.definition)
        assert facade.resume(source, owner_user_id=case.owner) == successor
        assert len(case.started) == 1
    with pytest.raises(RunRecoveryError) as missing:
        facade.resume('missing-source', owner_user_id=case.owner)
    assert missing.value.not_found
    other_runs = OrchestrationRunService(SidecarOrchestrationRunStore(case.owner + 1, client=case.client))
    other_facade = OrchestrationRuntimeStartService(object(), definition_service=lambda: None,
                                                    run_service=lambda: other_runs)
    with pytest.raises((RunRecoveryError, RunServiceError)) as denied:
        other_facade.resume(source, owner_user_id=case.owner + 1)
    if isinstance(denied.value, RunRecoveryError):
        assert denied.value.not_found
    else:
        # Native clients bind a principal capability and reject a forged owner
        # before the query. SQL repositories filter it to the same absent view.
        assert denied.value.__cause__.__cause__.code == 'database_forbidden'


def test_goal_identity_survives_first_live_worker_and_settled_projection(recovery):
    from lib.orchestration.chat_live_turn import begin_live_flow_turn
    from lib.orchestration.chat_flow_adapter import FlowEventAdapter
    from lib.turn_lifecycle import get_turn, sync_visible_run_turns
    case = recovery
    begin_live_flow_turn(case.task, {'flowProjection': 'autopilot', 'turnRole': 'worker', 'iteration': 1})
    live = get_turn(case.task['convId'], case.task['_turnId'], user_id=case.owner)
    assert live['runId'] == case.task['_attemptId']
    assert live['projection']['orchestration']['goalRunId'] == case.task['_goalRunId']
    assert not live['projection'].get('_autopilotRunId')
    from lib.turn_lifecycle import record_task_event
    case.task.update(content='saved worker progress', status='running')
    assert record_task_event(case.task, {'type': 'delta', 'content': 'saved worker progress'})
    adapter = FlowEventAdapter(projection='autopilot', vu_run_id=case.task['_goalRunId'])
    adapter.on_event({'type': 'step_start', 'role': 'worker'})
    adapter.on_event({'type': 'step_complete', 'role': 'worker', 'status': 'completed',
                      'output': 'saved worker progress'})
    sync_visible_run_turns(case.task, adapter.messages)
    settled = get_turn(case.task['convId'], case.task['_turnId'], user_id=case.owner)
    assert settled['projection']['orchestration']['goalRunId'] == case.task['_goalRunId']
    assert settled['projection']['content'] == 'saved worker progress'


def test_first_live_goal_worker_can_be_retired_before_any_delta(recovery):
    from lib.orchestration.chat_live_turn import begin_live_flow_turn
    from lib.turn_lifecycle import get_turn
    begin_live_flow_turn(recovery.task, {
        'flowProjection': 'autopilot', 'turnRole': 'worker', 'iteration': 1})
    recovery.retire()
    turn = get_turn(recovery.task['convId'], recovery.task['_turnId'], user_id=recovery.owner)
    assert turn['status'] == 'interrupted'
    assert turn['settlement']['cause'] == 'server_restart'
    assert turn['projection']['orchestration']['goalRunId'] == recovery.task['_goalRunId']

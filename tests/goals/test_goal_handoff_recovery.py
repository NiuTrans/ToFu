"""Crash every pre-effect handoff boundary against real conversation authorities."""
import pytest
from tests.support.semantic_authorities import semantic_authority
from tests.goals.test_goal_conversation_recovery import recovery as recovery
from lib.goal_runs.journal_codec import CHUNK_EVENT
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.goal_runs.recovery import resume_chat_goal, bind_goal_recovery_task
from lib.orchestration.run_recovery import recovery_claim_successor
from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
from lib.turn_lifecycle import recover_running_attempts, create_turn_pair

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


class Crash(BaseException):
    pass


def retire(case):
    recover_running_attempts()
    return retire_interrupted_orchestration_runs_detailed(
        error={'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'}, client=case.client)


def resume(case):
    return resume_chat_goal(case.runs, case.task['_goalRunId'], owner_user_id=case.owner, ports=case.ports)


def bind(case, task):
    return bind_goal_recovery_task(task, runs=case.runs,
                                  conversation_repository=case.ports.conversation_repository,
                                  get_attempt=case.ports.get_attempt)


@pytest.mark.parametrize('phase', ['before_bind', 'before_goal', 'header_chunks', 'seed_copy', 'before_ready'])
def test_crashed_pre_effect_handoff_retries_through_a_new_normal_command(recovery, monkeypatch, phase):
    case = recovery
    if phase == 'header_chunks':
        case.task['messages'] = [{'role': 'user', 'content': 'original working context ' * 40000}]
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    if phase == 'seed_copy':
        from lib.orchestration.engine import FlowExecutor
        executor = None
        def agent(*args):
            executor._default_runner_adapter.restore_worker_context({
                'messages': case.task['messages'], 'task_state': {'_todos': case.task['_todos']}})
            return {'output': 'committed evidence'}
        def after_a(event):
            if event.get('type') == 'step_complete' and event.get('node_id') == 'a':
                raise Crash()
        executor = FlowExecutor(case.definition, agent_runner=agent, on_event=after_a,
                                worker_context_policy='conversation', execution_journal=journal)
        with pytest.raises(Crash):
            executor.run(initial_context=case.launch.initial_context)
    case.retire()
    source = case.task['_goalRunId']
    if phase == 'before_bind':
        with monkeypatch.context() as patch:
            patch.setattr(case.ports.commands, '_start_task', lambda *args, **kwargs: (_ for _ in ()).throw(Crash()))
            with pytest.raises(Crash):
                resume(case)
        # The abandoned command has no Task/Goal, but its accepted config is durable.
    else:
        resume(case)
        first = case.started[-1]
        assert recovery_claim_successor(case.runs, source) == ''
        if phase != 'before_goal':
            bind(case, first)
            case.service.start(first, case.definition)
            project = case.runs.project_event
            def interrupted(run, cursor, event):
                fail = (event.get('type') == 'execution_journal_ready' if phase == 'before_ready'
                        else event.get('type') == 'execution_effect_completed' if phase == 'seed_copy'
                        else event.get('type') == CHUNK_EVENT and event.get('index') == 1)
                if fail:
                    raise Crash()
                return project(run, cursor, event)
            with monkeypatch.context() as patch:
                patch.setattr(case.runs, 'project_event', interrupted)
                with pytest.raises(Crash):
                    create_goal_execution_journal(first, case.definition, case.launch, runs=case.runs)
    assert recovery_claim_successor(case.runs, source) == ''
    assert source in {row['run_id'] for row in retire(case).candidates}
    successor_id = resume(case)
    successor = case.started[-1]
    seed = bind(case, successor)
    assert seed.source_run_id == source
    assert successor['config']['_goalObjective'] == 'full objective'
    case.service.start(successor, case.definition)
    create_goal_execution_journal(successor, case.definition, case.launch, runs=case.runs)
    assert recovery_claim_successor(case.runs, source) == successor_id
    assert source not in {row['run_id'] for row in retire(case).candidates}


def test_ready_child_without_parent_ack_is_reconciled_without_a_second_executor(recovery, monkeypatch):
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    child_id = resume(case)
    child = case.started[-1]
    bind(case, child)
    case.service.start(child, case.definition)
    with monkeypatch.context() as patch:
        patch.setattr('lib.orchestration.run_recovery.record_recovery_claim',
                      lambda *args: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            create_goal_execution_journal(child, case.definition, case.launch, runs=case.runs)
    assert recovery_claim_successor(case.runs, case.task['_goalRunId']) == ''
    retire(case)
    assert resume(case) == child_id
    assert len(case.started) == 1
    assert recovery_claim_successor(case.runs, case.task['_goalRunId']) == child_id


@pytest.mark.parametrize('superseded', ['new_human', 'forged_command'])
def test_human_or_public_command_cannot_impersonate_an_unfinished_handoff(recovery, superseded):
    from lib.orchestration.execution_journal import RecoveryRefused
    from lib.goal_runs.recovery import _recovery_command_id
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    if superseded == 'new_human':
        resume(case)
        retire(case)
        create_turn_pair(case.task['convId'], command_id='new-human', config={},
                         input_projection={'content': 'new human objective'}, user_id=case.owner)
    else:
        # Exact command ID/virtual actor alone is not authority: the public config pointer is stripped.
        command = _recovery_command_id(case.task['_goalRunId'], case.task['_attemptId'], case.task['_attemptId'])
        case.ports.commands.create_turn(case.task['convId'], case.owner, {
            'commandId': command, 'parentTurnId': case.task['_turnId'],
            'inputActor': 'virtual_user', 'inputKind': 'continuation',
            'inputTurn': {'content': 'full objective', '_goalContinuation': True},
            'config': {'autopilot': True, '_goalRecoverySourceRunId': case.task['_goalRunId']}},
            request_started_at=0)
        retire(case)
    before = len(case.started)
    with pytest.raises(RecoveryRefused):
        resume(case)
    assert len(case.started) == before


def test_repeated_preparation_crashes_retain_the_original_seed_and_objective(recovery, monkeypatch):
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    # First handoff has a partial archive; second dies before creating its Goal.
    resume(case)
    first = case.started[-1]
    bind(case, first)
    case.service.start(first, case.definition)
    project = case.runs.project_event
    with monkeypatch.context() as patch:
        def before_ready(run, cursor, event):
            if event.get('type') == 'execution_journal_ready':
                raise Crash()
            return project(run, cursor, event)
        patch.setattr(case.runs, 'project_event', before_ready)
        with pytest.raises(Crash):
            create_goal_execution_journal(first, case.definition, case.launch, runs=case.runs)
    retire(case)
    resume(case)
    retire(case)
    last_id = resume(case)
    assert len(case.started) == 3
    last = case.started[-1]
    bind(case, last)
    case.service.start(last, case.definition)
    create_goal_execution_journal(last, case.definition, case.launch, runs=case.runs)
    assert recovery_claim_successor(case.runs, case.task['_goalRunId']) == last_id
    assert last['_goalRecoverySeed'].metadata['objective'] == 'full objective'


def test_private_execution_config_is_opt_in_owned_and_not_cached_into_public_reads(recovery):
    from lib.turn_lifecycle import get_attempt, LifecycleNotFound
    from lib.storage.errors import StorageError
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()
    resume(case)
    attempt_id = case.started[-1]['_attemptId']
    private = get_attempt(attempt_id, user_id=case.owner, include_execution_config=True)
    assert private['config']['_goalRecoverySourceRunId'] == case.task['_goalRunId']
    assert 'config' not in get_attempt(attempt_id, user_id=case.owner)
    with pytest.raises((LifecycleNotFound, StorageError)) as denied:
        get_attempt(attempt_id, user_id=case.owner + 1, include_execution_config=True)
    if isinstance(denied.value, StorageError):
        assert denied.value.code in {'database_not_found', 'database_forbidden'}
    with pytest.raises(TypeError):
        get_attempt(attempt_id, user_id=case.owner, include_execution_config=1)

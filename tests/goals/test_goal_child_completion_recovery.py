"""A completed registered worker crosses the real Goal recovery command."""
from copy import deepcopy

import pytest

from tests.goals.test_goal_conversation_recovery import (
    recovery as recovery, semantic_authority as semantic_authority,
)
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.goal_runs.recovery import resume_chat_goal, bind_goal_recovery_task
from lib.orchestration.engine import FlowExecutor
from lib.turn_lifecycle import get_attempt

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('recovery', ['shared-context'], indirect=True)
def test_completed_worker_missing_flow_ack_resumes_with_its_original_context(recovery, monkeypatch):
    import lib.tasks_pkg.child_invocation as children
    case, executed, histories = recovery, [], []
    class Crash(BaseException):
        pass
    def execute(child, *, policy):
        executed.append(child['id'])
        child['messages'].append({'role': 'assistant', 'content': 'verified original worker result'})
        histories.append(deepcopy(child['messages']))
        child.update(status='done', _messages_include_current_run=True, content='verified original worker result',
                     finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr(children, 'execute_invocation', execute)
    original = case.runs.project_event
    def publication(run_id, seq, event, **kwargs):
        if event.get('type') == 'execution_effect_completed':
            raise Crash()
        return original(run_id, seq, event, **kwargs)
    monkeypatch.setattr(case.runs, 'project_event', publication)
    journal = create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    with pytest.raises(Crash):
        FlowExecutor(case.definition, parent_task=case.task, model=case.launch.model,
            model_routing_policy='selected', worker_context_policy='conversation',
            execution_journal=journal).run(initial_context=case.launch.initial_context)
    monkeypatch.setattr(case.runs, 'project_event', original)
    assert len(executed) == 1
    case.retire()
    source = case.task['_goalRunId']
    successor = resume_chat_goal(case.runs, source, owner_user_id=case.owner, ports=case.ports)
    task = case.started[0]
    seed = bind_goal_recovery_task(task, runs=case.runs,
        conversation_repository=case.ports.conversation_repository, get_attempt=get_attempt)
    receipt = next(iter(seed.records.values()))
    assert receipt['invocation_id'] == executed[0]
    assert receipt['result']['worker_context']['messages'] == histories[0]
    case.service.start(task, seed.definition)
    resumed = create_goal_execution_journal(task, seed.definition, seed.launch(), runs=case.runs)
    calls, executor = [], None
    def agent(node, *args):
        calls.append(node['id'])
        assert executor._default_runner_adapter.snapshot_worker_context()['messages'] == histories[0]
        return {'output': 'new verification'}
    executor = FlowExecutor(seed.definition, agent_runner=agent, parent_task=task,
        worker_context_policy='conversation', execution_journal=resumed)
    assert executor.run(initial_context=seed.launch().initial_context)['ok']
    assert calls == ['b'] and len(executed) == 1
    assert task['_attemptId'] != case.task['_attemptId']
    assert resume_chat_goal(case.runs, source, owner_user_id=case.owner, ports=case.ports) == successor
    assert len(case.started) == 1

"""Graph scheduling policy delegates to the ordinary invocation owner."""
from __future__ import annotations

import copy
import pytest

from lib.orchestration.agent_runner import (
    OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner,
)

pytestmark = pytest.mark.unit


def _parent():
    return {'id': 'graph-parent', 'convId': 'graph-conversation', '_userId': 71,
            '_transientRuntime': True, '_turnId': 'parent-turn', '_attemptId': 'parent-attempt',
            'status': 'running', 'content': 'parent output', 'thinking': 'parent thought',
            'toolRounds': [{'toolName': 'parent'}],
            'config': {'model': 'chosen', 'preset': 'wrong-model',
                       'assistantMsgId': 'parent-message', 'checkpointContent': 'parent checkpoint',
                       '_turnId': 'parent-turn', '_attemptId': 'parent-attempt',
                       'modelRef': {'must': 'not rebind'}, 'projectPaths': ['/repo']}}


def _node():
    return {'id': 'worker', 'type': 'role', 'role': 'worker',
            'params': {'objective': 'Ship it', 'tier': 'heavy'}}


def test_config_requires_selected_model_and_valid_policy():
    with pytest.raises(ValueError, match='requires a model'):
        OrchestrationAgentRunnerConfig(model_routing_policy='selected')
    with pytest.raises(ValueError, match='unsupported'):
        OrchestrationAgentRunnerConfig(model='m', model_routing_policy='other')
    parent = _parent()
    config = OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen')
    assert config.executor_options()['parent_task'] is parent
    assert config.executor_options() is not config.executor_options()


def test_graph_leaf_uses_canonical_output_and_scoped_child(monkeypatch):
    import lib.tasks_pkg.child_invocation as child_module
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    parent = _parent()
    prior = copy.deepcopy(parent)
    captured = []
    events = []
    ref = {'taskId': 'graph-parent#agent:fixture', 'roundNum': 1, 'turn': 'request:fixture'}
    round_row = {'roundNum': 1, 'toolCallId': 'occurrence', 'toolName': 'read_files',
                 'toolContent': 'FULL result', 'status': 'done', 'inspectorRef': ref}

    def execute(task, *, policy):
        captured.append(task)
        assert policy.is_child and policy.parent_task_id == parent['id']
        assert task['id'] != parent['id']
        assert not task['_turnId'] and not task['_attemptId']
        assert task['config']['model'] == 'chosen'
        assert task['config']['preset'] == ''
        assert 'checkpointContent' not in task['config']
        assert 'modelRef' not in task['config']
        assert [tool['function']['name'] for tool in task['_invocationTools']] == ['read_files']
        task['config']['projectPaths'].append('/child-only')
        append_event(task, {'type': 'phase', 'phase': 'model', 'detail': 'waiting'})
        append_event(task, {'type': 'delta', 'thinking': 'reasoning', 'content': 'narration'})
        append_event(task, {'type': 'tool_start', 'query': 'Read fixture',
                            **{key: value for key, value in round_row.items()
                               if key != 'toolContent'}})
        append_event(task, {'type': 'tool_complete', **round_row})
        task.update(content='final answer', thinking='reasoning', status='done',
                    finishReason='stop', streamState='provider_finished',
                    toolRounds=[round_row], segments=[{'type': 'text', 'text': 'narration'},
                        {'type': 'tool', 'toolCallId': 'occurrence'},
                        {'type': 'text', 'text': 'final answer', 'terminal': True}])
    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    tools = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
             for name in ('read_files', 'ask_human', 'spawn_agents')]
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen', all_tools=tools,
                                       model_routing_policy='selected'),
        emit=events.append, abort_check=lambda: False,
        model_resolver=lambda *a, **kw: pytest.fail('Goal model must stay selected'),
    )(_node(), 'full latest handoff', 3)
    assert result.status == 'completed'
    assert result.output == 'final answer' and result.thinking == 'reasoning'
    assert result.output_projection['toolRounds'] == [round_row]
    assert 'usage' not in result.output_projection  # no response billing evidence
    assert result.tool_log == ()  # No lossy reconstruction carrier.
    assert result.tool_usage.exploratory_count == 1
    assert [e['type'] for e in events] == ['step_phase', 'step_delta', 'step_delta',
                                          'step_tool_event', 'step_tool_event']
    assert events[-1]['event']['inspectorRef'] == ref
    for key in prior:
        assert parent[key] == prior[key]
    assert chat_task_runtime.get(captured[0]['id']) is None


def test_conversation_worker_keeps_delegation_tools_without_granting_root_lifecycle(monkeypatch):
    import lib.tasks_pkg.child_invocation as child_module
    parent = _parent()
    parent['messages'] = [{'role': 'user', 'content': 'Investigate with independent reviewers'}]
    observed = []
    def execute(task, *, policy):
        observed.append(task)
        assert policy.is_child
        assert {t['function']['name'] for t in task['_invocationTools']} == {
            'read_files', 'spawn_agents', 'await_agents', 'get_agent_result'}
        assert 'delegated objective' not in str(task['messages'])
        task.update(content='done', status='done', finishReason='stop',
                    streamState='provider_finished', _messages_include_current_run=True)
    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    tools = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
             for name in ('read_files', 'ask_human', 'spawn_agents', 'await_agents', 'get_agent_result')]
    runner = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen', all_tools=tools,
                                       model_routing_policy='selected', worker_context_policy='conversation'),
        emit=lambda event: None, abort_check=lambda: False)
    try:
        node = _node()
        node['params']['isolation'] = 'shared-context'
        result = runner(node, '', 1)
        assert result.status == 'completed'
        assert len(observed) == 1
    finally:
        runner.clear_worker_context()


@pytest.mark.parametrize('finish,state,aborted,error,expected', [
    ('stop', 'provider_finished', False, None, 'completed'),
    ('aborted', 'provider_finished', True, None, 'cancelled'),
    ('stop', 'premature_close', False, None, 'failed'),
    ('error', 'unknown', False, 'provider unavailable', 'failed'),
    ('incomplete', 'provider_finished', False, None, 'stalled'),
])
def test_leaf_terminal_status_uses_shared_evidence_matrix(
        monkeypatch, finish, state, aborted, error, expected):
    import lib.tasks_pkg.child_invocation as child_module
    def execute(task, *, policy):
        task.update(content='partial or final text', finishReason=finish,
                    streamState=state, aborted=aborted, error=error, status='done')
    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=_parent(), model='chosen',
                                       model_routing_policy='selected'),
        emit=lambda event: None, abort_check=lambda: False,
    )(_node(), '', 0)
    assert result.status == expected


def test_role_route_resolves_once_and_discloses_before_dispatch(monkeypatch):
    import lib.tasks_pkg.child_invocation as child_module
    order = []
    def resolve(tier, model, **kwargs):
        order.append(('resolve', tier, model, kwargs['owner_user_id']))
        return 'routed-model'
    def execute(task, *, policy):
        order.append(('dispatch', task['config']['model']))
        task.update(content='done', status='done', finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=_parent(), model='chosen'),
        emit=lambda event: order.append(('event', event['modelRoute']['resolvedModel'])),
        abort_check=lambda: False, model_resolver=resolve,
    )(_node(), '', 0)
    assert order == [('resolve', 'heavy', 'chosen', 71), ('event', 'routed-model'),
                     ('dispatch', 'routed-model')]
    assert result.model_route.switched


def test_parentless_production_invocation_fails_before_dispatch(monkeypatch):
    import lib.tasks_pkg.child_invocation as child_module
    monkeypatch.setattr(child_module, 'execute_invocation', lambda *a, **kw: pytest.fail('unauthorized'))
    with pytest.raises(ValueError):
        OrchestrationInvocationRunner(
            OrchestrationAgentRunnerConfig(model='chosen', model_routing_policy='selected'),
            emit=lambda event: None, abort_check=lambda: False,
        )(_node(), '', 0)


def test_node_execution_overrides_narrow_tools_and_keep_model_provenance(monkeypatch):
    import lib.tasks_pkg.child_invocation as child_module
    observed = []
    def execute(task, *, policy):
        observed.append(task)
        task.update(content='done', status='done', finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    tools = [{'type': 'function', 'function': {'name': name}} for name in ('read_files', 'ask_human')]
    node = _node()
    node['params'].update(model_override='explicit-model', tools=['read_files', 'ask_human', 'not-granted'])
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=_parent(), model='chosen', all_tools=tools),
        emit=lambda event: None, abort_check=lambda: False,
        model_resolver=lambda *args, **kwargs: pytest.fail('explicit model must bypass tier selection'),
    )(node, '', 0)
    assert result.model_route.resolved_model == 'explicit-model'
    assert result.model_route.selected_model == 'chosen'
    assert [t['function']['name'] for t in observed[0]['_invocationTools']] == ['read_files']


def test_selected_goal_model_cannot_be_overridden_by_node():
    node = _node()
    node['params']['model_override'] = 'another-model'
    runner = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=_parent(), model='chosen', model_routing_policy='selected'),
        emit=lambda event: None, abort_check=lambda: False)
    with pytest.raises(ValueError, match='selected-model authority'):
        runner(node, '', 0)

"""Concurrent Goal leaves can delegate again through the same runtime and Stop."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
from tests.goals import test_goal_invocation_integration as fixture

runtime = fixture.runtime
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('cancel', [False, True])
def test_parallel_goal_children_and_nested_swarm_join_with_exact_effects(runtime, monkeypatch, cancel):
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.llm_errors import AbortedError
    from lib.llm.stream_result import ProviderStreamResult
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    branches = 8
    condition = threading.Condition()
    entered = {'outer': 0, 'inner': 0}
    outer_release, inner_release = threading.Event(), threading.Event()
    original_resolve = runtime.parent['_tool_env'].resolve
    nested_results, nested_scopes = [], []

    def tool(task, *args):
        if task['_parentTaskId'] == runtime.parent['id']:
            control = ChildCancellationScope(
                lambda: invocation_abort_requested(task), source_signal=task['abort_event'])
            nested_scopes.append(control)
            result = SubAgent(
                SubTaskSpec(role='general', objective='nested graph probe'),
                parent_task=task, all_tools=[fixture.TOOL], model='fixture-model',
                abort_check=control).run()
            nested_results.append(result)
            if result.status != 'completed':
                raise AbortedError('nested child stopped')
        return original_resolve(fixture.TOOL['function']['name'])(task, *args)

    runtime.parent['_tool_env'].resolve = lambda name: tool if name == fixture.TOOL['function']['name'] else None

    def provider(body, **kwargs):
        runtime.calls.append(copy.deepcopy(body))
        first = not any(message.get('role') == 'tool' for message in body['messages'])
        if first:
            nested = any('nested graph probe' in str(message.get('content', ''))
                         for message in body['messages'])
            lane = 'inner' if nested else 'outer'
            with condition:
                entered[lane] += 1
                condition.notify_all()
            assert (inner_release if nested else outer_release).wait(15)
        if kwargs['abort_check']():
            raise AbortedError('owned graph Stop')
        message = {'role': 'assistant', 'content': fixture.NARRATION if first else fixture.ANSWER}
        kwargs['on_content'](message['content'])
        if first:
            message['tool_calls'] = [{'id': 'task-local-call', 'type': 'function', 'function': {
                'name': fixture.TOOL['function']['name'], 'arguments': '{}'}}]
        return ProviderStreamResult.from_legacy(
            message, 'tool_calls' if first else 'stop',
            {'prompt_tokens': 40, 'completion_tokens': 30, 'total_tokens': 70})

    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    runner = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=runtime.parent, model='fixture-model',
                                      model_routing_policy='selected', all_tools=[fixture.TOOL]),
        emit=lambda event: None, abort_check=runtime.stop.is_set)
    with ThreadPoolExecutor(max_workers=branches) as executor:
        futures = [executor.submit(runner, {
            'id': f'branch-{i}', 'type': 'role', 'role': 'worker',
            'params': {'objective': f'outer graph probe {i}'}}, 'synthetic graph context', 1)
            for i in range(branches)]
        try:
            with condition:
                assert condition.wait_for(lambda: entered['outer'] == branches, timeout=15)
            assert runner._child_cancellation.child_count == branches
            assert runner._child_cancellation.observer_count == 1
            outer_release.set()
            with condition:
                assert condition.wait_for(lambda: entered['inner'] == branches, timeout=15)
            if cancel:
                runtime.stop.set()
            inner_release.set()
            results = [future.result(timeout=30) for future in futures]
        finally:
            outer_release.set()
            inner_release.set()
            runtime.stop.set()
    expected = 'cancelled' if cancel else 'completed'
    assert all(result.status == expected for result in results), results
    assert all(result.status == expected for result in nested_results), nested_results
    assert len(runtime.children) == branches * 2
    assert len(runtime.calls) == branches * (2 if cancel else 4)
    assert len(runtime.tool_calls) == (0 if cancel else branches * 2)
    assert all(chat_task_runtime.get(child['id']) is None for child in runtime.children)
    assert all(scope.child_count == scope.observer_count == 0
               for scope in [runner._child_cancellation, *nested_scopes])
    assert not runtime.parent['aborted']  # no upward cancellation right
    if not cancel:
        assert all(fixture.ANSWER in result.output for result in results)
        assert all(child['usage']['total_tokens'] == 140 for child in runtime.children)

"""Production SubAgent adapter contract for the graph interpreter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.orchestration_agent_runner import (
    OrchestrationAgentRunnerConfig,
    OrchestrationSubAgentRunner,
)
from lib.orchestration_runner_result import (
    OrchestrationAgentResult,
    OrchestrationModelRoute,
)
from lib.orchestration_tool_usage import OrchestrationToolUsage


pytestmark = pytest.mark.unit


def test_config_replays_public_executor_options_for_isolated_children():
    parent = {'id': 'parent'}
    tools = [{'function': {'name': 'write_file'}}]
    config = OrchestrationAgentRunnerConfig(
        parent_task=parent,
        all_tools=tools,
        model='model-x',
        project_path='/workspace',
        system_prompt_base='project policy',
        thinking_enabled=False,
    )

    first = config.executor_options()
    second = config.executor_options()
    assert first == second
    assert first is not second
    assert first['parent_task'] is parent
    assert first['all_tools'] is tools
    assert first['model'] == 'model-x'
    assert first['project_path'] == '/workspace'
    assert first['system_prompt_base'] == 'project policy'
    assert first['thinking_enabled'] is False
    assert first['model_routing_policy'] == 'role_tier'


def test_config_fails_closed_when_selected_model_is_missing():
    with pytest.raises(
            ValueError, match='selected-model orchestration requires a model'):
        OrchestrationAgentRunnerConfig(model_routing_policy='selected')

    with pytest.raises(ValueError, match='unsupported orchestration model'):
        OrchestrationAgentRunnerConfig(model_routing_policy='surprise')


def test_adapter_maps_node_config_streaming_and_result(monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    captured = {}

    class FakeSpec:
        def __init__(self, **kwargs):
            captured['spec'] = kwargs

    class FakeAgent:
        def __init__(self, spec, **kwargs):
            captured['agent'] = {'spec': spec, **kwargs}

        def run(self):
            sink = captured['agent']['stream_sink']
            sink('phase', 'waiting', phase='model', attempt=2)
            sink('thinking', 'reasoning ')
            sink('thinking', 'continues')
            sink('content', 'answer')
            return SimpleNamespace(
                final_answer='final answer',
                status='completed',
                error_message='must be suppressed',
                tool_log=[{'tool': 'write_file'}],
            )

    monkeypatch.setattr(protocol_module, 'SubTaskSpec', FakeSpec)
    monkeypatch.setattr(agent_module, 'SubAgent', FakeAgent)
    events = []
    abort = lambda: False
    parent = {'id': 'task'}
    tools = [{'function': {'name': 'write_file'}}]
    runner = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(
            parent_task=parent,
            all_tools=tools,
            model='m',
            project_path='/repo',
            system_prompt_base='system',
            thinking_enabled=True,
        ),
        emit=events.append,
        abort_check=abort,
        model_resolver=lambda _tier, parent, **_kwargs: parent,
    )

    result = runner({
        'id': 'worker-1',
        'type': 'role',
        'role': 'worker',
        'name': 'Worker',
        'params': {'objective': 'Ship it', 'tier': 'heavy'},
    }, 'upstream context', 3)

    assert captured['spec'] == {
        'role': 'worker',
        'objective': 'Ship it',
        'context': 'upstream context',
        'model_override': 'm',
        'model_tier': 'heavy',
    }
    agent = captured['agent']
    assert agent['parent_task'] is parent
    assert agent['all_tools'] is tools
    assert agent['system_prompt_base'] == 'system'
    assert agent['model'] == 'm'
    assert agent['thinking_enabled'] is True
    assert agent['abort_check'] is abort
    assert agent['project_path'] == '/repo'
    assert events == [
        {
            'type': 'step_phase', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'phase': 'model', 'detail': 'waiting',
            'modelRoute': {
                'selectedModel': 'm', 'resolvedModel': 'm',
                'role': 'worker', 'tier': 'heavy', 'kind': 'role_tier',
            },
            'attempt': 2,
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'thinking',
            'chunk': 'reasoning ',
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'thinking', 'chunk': 'continues',
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'content', 'chunk': 'answer',
        },
    ]
    assert result == OrchestrationAgentResult(
        output='final answer',
        status='completed',
        error='',
        thinking='reasoning continues',
        model_route=OrchestrationModelRoute(
            selected_model='m', resolved_model='m', role='worker',
            tier='heavy', kind='role_tier',
        ),
        tool_usage=OrchestrationToolUsage(
            state_changing_tools=('write_file',),
            reported=True,
        ),
        tool_log=({'tool': 'write_file'},),
    )


def test_adapter_streams_canonical_tool_lifecycle_and_reuses_identity(
        monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    captured = {}

    class FakeAgent:
        def __init__(self, _spec, **kwargs):
            captured.update(kwargs)

        def run(self):
            sink = captured['tool_event_sink']
            sink({'type': 'tool_start', 'roundNum': 2,
                  'toolCallId': 'flow-tool-occurrence',
                  'toolName': 'read_files', 'query': 'Read a.py'})
            sink({'type': 'tool_result', 'roundNum': 2,
                  'toolCallId': 'flow-tool-occurrence',
                  'toolName': 'read_files', 'results': [], 'status': 'done'})
            sink({'type': 'tool_complete', 'roundNum': 2,
                  'toolCallId': 'flow-tool-occurrence',
                  'toolName': 'read_files', 'toolContent': 'ok'})
            return SimpleNamespace(
                final_answer='done', status='completed', error_message='',
                tool_log=[{
                    'round': 2, 'tool': 'read_files',
                    'tool_call_id': 'flow-tool-occurrence',
                    'args_brief': 'Read a.py', 'preview': 'ok',
                    'preview_full_chars': 2, 'error': '',
                    'error_full_chars': 0, 'status': 'done',
                }],
            )

    monkeypatch.setattr(agent_module, 'SubAgent', FakeAgent)
    monkeypatch.setattr(protocol_module, 'SubAgentStatus', SimpleNamespace(
        COMPLETED=SimpleNamespace(value='completed')))
    events = []
    result = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(model='m'),
        emit=events.append,
        abort_check=lambda: False,
        model_resolver=lambda _tier, parent, **_kwargs: parent,
    )({'id': 'worker', 'type': 'role', 'role': 'worker', 'params': {}},
      '', 0)

    lifecycle = [event for event in events
                 if event.get('type') == 'step_tool_event']
    assert [event['event']['type'] for event in lifecycle] == [
        'tool_start', 'tool_result', 'tool_complete']
    assert {event['event']['toolCallId'] for event in lifecycle} == {
        'flow-tool-occurrence'}
    assert all(event['node_id'] == 'worker' for event in lifecycle)
    assert result.tool_log[0]['tool_call_id'] == 'flow-tool-occurrence'


def test_adapter_preserves_subagent_failure(monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    monkeypatch.setattr(
        protocol_module, 'SubTaskSpec', lambda **kwargs: kwargs)

    class FailedAgent:
        def __init__(self, _spec, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                final_answer='partial',
                status='failed',
                error_message='provider unavailable',
                tool_log=None,
            )

    monkeypatch.setattr(agent_module, 'SubAgent', FailedAgent)
    runner = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(),
        emit=lambda _event: None,
        abort_check=lambda: False,
        model_resolver=lambda _tier, parent, **_kwargs: parent,
    )
    result = runner({
        'id': 'worker', 'type': 'role', 'role': 'worker', 'params': {},
    }, '', 0)

    assert result == OrchestrationAgentResult(
        output='partial',
        status='failed',
        error='provider unavailable',
        thinking='',
        model_route=OrchestrationModelRoute(
            role='worker', tier='standard', kind='role_tier'),
        tool_usage=OrchestrationToolUsage(reported=True),
    )


def test_selected_policy_pins_goal_mode_and_role_tier_switch_is_observable(
        monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    captured = []

    class FakeAgent:
        def __init__(self, spec, **kwargs):
            self.model = spec.model_override
            captured.append((spec, kwargs))

        def run(self):
            return SimpleNamespace(
                final_answer='done', status='completed', error_message='',
                tool_log=[],
            )

    monkeypatch.setattr(agent_module, 'SubAgent', FakeAgent)
    monkeypatch.setattr(protocol_module, 'SubAgentStatus', SimpleNamespace(
        COMPLETED=SimpleNamespace(value='completed')))

    def cheapest_heavy(_tier, _parent, **_kwargs):
        return 'deepseek-v4-pro'

    node = {
        'id': 'worker', 'type': 'role', 'role': 'worker',
        'params': {'tier': 'heavy', 'objective': 'work'},
    }
    selected_events = []
    selected = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(
            model='kimi-k3', model_routing_policy='selected'),
        emit=selected_events.append,
        abort_check=lambda: False,
        model_resolver=lambda *_args, **_kwargs: pytest.fail(
            'selected policy must not invoke role-tier routing'),
    )(node, '', 0)
    assert captured[-1][0].model_override == 'kimi-k3'
    assert selected.model_route == OrchestrationModelRoute(
        selected_model='kimi-k3', resolved_model='kimi-k3', role='worker',
        tier='heavy', kind='selected',
    )
    assert not selected_events

    routed_events = []
    routed = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(
            model='kimi-k3', model_routing_policy='role_tier'),
        emit=routed_events.append,
        abort_check=lambda: False,
        model_resolver=cheapest_heavy,
    )(node, '', 0)
    assert captured[-1][0].model_override == 'deepseek-v4-pro'
    assert routed.model_route.switched is True
    assert routed_events == [{
        'type': 'step_phase',
        'node_id': 'worker',
        'role': 'worker',
        'emits': 'assistant',
        'phase': 'working',
        'detail': 'Model routing: kimi-k3 → deepseek-v4-pro (worker, heavy)',
        'detailKey': 'stream.phase.modelRouted',
        'detailArgs': {
            'from': 'kimi-k3', 'to': 'deepseek-v4-pro',
            'role': 'worker', 'tier': 'heavy',
        },
        'model': 'deepseek-v4-pro',
        'modelRoute': {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        },
    }]


def test_role_tier_resolver_receives_parent_owner_and_tenant(monkeypatch):
    import lib.swarm.agent as agent_module

    captured = {}

    class FakeAgent:
        def __init__(self, spec, **_kwargs):
            captured['model'] = spec.model_override

        def run(self):
            return SimpleNamespace(
                final_answer='done', status='completed', error_message='',
                tool_log=[],
            )

    def resolve(tier, parent, **kwargs):
        captured['resolver'] = (tier, parent, kwargs)
        return 'owner-routed-model'

    monkeypatch.setattr(agent_module, 'SubAgent', FakeAgent)
    parent = {
        'id': 'parent',
        '_userId': 73,
        '_tenant_id': 'tenant-routed',
        'config': {'_pinned_provider_id': 'provider-preference'},
    }
    result = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(
            parent_task=parent, model='selected-model'),
        emit=lambda _event: None,
        abort_check=lambda: False,
        model_resolver=resolve,
    )({
        'id': 'worker', 'type': 'role', 'role': 'worker',
        'params': {'tier': 'heavy'},
    }, '', 0)

    assert captured['resolver'] == (
        'heavy',
        'selected-model',
        {
            'role': 'worker',
            'provider_id': 'provider-preference',
            'owner_user_id': 73,
            'tenant_id': 'tenant-routed',
        },
    )
    assert captured['model'] == 'owner-routed-model'
    assert result.model_route.resolved_model == 'owner-routed-model'


def test_engine_keeps_runner_patch_point_but_no_swarm_implementation():
    engine = open('lib/orchestration_engine.py', encoding='utf-8').read()
    adapter = open(
        'lib/orchestration_agent_runner.py', encoding='utf-8').read()

    assert 'def _default_runner(' in engine
    assert 'return self._default_runner_adapter(' in engine
    assert 'from lib.swarm.agent import SubAgent' not in engine
    assert 'from lib.swarm.protocol import' not in engine
    assert 'class OrchestrationSubAgentRunner' in adapter
    assert '-> OrchestrationAgentResult' in adapter
    assert 'from lib.swarm.agent import SubAgent' in adapter
    assert adapter.index('def __call__(') < adapter.index(
        'from lib.swarm.agent import SubAgent')
    assert engine.count('\n') < 1820

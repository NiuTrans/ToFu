"""Identical reasoning intent reaches the wire through root, Goal and Swarm."""
from copy import deepcopy
import pytest
from lib.llm.reasoning import compile_reasoning
from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.goals.test_goal_invocation_integration import TOOL, _run_goal

runtime = goal_fixture.runtime
from lib.swarm.agent import SubAgent
from lib.swarm.protocol import SubTaskSpec

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('entry', ['root', 'goal', 'swarm'])
@pytest.mark.parametrize('settings,effort', [
    ({'thinkingDepth': 'max'}, 'max'),
    ({'thinkingDepth': 'lowest'}, 'low'),
    # 'default' now emits the destination's DECLARED default rung
    # explicitly on the effort-ladder wire (kimi-k3 anthropic-face repair).
    ({'thinkingDepth': 'default'}, 'medium'),
    ({'thinkingDepth': 'enabled'}, 'medium'),
    ({'thinkingDepth': 'off'}, 'none'),
    ({'thinkingDepth': 'max', 'thinkingEnabled': False}, 'none'),
])
def test_all_invocations_compile_the_same_saved_intent(runtime, monkeypatch, entry, settings, effort):
    from lib.llm.body import build_body
    import lib.tasks_pkg.orchestrator._ports as ports
    import lib.tasks_pkg.manager._stream as stream
    import lib.tasks_pkg.orchestrator._run as runner
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    runtime.parent['config'].pop('thinkingEnabled', None)
    runtime.parent['config'].update(settings)
    profile = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
        'supported_efforts': ['none', 'low', 'medium', 'high', 'max'],
        'default_effort': 'medium', 'can_disable': True}, 'sampling': {'omit': ['temperature']}}
    monkeypatch.setattr(ports, 'build_request_body', build_body)
    provider = stream.dispatch_stream
    wires = []
    def dispatch(body, **kwargs):
        compile_reasoning(body, 'selected-synthetic-model', inference_profile=profile)
        wires.append(deepcopy(body))
        return provider(body, **kwargs)
    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
    if entry == 'swarm':
        agent = SubAgent(SubTaskSpec(role='general', objective='Verify'),
                         parent_task=runtime.parent, all_tools=[TOOL], model='fixture-model')
        result = agent.run()
        assert result.status == 'completed', result.error_message
    elif entry == 'goal':
        result, _, _, _ = _run_goal(runtime)
        assert result.status == 'completed', result.error
    else:
        assembly = runner.assemble_round_tools
        def assemble(cfg, task, mcfg):
            if task is runtime.parent:
                task['_executable_tool_catalog'] = [TOOL]
                return [TOOL], True
            return assembly(cfg, task, mcfg)
        monkeypatch.setattr(runner, 'assemble_round_tools', assemble)
        execute_invocation(runtime.parent, policy=ROOT_INVOCATION_POLICY)
        assert runtime.parent['finishReason'] == 'stop', runtime.parent.get('error')
    assert len(wires) == 2
    assert all(body.get('reasoning_effort') == effort for body in wires)
    assert all('temperature' not in body for body in wires)


def test_child_config_copy_keeps_missing_false_and_executor_overrides_distinct():
    from lib.tasks_pkg.child_invocation import inherit_child_config
    source = {'thinkingEnabled': False, 'thinkingDepth': 'max', 'temperature': .3,
              'maxTokens': 8192, '_turnId': 'must-not-inherit'}
    original = deepcopy(source)
    assert inherit_child_config({}) == {}
    copied = inherit_child_config(source)
    assert copied['thinkingEnabled'] is False
    assert '_turnId' not in copied
    assert inherit_child_config(source, {'thinking_enabled': None,
        'thinking_depth': 'lowest'})['thinkingEnabled'] is None
    assert source == original


@pytest.mark.parametrize('enabled', [None, False, True])
def test_spawn_and_restart_preserve_parent_reasoning_policy(monkeypatch, tmp_path, enabled):
    import json
    from lib.swarm.integration import _tools, _rehydrate
    from lib.swarm.master import MasterOrchestrator
    saved, sessions = [], []
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    for module in (_tools, _rehydrate):
        monkeypatch.setattr(module, '_get_session', lambda key: None)
        monkeypatch.setattr(module, '_set_session', lambda key, session, **kw: sessions.append(session))
        monkeypatch.setattr(module, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr(MasterOrchestrator, 'run_in_background', lambda self: None)
    monkeypatch.setattr(MasterOrchestrator, 'rehydrate_in_background', lambda self, agents: None)
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda config: [])
    monkeypatch.setattr(_rehydrate, '_recovery_allowed', lambda *a, **kw: True)
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session',
        lambda key, **kw: saved.append(deepcopy(dict(swarm_key=key, **kw))))
    cfg = {'thinkingDepth': 'max', '_pinned_provider_id': 'scoped-provider'}
    if enabled is not None:
        cfg['thinkingEnabled'] = enabled
    task = {'id': 'reasoning-parent', 'convId': 'reasoning-conv', '_userId': 7, 'config': cfg}
    result = json.loads(_tools.execute_swarm_tool('spawn_agents',
        {'agents': [{'id': 'child', 'role': 'general', 'objective': 'Inspect'}]},
        task=task, model='fixture-model'))
    assert result['status'] == 'async_launched'
    assert saved[0]['config']['parent_cfg']['thinkingDepth'] == 'max'
    assert saved[0]['config']['thinking_enabled'] is enabled
    # Older snapshots could omit the switch entirely; omission remains default.
    if enabled is None:
        saved[0]['config'].pop('thinking_enabled')
    assert _rehydrate._rehydrate_one(saved[0])
    for session in sessions:
        assert session.thinking_enabled is enabled
        assert session._parent_task_proxy['config']['thinkingDepth'] == 'max'
        assert session._parent_task_proxy['config']['_pinned_provider_id'] == 'scoped-provider'

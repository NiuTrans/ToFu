"""New workers receive the caller's complete authorized tool scope."""
from copy import deepcopy

import pytest

from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.swarm import test_retained_assignment_authority as assignment_fixture

runtime = goal_fixture.runtime
authority = assignment_fixture.authority
tool = assignment_fixture.tool
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('catalog', [[], [tool('browser_list_tabs'), tool('mcp__demo__read')]])
def test_spawn_handler_uses_authority_not_visible_wire(runtime, monkeypatch, catalog):
    from lib.tasks_pkg.handlers.misc import _agents
    import lib.swarm.integration as integration
    seen = []
    monkeypatch.setattr(integration, 'execute_swarm_tool',
                        lambda *a, **kw: seen.append(kw['all_tools']) or '{}')
    runtime.parent['_executable_tool_catalog'] = deepcopy(catalog)
    _agents._handle_swarm_tool(
        runtime.parent, {}, 'spawn_agents', 'spawn', {'agents': []}, 1,
        {'query': 'Spawn agents', 'toolName': 'spawn_agents'},
        runtime.parent['config'], '', False,
        all_tools=[tool('spawn_agents'), tool('search_tools')])
    assert seen == [catalog]
    assert runtime.parent['_executable_tool_catalog'] == catalog


def test_later_ordinary_spawn_uses_fresh_assignment_not_old_session(authority):
    from lib.swarm.master import bind_assignment_context
    from lib.swarm.protocol import SubTaskSpec
    a = authority
    # The old session predates a browser reconnect/tool selection change.
    old_tools = deepcopy(a.session.all_tools)
    old_parent = deepcopy(a.session._parent_task_proxy)
    a.spec = SubTaskSpec(id='new-child', role='general', objective='Use current tools')
    granted = [tool('browser_list_tabs'), tool('mcp__demo__read')]
    bind_assignment_context(a.spec, parent_task=a.current,
        parent_config=a.current['config'], all_tools=granted,
        model='current-model', project_path='/current-project')
    agent = a.session._make_agent(a.spec)
    assert {t['function']['name'] for t in agent.tools} == {
        'browser_list_tabs', 'mcp__demo__read', 'store_artifact', 'read_artifact', 'list_artifacts'}
    assert agent.parent_task['id'] == a.current['id']
    assert agent.parent_task['config']['browserClientId'] == 'current-device'
    assert agent.model == 'current-model'
    assert not agent.abort_check(), 'a previous caller cancellation cannot cancel new work'
    assert a.session.all_tools == old_tools
    assert a.session._parent_task_proxy == old_parent
    a.current['aborted'] = True
    assert agent.abort_check()


def test_ordinary_assignment_roundtrip_preserves_narrow_scope(authority):
    from lib.swarm.master import bind_assignment_context
    from lib.swarm.protocol import SubTaskSpec
    a = authority
    spec = SubTaskSpec(id='new-child', role='general', objective='Use current tools',
                       tools=['browser_list_tabs'])
    bind_assignment_context(spec, parent_task=a.current,
        parent_config=a.current['config'], all_tools=[tool('browser_list_tabs'), tool('write_file')],
        model='current-model', project_path='/current-project')
    restored = SubTaskSpec.from_dict(spec.to_dict())
    assert restored.retained_assignment_parent is None
    agent = a.session._make_agent(restored)
    assert 'browser_list_tabs' in {t['function']['name'] for t in agent.tools}
    assert 'write_file' not in {t['function']['name'] for t in agent.tools}
    assert agent.parent_task['config']['browserClientId'] == 'current-device'
    forged = deepcopy(spec.to_dict())
    forged['id'] = 'different-child'
    with pytest.raises(ValueError, match='assignment'):
        a.session._make_agent(SubTaskSpec.from_dict(forged))


@pytest.mark.parametrize('runtime_kind', ['mappingproxy', 'lock'])
def test_followup_spawn_entrypoint_binds_new_catalog_and_device(monkeypatch, runtime_kind):
    from threading import Event, Lock
    from types import MappingProxyType
    import json
    from lib.swarm.integration import execute_swarm_tool, get_active_session
    from tests.swarm.test_swarm_async import _FakeAgent, _reset_global_state
    key = 'tool-parity-spawn'
    _reset_global_state(key)
    observed = {}
    ready = Event()
    first_running = Event()
    release_first = Event()
    class HeldAgent(_FakeAgent):
        def run(self):
            if self.spec.id == 'first':
                first_running.set()
                assert release_first.wait(10), 'first wave was not released'
            return self.result
    def factory(spec, **kwargs):
        observed[spec.id] = kwargs
        if spec.id == 'second':
            ready.set()
        return HeldAgent(spec)
    monkeypatch.setattr('lib.swarm.master._build_sub_agent', factory)
    try:
        parent = {'id': key, '_userId': 771, 'status': 'running',
                  'config': {'browserClientId': 'first-device'}}
        first = execute_swarm_tool('spawn_agents',
            {'agents': [{'id': 'first', 'role': 'general', 'objective': 'First wave'}]},
            task=parent, all_tools=[tool('write_file')])
        assert json.loads(first)['status'] == 'async_launched'
        assert first_running.wait(3)
        runtime_value = (MappingProxyType({'browser_list_tabs': 'private search text'})
                         if runtime_kind == 'mappingproxy' else Lock())
        runtime_key = '_mcpToolSearchTextByName' if runtime_kind == 'mappingproxy' else '_runtimeLock'
        parent['config'] = {'browserClientId': 'second-device',
                            runtime_key: runtime_value}
        second = execute_swarm_tool('spawn_agents',
            {'agents': [{'id': 'second', 'role': 'general', 'objective': 'Inspect browser'}]},
            task=parent, all_tools=[tool('browser_list_tabs')])
        assert json.loads(second)['status'] == 'async_launched'
        assert ready.wait(3)
        assert observed['second']['all_tools'] == [tool('browser_list_tabs')]
        assert observed['second']['parent_task']['config']['browserClientId'] == 'second-device'
        assert runtime_key not in observed['second']['parent_task']['config']
        assert parent['config'][runtime_key] is runtime_value
        session = get_active_session(key)
        assert session.all_tools == [tool('write_file')]
        assert session._parent_task_proxy['config']['browserClientId'] == 'first-device'
    finally:
        session = get_active_session(key)
        if session is not None:
            session.abort()
            release_first.set()
            if session._scheduler is not None:
                session._scheduler.shutdown(require_quiescence=True)
            if session._driver_thread is not None:
                session._driver_thread.join(timeout=3)
                assert not session._driver_thread.is_alive()
        _reset_global_state(key)


def test_followup_with_runtime_cache_completes_real_child_loop(runtime):
    from types import MappingProxyType
    from lib.swarm.master import MasterOrchestrator, bind_assignment_context
    from lib.swarm.protocol import SubTaskSpec
    from tests.goals.test_goal_invocation_integration import ANSWER, TOOL

    parent = runtime.parent
    session = MasterOrchestrator(parent['id'], parent['convId'], [], user_id=771,
        parent_task=parent, parent_config=parent['config'], model='fixture-model',
        all_tools=[TOOL])
    cache = MappingProxyType({TOOL['function']['name']: 'private search text'})
    parent['config']['_mcpToolSearchTextByName'] = cache
    spec = SubTaskSpec(id='followup', role='general', objective='Verify synthetic evidence')
    bind_assignment_context(spec, parent_task=parent, parent_config=parent['config'],
                            all_tools=[TOOL], model='fixture-model')
    agent = session._make_agent(spec)
    result = agent.run()

    assert result.status == 'completed', result.error_message
    assert ANSWER in result.final_answer
    assert len(runtime.calls) == 2
    assert len(runtime.tool_calls) == 1
    assert result.total_tokens == 140
    assert parent['config']['_mcpToolSearchTextByName'] is cache
    assert '_mcpToolSearchTextByName' not in agent.parent_task['config']

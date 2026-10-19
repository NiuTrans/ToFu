"""Complete assignment grants survive large catalogs, prompts and recovery."""
from copy import deepcopy
import json
from threading import Event

import pytest

from lib.swarm.master import bind_assignment_context
from lib.swarm.protocol import SubTaskSpec
from tests.swarm.test_retained_assignment_authority import authority as authority, tool
from tests.goals import test_goal_invocation_integration as goal_fixture

pytestmark = pytest.mark.unit
runtime = goal_fixture.runtime


@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('source', ['catalog', 'prompt'])
def test_large_assignment_roundtrip_preserves_complete_detached_authority(
        authority, retained, source):
    a = authority
    if not retained:
        a.spec = SubTaskSpec(id='ordinary', role='general', objective='Read evidence')
    a.spec.tools = ['read_files']
    catalog = [tool('read_files'), tool('write_file')]
    # UTF-8 itself exceeds the former ceiling, even without JSON escaping or
    # duplicate schema carriers. Tail witnesses catch silent trimming.
    large_text = '工具 evidence ' * 100_000 + 'COMPLETE-TAIL'
    if source == 'catalog':
        catalog[0]['function']['description'] = large_text
    else:
        a.current['config']['systemPromptBlocks'] = {'custom': large_text}
    expected_catalog = deepcopy(catalog)
    bind_assignment_context(a.spec, parent_task=a.current,
        parent_config=a.current['config'], all_tools=catalog,
        model='current-model', project_path='/current-project')
    payload = json.dumps(a.spec.to_dict(), ensure_ascii=False)
    assert len(payload.encode('utf-8')) > 1024 * 1024
    catalog[0]['function']['description'] = 'mutated after admission'
    a.current['config']['systemPromptBlocks'] = {'custom': 'changed'}
    restored = SubTaskSpec.from_dict(json.loads(payload))
    assert restored.retained_assignment_parent is None
    assert restored.retained_execution_context == a.spec.retained_execution_context
    assert restored.retained_execution_context['tools'] == expected_catalog
    agent = a.session._make_agent(restored)
    worker_names = {t['function']['name'] for t in agent.tools}
    assert 'read_files' in worker_names
    assert 'write_file' not in worker_names
    assert agent.parent_task['config']['_executableToolCatalog'] == expected_catalog
    assert agent.parent_task['_userId'] == a.current['_userId']
    assert agent.parent_task['config']['browserClientId'] == 'current-device'
    if source == 'prompt':
        assert agent.parent_task['config']['systemPromptBlocks'] == {'custom': large_text}
    else:
        assert agent.tools[0]['function']['description'] == large_text
    # Size must not weaken the independent selected-catalog authority fence.
    restored.retained_execution_context['config']['_executableToolCatalog'] = [tool('forged')]
    with pytest.raises(ValueError, match='selected tool authority'):
        a.session._make_agent(restored)


@pytest.mark.parametrize('invalid', [float('nan'), object()])
def test_assignment_still_rejects_non_json_policy(authority, invalid):
    a = authority
    a.current['config']['systemPromptBlocks'] = {'custom': invalid}
    with pytest.raises((TypeError, ValueError)):
        bind_assignment_context(a.spec, parent_task=a.current,
            parent_config=a.current['config'], all_tools=[tool('read_files')],
            model='current-model')
    assert a.spec.retained_execution_context is None
    assert a.spec.retained_assignment_parent is None


def test_large_assignment_completes_shared_model_tool_loop(runtime):
    from lib.swarm.master import MasterOrchestrator

    parent = runtime.parent
    catalog = [deepcopy(goal_fixture.TOOL), tool('unselected_large_tool')]
    catalog[1]['function']['description'] = 'complete contract ' * 100_000
    session = MasterOrchestrator(parent['id'], parent['convId'], [], user_id=771,
        parent_task=parent, parent_config=parent['config'], model='fixture-model',
        all_tools=catalog)
    spec = SubTaskSpec(id='large-grant-loop', role='general', objective='Verify evidence',
                       tools=[goal_fixture.TOOL['function']['name']])
    bind_assignment_context(spec, parent_task=parent, parent_config=parent['config'],
                            all_tools=catalog, model='fixture-model')
    result = session._make_agent(spec).run()
    assert result.status == 'completed', result.error_message
    assert goal_fixture.ANSWER in result.final_answer
    assert len(runtime.calls) == 2
    assert len(runtime.tool_calls) == 1
    assert result.total_tokens == 140
    for request in runtime.calls:
        assert 'unselected_large_tool' not in {t['function']['name'] for t in request['tools']}


def test_large_catalog_first_and_followup_spawn_return_launch_receipts(monkeypatch):
    from lib.swarm.integration import execute_swarm_tool, get_active_session
    from tests.swarm.test_swarm_async import _FakeAgent, _reset_global_state

    key = 'large-assignment-spawn'
    ready = {name: Event() for name in ('first', 'second')}
    release = Event()
    observed = {}

    class HeldAgent(_FakeAgent):
        def run(self):
            ready[self.spec.id].set()
            assert release.wait(10), 'spawn test did not release its workers'
            return self.result

    def factory(spec, **kwargs):
        observed[spec.id] = kwargs
        return HeldAgent(spec)

    monkeypatch.setattr('lib.swarm.master._build_sub_agent', factory)
    _reset_global_state(key)
    catalog = [tool('read_files')]
    catalog[0]['function']['description'] = 'tool contract ' * 100_000 + 'TAIL'
    parent = {'id': key, '_userId': 771, 'status': 'running', 'config': {}}
    try:
        for name in ready:
            parent['config']['browserClientId'] = name + '-device'
            receipt = json.loads(execute_swarm_tool('spawn_agents',
                {'agents': [{'id': name, 'role': 'general', 'objective': name}]},
                task=parent, all_tools=catalog))
            assert receipt['status'] == 'async_launched'
            assert ready[name].wait(5), 'acknowledged assignment did not reach its worker'
            assert observed[name]['all_tools'] == catalog
            assert observed[name]['parent_task']['config']['browserClientId'] == name + '-device'
    finally:
        session = get_active_session(key)
        release.set()
        if session is not None:
            session.abort()
            if session._scheduler is not None:
                session._scheduler.shutdown(require_quiescence=True)
            if session._driver_thread is not None:
                session._driver_thread.join(timeout=3)
                assert not session._driver_thread.is_alive()
        _reset_global_state(key)

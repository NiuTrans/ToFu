"""Wave preparation is detached, lossless and rejected before admission."""
from copy import deepcopy
import json
from threading import Event

import pytest

from lib.swarm.master import bind_assignment_context, bind_assignment_contexts
from lib.swarm.protocol import SubTaskSpec
from tests.swarm.test_retained_assignment_authority import authority as authority, tool

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('invalid_id', [object(), 7])
def test_assignment_identity_cannot_bypass_json_preparation(authority, invalid_id):
    a = authority
    spec = SubTaskSpec(id=invalid_id, role='general', objective='invalid identity')
    with pytest.raises(ValueError, match='assignment identity'):
        bind_assignment_context(spec, parent_task=a.current, parent_config=a.current['config'],
            all_tools=[tool('read_files')], model='current-model')
    assert spec.retained_execution_context is None


def test_wave_prepares_once_per_model_and_detaches_siblings(authority, monkeypatch):
    import lib.tasks_pkg.child_invocation as owner

    calls = []
    original = owner.snapshot_conversation_child_policy
    def snapshot(*args, **kwargs):
        calls.append(kwargs['model'])
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, 'snapshot_conversation_child_policy', snapshot)
    a = authority
    catalog = [tool('read_files')]
    specs = [SubTaskSpec(id=str(index), role='general', objective=str(index),
                        model_override='override' if index == 3 else '')
             for index in range(4)]
    bind_assignment_contexts(specs, parent_task=a.current,
        parent_config=a.current['config'], all_tools=catalog, model='current-model')
    assert calls == ['current-model', 'override']
    assert [spec.retained_execution_context['assignment_id'] for spec in specs] == ['0', '1', '2', '3']
    contexts = [spec.retained_execution_context for spec in specs]
    assert all('_executableToolCatalog' not in context['config'] for context in contexts)
    contexts[0]['tools'][0]['function']['description'] = 'changed'
    contexts[0]['config']['browserClientId'] = 'changed'
    assert contexts[1]['tools'] == catalog
    assert contexts[1]['config']['browserClientId'] == 'current-device'
    assert a.current['config']['browserClientId'] == 'current-device'
    assert a.session._make_agent(specs[3]).model == 'override'


def test_failed_wave_leaves_every_assignment_unbound(authority, monkeypatch):
    import lib.tasks_pkg.child_invocation as owner

    original = owner.snapshot_conversation_child_policy
    def snapshot(*args, **kwargs):
        if kwargs['model'] == 'unavailable':
            raise ValueError('model preparation failed')
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, 'snapshot_conversation_child_policy', snapshot)
    a = authority
    specs = [SubTaskSpec(id='first', role='general', objective='first'),
             SubTaskSpec(id='second', role='general', objective='second', model_override='unavailable')]
    with pytest.raises(ValueError, match='model preparation failed'):
        bind_assignment_contexts(specs, parent_task=a.current,
            parent_config=a.current['config'], all_tools=[tool('read_files')], model='current-model')
    assert all(spec.retained_execution_context is None for spec in specs)
    assert all(spec.retained_assignment_parent is None for spec in specs)


@pytest.mark.parametrize('legacy', [False, True])
def test_known_assignment_versions_restore_the_same_selected_authority(authority, legacy):
    a = authority
    bind_assignment_context(a.spec, parent_task=a.current, parent_config=a.current['config'],
        all_tools=[tool('read_files')], model='current-model')
    if legacy:
        context = a.spec.retained_execution_context
        context['schema'] = 'tofu.retained-assignment-execution/v1'
        context['config']['_executableToolCatalog'] = deepcopy(context['tools'])
    restored = SubTaskSpec.from_dict(json.loads(json.dumps(a.spec.to_dict())))
    agent = a.session._make_agent(restored)
    assert agent.parent_task['config']['_executableToolCatalog'] == [tool('read_files')]
    assert [t['function']['name'] for t in agent.tools] == ['read_files']
    restored.retained_execution_context['config']['_executableToolCatalog'] = [tool('write_file')]
    with pytest.raises(ValueError, match='selected tool authority'):
        a.session._make_agent(restored)


def test_unselected_catalog_metadata_is_not_copied_or_compiled(authority):
    from threading import Lock

    a = authority
    broader = [tool('read_files'), tool('unselected')]
    unselected = Lock()
    a.current['config']['_executableToolCatalog'] = broader
    a.current['config']['_toolContractDocumentsByName'] = {'unselected': unselected}
    a.current['_toolContractDocumentsByName'] = {'unselected': unselected}
    bind_assignment_context(a.spec, parent_task=a.current, parent_config=a.current['config'],
        all_tools=[tool('read_files')], model='current-model')
    context = a.spec.retained_execution_context
    assert context['tools'] == [tool('read_files')]
    assert set(context['config']['_toolContractDocumentsByName']) == {'read_files'}
    assert a.current['config']['_toolContractDocumentsByName']['unselected'] is unselected


@pytest.fixture
def launch(monkeypatch):
    from lib.swarm.integration import execute_swarm_tool, get_active_session
    from tests.swarm.test_swarm_async import _FakeAgent, _reset_global_state

    key = 'wave-preparation-errors'
    release, running = Event(), Event()
    class HeldAgent(_FakeAgent):
        def run(self):
            running.set()
            assert release.wait(15)
            return self.result
    monkeypatch.setattr('lib.swarm.master._build_sub_agent', lambda spec, **kwargs: HeldAgent(spec))
    _reset_global_state(key)
    parent = {'id': key, '_userId': 771, 'status': 'running', 'config': {}}
    def spawn(agents=None, catalog=None):
        return json.loads(execute_swarm_tool('spawn_agents',
            {'agents': agents or [{'id': 'first', 'objective': 'first'}]},
            task=parent, all_tools=catalog or [tool('read_files')]))
    try:
        yield parent, spawn, lambda: get_active_session(key), running
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


@pytest.mark.parametrize('followup', [False, True])
def test_invalid_wave_has_typed_rejection_without_scheduler_effects(launch, followup):
    from lib.tools.result_envelope import tool_result_error

    parent, spawn, current, running = launch
    if followup:
        assert spawn()['status'] == 'async_launched'
        assert running.wait(3)
    original = current()
    parent['config']['systemPromptBlocks'] = {'custom': float('nan')}
    result = spawn([{'id': 'bad-a', 'objective': 'a'}, {'id': 'bad-b', 'objective': 'b'}])
    assert result['code'] == 'swarm_context_preparation_failed'
    assert result['submission_state'] == 'rejected'
    assert result['phase'] == 'preparation'
    assert result['exception_type'] == 'ValueError'
    assert result['summary'] == result['message']
    assert tool_result_error(result).retryable is False
    assert current() is original
    if followup:
        assert [spec.id for spec in current().specs] == ['first']
        assert current()._scheduler.running_count == 1


@pytest.mark.parametrize('after_dispatch', [False, True])
def test_noncycle_dispatch_value_error_preserves_unknown_launch_state(launch, monkeypatch, after_dispatch):
    from lib.swarm.master import MasterOrchestrator

    _, spawn, current, running = launch
    original = MasterOrchestrator.run_in_background
    def failed_start(self):
        if after_dispatch:
            original(self)
        raise ValueError('driver setup failure')
    monkeypatch.setattr(MasterOrchestrator, 'run_in_background', failed_start)
    result = spawn()
    assert result['code'] == 'swarm_launch_unknown'
    assert result['submission_state'] == 'unknown'
    assert result['exception_type'] == 'ValueError'
    assert 'driver setup failure' in result['summary']
    assert result['retryable'] is False
    assert current() is not None
    assert result['swarm_run_id'] == current().task_id
    assert result['requested_agent_ids'] == ['first']
    assert 'agents' not in result, 'requested IDs must not masquerade as launch receipts'
    if after_dispatch:
        assert running.wait(3)
        assert current()._scheduler.running_count == 1


def test_cycle_rejected_before_session_publication(launch, monkeypatch):
    from unittest.mock import Mock
    save = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_session', save)
    _, spawn, current, _ = launch
    result = spawn([{'id': 'a', 'objective': 'a', 'depends_on': ['b']},
                    {'id': 'b', 'objective': 'b', 'depends_on': ['a']}])
    assert result['code'] == 'agent_dependency_cycle'
    assert result['submission_state'] == 'rejected'
    assert current() is None
    save.assert_not_called()


def test_initial_wave_compiles_selected_policy_once_without_per_child_grants(launch, monkeypatch):
    from threading import Lock
    from unittest.mock import Mock
    import lib.tasks_pkg.child_invocation as owner

    snapshot = Mock(wraps=owner.snapshot_conversation_child_policy)
    monkeypatch.setattr(owner, 'snapshot_conversation_child_policy', snapshot)
    parent, spawn, current, _ = launch
    parent['config']['_executableToolCatalog'] = [tool('read_files'), tool('unselected')]
    parent['config']['_toolContractDocumentsByName'] = {'unselected': Lock()}
    result = spawn([{'id': 'a', 'objective': 'a'}, {'id': 'b', 'objective': 'b'}])
    assert result['status'] == 'async_launched'
    assert snapshot.call_count == 1
    assert all(spec.retained_execution_context is None for spec in current().specs)
    assert current()._parent_task_proxy['config']['_executableToolCatalog'] == [tool('read_files')]


def test_config_copy_failure_is_typed_before_dispatch(launch):
    from threading import Lock

    parent, spawn, current, _ = launch
    parent['config']['systemPromptBlocks'] = {'invalid': Lock()}
    result = spawn()
    assert result['code'] == 'swarm_tool_failed'
    assert result['submission_state'] == 'rejected'
    assert result['phase'] == 'preparation'
    assert result['exception_type'] == 'TypeError'
    assert current() is None

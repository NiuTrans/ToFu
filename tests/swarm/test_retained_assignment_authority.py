"""Retained dialogue never supplies a later assignment's execution grant."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.identity import PrincipalContext
from lib.swarm.master import MasterOrchestrator, bind_retained_assignment_context
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.routing import tool_schema_name
from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.goals.test_goal_invocation_integration import TOOL

runtime = goal_fixture.runtime
pytestmark = pytest.mark.unit


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}}}}


@pytest.fixture
def authority(monkeypatch):
    import lib.swarm.master as master
    import lib.swarm.retained_repository as retained
    monkeypatch.setattr(master, '_copy_execution_context', lambda task: deepcopy(
        task.get('_authorityWorkspace', {'_workspaceRootPolicy': {'primaryPath': '', 'roots': {}}})))
    monkeypatch.setattr(retained.RetainedAgentRepository, 'get', lambda self, scope, aid:
        ({'state': 'busy', 'run_id': 'assignment', 'messages': [
            {'role': 'system', 'content': 'OLD WRITE AUTHORITY'},
            {'role': 'user', 'content': 'old assignment'},
            {'role': 'assistant', 'content': 'old completed observation'}]}, 1))

    monkeypatch.setattr(retained.RetainedAgentRepository, 'change',
        lambda self, scope, aid, transform: transform(deepcopy(self.get(scope, aid)[0])))
    principal = PrincipalContext.user(subject_id='retained-test', owner_user_id=771)
    old = {'id': 'original-swarm-run', 'convId': 'conversation', '_userId': 771,
           '_principalContext': principal.to_payload(), 'model': 'old-model',
           'config': {'model': 'old-model', 'projectPath': '/old-project',
                      'browserClientId': 'old-device', 'codeExecEnabled': True}}
    current = deepcopy(old)
    current.update(id='current-caller', model='current-model', _turnId='current-turn', _attemptId='current-attempt')
    current['config'].update(model='current-model', browserClientId='current-device',
                             codeExecEnabled=False, maxTokens=211, thinkingEnabled=False)
    spec = SubTaskSpec(id='assignment', role='general', objective='Fresh follow-up',
                       retained_agent_id='retained', retained_scope='conversation:conversation',
                       retained_run_id='assignment')
    session = MasterOrchestrator(old['id'], old['convId'], [], user_id=771,
        parent_task=old, parent_config=old['config'], model=old['model'],
        all_tools=[tool('read_files'), tool('write_file')], inbox_key='same-session',
        project_path='/old-project', abort_check=lambda: True)
    return SimpleNamespace(spec=spec, old=old, current=current, session=session)


def bind(a, **overrides):
    kwargs = dict(parent_task=a.current, parent_config=a.current['config'],
                  all_tools=[tool('read_files')], model='current-model',
                  project_path='/current-project', thinking_enabled=False)
    kwargs.update(overrides)
    bind_retained_assignment_context(a.spec, **kwargs)


@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('bound_workspace', [False, True])
def test_assignment_workspace_snapshot_excludes_runtime_config(
        tmp_path, monkeypatch, retained, bound_workspace):
    from collections import OrderedDict
    from threading import Lock
    from types import MappingProxyType
    from lib.project_mod.config import build_root_policy
    from lib.swarm.master import bind_assignment_context

    for field in ('_conv_roots', '_conv_primary', '_conv_cwd'):
        monkeypatch.setattr('lib.project_mod.config.' + field, OrderedDict())
    primary, extra = str(tmp_path / 'primary'), str(tmp_path / 'extra')
    cache = MappingProxyType({'read_files': 'private search text'})
    cfg = {'projectPath': primary, 'projectPaths': [extra], 'readOnlyPaths': [extra],
           '_mcpToolSearchTextByName': cache, '_runtimeLock': Lock()}
    parent = {'id': 'workspace-caller', 'convId': 'workspace-conversation', '_userId': 771,
              '_principalContext': PrincipalContext.user(
                  subject_id='workspace-test', owner_user_id=771).to_payload(), 'config': cfg}
    policy = build_root_policy(primary, [extra], [extra])
    if bound_workspace:
        parent['_workspaceRootPolicy'] = deepcopy(policy)
        parent['_workspaceExecution'] = {'repositories': [
            {'canonicalRoot': str(tmp_path / 'canonical'), 'workspacePath': primary}]}
    original = deepcopy({key: value for key, value in parent.items() if key != 'config'})
    spec = SubTaskSpec(id='workspace-assignment', role='general', objective='Inspect evidence')
    binder = bind_assignment_context
    if retained:
        spec.retained_agent_id = 'retained'
        spec.retained_run_id = spec.id
        binder = bind_retained_assignment_context
    binder(spec, parent_task=parent, parent_config=cfg, all_tools=[tool('read_files')],
           model='fixture-model', project_path=str(tmp_path / 'child-project'))

    context = json.loads(json.dumps(spec.retained_execution_context))
    assert context['execution_context']['_workspaceRootPolicy'] == policy
    assert {key: value for key, value in parent.items() if key != 'config'} == original
    if bound_workspace:
        assert context['execution_context']['_workspaceExecution'] == parent['_workspaceExecution']
        parent['_workspaceRootPolicy']['roots']['extra']['access'] = 'rw'
        assert spec.retained_execution_context['execution_context']['_workspaceRootPolicy'] == policy
    assert parent['config'] is cfg
    assert cfg['_mcpToolSearchTextByName'] is cache
    assert cfg['projectPaths'] == [extra]
    assert cfg['readOnlyPaths'] == [extra]
    assert '_mcpToolSearchTextByName' not in context['config']
    assert '_runtimeLock' not in context['config']


def test_fresh_narrowed_catalog_config_model_and_exact_run_mailbox(authority):
    a = authority
    bind(a)
    agent = a.session._make_agent(a.spec)
    assert {tool_schema_name(t) for t in agent.tools} == {'read_files'}
    assert agent.model == 'current-model'
    assert agent.project_path == '/current-project'
    assert agent.thinking_enabled is False
    assert agent.parent_task['id'] == 'current-caller'
    source = agent.parent_task['_turnRequirementSource']
    assert source.identity == ('conversation', 'current-turn', 'current-attempt', 'current-caller')
    assert agent.parent_task['config']['browserClientId'] == 'current-device'
    assert agent.parent_task['config']['codeExecEnabled'] is False
    assert agent.parent_task['config']['maxTokens'] == 211
    assert agent.parent_task is not a.current
    binding = agent._message_binding()
    assert binding['swarmRunId'] == 'original-swarm-run'
    assert binding['swarmKey'] == 'same-session'
    assert binding['conversationId'] == 'conversation'
    assert binding['projectPath'] == '/old-project'
    assert binding['retainedRunId'] == 'assignment'
    assert not agent.abort_check(), 'old caller cancellation is not new assignment authority'
    a.current['aborted'] = True
    assert agent.abort_check()
    a.current['aborted'] = False

    import threading
    a.current['abort_event'] = threading.Event()
    a.current['abort_event'].set()
    assert agent.abort_check(), 'Stop during reservation-to-entry is effective'
    a.current['status'] = 'done'
    assert not agent.abort_check(), 'normal finish is not assignment cancellation'
    a.session._aborted = True
    assert agent.abort_check()
    assert 'OLD WRITE AUTHORITY' not in str(agent.messages)
    assert a.session._resolve_spec_model(a.spec) == 'current-model'


def test_durable_restart_preserves_policy_and_drops_all_runtime_ports(authority):
    a = authority
    calls = []
    a.current['_tool_env'] = object()
    bind(a, on_agent_usage=lambda *args: calls.append(args), abort_check=lambda: False)
    payload = json.loads(json.dumps(a.spec.to_dict()))
    assert not any(key.startswith('retained_assignment_') for key in payload)
    payload['retained_assignment_parent'] = a.old
    restored = SubTaskSpec.from_dict(payload)
    assert restored.retained_assignment_parent is None
    assert restored.retained_assignment_usage is None
    assert restored.retained_assignment_abort_check is None
    assert restored.retained_execution_context == a.spec.retained_execution_context
    a.session._resume_messages[restored.id] = [
        {'role': 'system', 'content': 'OLD WRITE AUTHORITY'},
        {'role': 'user', 'content': 'interrupted objective'}]
    agent = a.session._make_agent(restored)
    assert agent.model == 'current-model'
    assert [tool_schema_name(t) for t in agent.tools] == ['read_files']
    assert agent.parent_task['config']['browserClientId'] == 'current-device'
    assert agent.parent_task['config']['_executableToolCatalog'] == [tool('read_files')]
    assert agent.parent_task['_workspaceRootPolicy'] == {'primaryPath': '', 'roots': {}}
    assert '_tool_env' not in agent.parent_task
    assert '_executionBudget' not in agent.parent_task
    assert agent.parent_task['_turnRequirementSource'].identity == (
        'conversation', 'current-turn', 'current-attempt', 'current-caller')
    assert agent._message_binding()['swarmRunId'] == a.session.task_id
    assert 'OLD WRITE AUTHORITY' not in str(agent.messages)


@pytest.mark.parametrize('corruption', ['missing', 'unknown', 'owner', 'catalog', 'workspace'])
def test_restart_unknown_authority_fails_closed(authority, corruption):
    a = authority
    bind(a)
    restored = SubTaskSpec.from_dict(deepcopy(a.spec.to_dict()))
    context = restored.retained_execution_context
    if corruption == 'missing':
        restored.retained_execution_context = None
    elif corruption == 'unknown':
        context['schema'] = 'future-unknown'
    elif corruption == 'owner':
        context['principal'] = PrincipalContext.user(
            subject_id='other', owner_user_id=772).to_payload()
    elif corruption == 'catalog':
        context['config']['_executableToolCatalog'] = [tool('write_file')]
    else:
        context['execution_context']['_tool_env'] = {'borrowed': 'stale'}
    with pytest.raises((ValueError, PermissionError)):
        a.session._make_agent(restored)


def test_current_principal_is_revalidated_before_entry(authority):
    a = authority
    bind(a)
    a.current['_principalContext'] = PrincipalContext.user(
        subject_id='different', owner_user_id=772).to_payload()
    with pytest.raises(PermissionError):
        a.session._make_agent(a.spec)


def test_assignment_cannot_widen_session_read_only_floor(authority):
    a = authority
    a.session._parent_task_proxy['config']['_toolOrchestration'] = {'multiAgent': 'read_only'}
    a.current['config']['_toolOrchestration'] = {'multiAgent': 'enabled'}
    bind(a, all_tools=[tool('read_files'), tool('write_file'), tool('run_command')])
    agent = a.session._make_agent(a.spec)
    assert [tool_schema_name(t) for t in agent.tools] == ['read_files']
    assert agent.parent_task['config']['_toolOrchestration']['multiAgent'] == 'read_only'


def test_current_assignment_shared_budget_and_usage_not_old_session(authority, monkeypatch):
    a = authority
    from lib.agent_core.execution_budget import share_execution_budget
    a.current['config']['executionLimits'] = {'max_tool_calls': 4, 'max_tokens': None, 'deadline': None}
    a.spec.execution_limits = {'max_tool_calls': 4, 'max_tokens': None, 'deadline': None}
    current_calls, old_calls = [], []
    a.session.on_agent_usage = lambda *args: old_calls.append(args)
    bind(a, on_agent_usage=lambda *args: current_calls.append(args))
    share_execution_budget(a.current, {'id': 'carrier', 'config': {}})
    agent = a.session._make_agent(a.spec)
    assert agent.parent_task['_executionBudget'] is a.current['_executionBudget']
    assert agent.parent_task['_executionBudgetControlTaskId'] == a.current['id']
    assert '_executionBudget' not in str(a.spec.to_dict())
    agent.result.status = 'completed'
    agent.result.total_tokens = 9
    monkeypatch.setattr(a.session, '_publish_completion_inbox', lambda *args: None)
    monkeypatch.setattr(a.session, '_persist_agent_snapshot', lambda: None)
    monkeypatch.setattr(a.session, '_emit_agent_complete', lambda *args: None)
    # Storage fence is orthogonal here; the actual mailbox is covered above.
    a.session._parent_task_proxy['_transientRuntime'] = True
    a.session._on_agent_complete_callback(a.spec, agent.result)
    a.session._on_agent_complete_callback(a.spec, agent.result)
    assert len(current_calls) == 1
    assert current_calls[0][0]['total_tokens'] == 9
    assert old_calls == []

    assert a.spec.retained_assignment_parent is None
    assert a.spec.retained_assignment_usage is None

@pytest.mark.parametrize('request_stale_write', [False, True])
def test_real_child_execution_uses_assignment_catalog_not_remembered_grant(
        authority, runtime, monkeypatch, request_stale_write):
    a = authority
    a.current = runtime.parent
    a.current['config']['codeExecEnabled'] = False
    a.session._parent_task_proxy['convId'] = ''
    # Storage-free fixture tests the real execution gateway rather than mocks.
    bind(a, all_tools=[TOOL], model='fixture-model', project_path='')
    import lib.swarm.retained as retained
    monkeypatch.setattr(retained, 'complete_assignment', lambda **kwargs: True)

    if request_stale_write:
        from tests.swarm.test_swarm_invocation import scripted_provider, reply, tool_call
        scripted_provider(monkeypatch, runtime, [
            reply(calls=[tool_call(name='write_file', arguments='{}')]), reply()])
    agent = a.session._make_agent(a.spec)
    # Real execution stays storage-free; this test owns the durable ACK port.
    monkeypatch.setattr(agent, '_checkpoint', lambda **kwargs: True)
    result = agent.run()

    assert result.status == ('failed' if request_stale_write else 'completed'), result.error_message
    if request_stale_write:
        assert 'write_file' in result.error_message
    assert runtime.calls[0]['tools'] == [TOOL]
    assert runtime.calls[0]['model'] == 'fixture-model'
    assert runtime.children[0]['_parentTaskId'] == runtime.parent['id']
    assert runtime.children[0]['config']['codeExecEnabled'] is False
    assert len(runtime.tool_calls) == (0 if request_stale_write else 1)
    if request_stale_write:
        evidence = [m.get('content', '') for m in runtime.calls[1]['messages']
                    if m.get('role') == 'tool']
        assert any('write_file' in str(m) for m in evidence)


def test_ordinary_children_keep_existing_session_authority(authority):
    a = authority
    spec = SubTaskSpec(role='general', objective='ordinary')
    agent = a.session._make_agent(spec)
    assert agent.parent_task is a.session._parent_task_proxy
    assert agent.parent_task['config']['browserClientId'] == 'old-device'
    assert agent.abort_check()
    assert 'write_file' in {tool_schema_name(t) for t in agent.tools}


def test_current_model_ref_survives_restart_without_old_provider_pin(authority):
    a = authority
    current_ref = {'model': 'current-model', 'selection': {'kind': 'auto'}}
    a.current['_requestedModelRef'] = current_ref
    a.current['config']['routing'] = {'strategy': 'current-policy'}
    a.current['config']['preferredProviderId'] = 'current-provider'
    a.current['config']['_pinned_provider_id'] = 'expired-native-slot'
    bind(a)
    restored = SubTaskSpec.from_dict(json.loads(json.dumps(a.spec.to_dict())))
    agent = a.session._make_agent(restored)
    from lib.tasks_pkg.child_invocation import resolve_child_config
    cfg = resolve_child_config(agent.parent_task, model=agent.model,
                               project_path=agent.project_path)
    assert cfg['modelRef'] == current_ref
    assert cfg['routing'] == {'strategy': 'current-policy'}
    assert cfg['preferredProviderId'] == 'current-provider'
    assert '_pinned_provider_id' not in cfg
    assert agent.parent_task['config'].get('_pinned_provider_id') is None
    assert restored.retained_execution_context['parent_turn_id'] == 'current-turn'


@pytest.mark.parametrize('mode', ['ack', 'no-ack', 'write-error', 'memory-error'])
def test_exact_terminal_checkpoint_precedes_retained_memory_and_cleanup(
        authority, runtime, monkeypatch, mode):
    a = authority
    a.current = runtime.parent
    a.session._parent_task_proxy['convId'] = ''
    bind(a, all_tools=[TOOL], model='fixture-model', project_path='')
    agent = a.session._make_agent(a.spec)
    events = []

    def checkpoint(*, final=False):
        if final:
            events.append(('checkpoint', agent.result.status, deepcopy(agent.messages)))
            if mode == 'write-error':
                raise RuntimeError('required terminal write failed')
            return mode != 'no-ack'
        return True

    def complete(**kwargs):
        events.append(('memory', kwargs['result']['status'], deepcopy(kwargs['messages'])))
        if mode == 'memory-error':
            raise RuntimeError('retained save failed')
        return True

    import lib.swarm.retained as retained
    monkeypatch.setattr(agent, '_checkpoint', checkpoint)
    monkeypatch.setattr(retained, 'complete_assignment', complete)
    if mode in {'write-error', 'no-ack'}:
        with pytest.raises(RuntimeError):
            agent.run()
        assert [event[0] for event in events] == ['checkpoint']
    else:
        result = agent.run()
        assert [event[0] for event in events] == (
            ['checkpoint', 'memory', 'checkpoint'] if mode == 'memory-error'
            else ['checkpoint', 'memory'])
        assert result.status == ('failed' if mode == 'memory-error' else 'completed')
        assert events[0][2] == events[1][2]
        assert events[0][2], 'exact full evidence exists before working-history reduction'
        if mode == 'memory-error':
            assert events[-1][1] == 'failed', 'lost retained memory publishes error evidence'
    assert agent._cleaned_up
    assert agent._child is None and not agent.messages


def test_actual_session_identity_is_bound_before_fresh_and_restart_entry(authority, monkeypatch):
    a = authority
    bind(a)
    changes = []
    import lib.swarm.retained_repository as retained

    def change(repository, scope, aid, transform):
        value = transform(deepcopy(repository.get(scope, aid)[0]))
        changes.append(value)
        return value

    monkeypatch.setattr(retained.RetainedAgentRepository, 'change', change)
    a.session._make_agent(a.spec)
    assert changes[-1]['run_session_id'] == 'original-swarm-run'
    assert changes[-1]['run_conversation_id'] == 'conversation'
    assert a.spec.retained_execution_context['parent_task_id'] == 'current-caller'
    a.session._resume_messages[a.spec.id] = [{'role': 'user', 'content': 'checkpoint'}]
    a.session._make_agent(a.spec)
    assert len(changes) == 2
    assert changes[-1]['run_session_id'] == 'original-swarm-run'

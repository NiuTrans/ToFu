"""Swarm handoff borrows detached execution policy, never delivery ownership.

Only provider replies, durable stores and worker scheduling are replaced. The
Master -> SubAgent -> child allocation, project dispatch, checkout fences and
cap-driven root LRU eviction execute on disposable repositories.
"""
from collections import OrderedDict
from copy import deepcopy
import json
import subprocess
from unittest.mock import Mock

import pytest

from lib.project_mod import config as roots
from lib.swarm.integration import _config, _rehydrate, _tools
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.tasks_pkg import workspace_delivery as delivery
from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.swarm.test_swarm_invocation import reply, scripted_provider, tool_call

runtime = goal_fixture.runtime
pytestmark = pytest.mark.unit

PROJECT_TOOLS = [
    {'type': 'function', 'function': {
        'name': name, 'description': 'Disposable workspace regression',
        'parameters': {'type': 'object', 'properties': {}}}}
    for name in ('write_file', 'run_command')
]


def _git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(runtime, tmp_path, monkeypatch):
    from lib.project_mod.write_tools import _temp_roots
    from lib.tasks_pkg.manager import runtime as task_runtime

    monkeypatch.setattr(roots, '_roots', {})
    monkeypatch.setattr(roots, '_conv_roots', OrderedDict())
    monkeypatch.setattr(roots, '_conv_primary', {})
    monkeypatch.setattr(roots, '_conv_cwd', OrderedDict())
    monkeypatch.setattr(roots, '_state', {**roots._state, 'path': None,
                                        'modifications': [], 'sessionId': None})
    monkeypatch.setattr(roots, 'MAX_CONV_ROOTS', 1)

    monkeypatch.setattr(_temp_roots, '_cache', set(), raising=False)
    canonical = tmp_path / 'canonical'
    selected, peer = tmp_path / 'selected', tmp_path / 'peer'
    reference = tmp_path / 'reference'
    canonical.mkdir()
    reference.mkdir()
    _git(canonical, 'init', '-b', 'main')
    _git(canonical, 'config', 'user.name', 'Swarm regression')
    _git(canonical, 'config', 'user.email', 'swarm@example.invalid')
    (canonical / 'f.txt').write_text('canonical protected\n')
    _git(canonical, 'add', 'f.txt')
    _git(canonical, 'commit', '-m', 'initial')
    _git(canonical, 'worktree', 'add', '--detach', str(selected))
    _git(canonical, 'worktree', 'add', '--detach', str(peer))
    (reference / 'notes.txt').write_text('readonly protected\n')

    parent = runtime.parent
    parent['config'].update(
        projectPath=str(selected), projectPaths=[str(selected), str(reference)],
        readOnlyPaths=[str(reference)], _canonicalProjectPath=str(canonical),
        _canonicalProjectPaths=[str(canonical), str(reference)],
        browserClientId='workspace-client',
        _pinned_provider_id='fixture-route',
        _executableToolCatalog=deepcopy(PROJECT_TOOLS),
        _toolExecutionScope='conversation',
        _toolDiscoveryPolicyByName={'write_file': {'mode': 'visible'}},
        routing={'mode': 'fixture-route', 'nested': {'selection': 'original'}},
    )
    parent['_workspaceDelivery'] = {'schema': 1, 'repositories': [{
        'canonicalRoot': str(canonical), 'workspacePath': str(selected),
        'workId': parent['id'], 'status': 'registered',
        'settlement': {'owner': parent['id']},
    }]}
    # The liveness probe and eviction implementation themselves remain real.
    monkeypatch.setattr(task_runtime.chat_task_runtime, 'get',
                        lambda key: parent if key == parent['id'] else None)
    monkeypatch.setattr(task_runtime, '_latest_task_for_conv',
                        lambda conv: parent['id'] if conv == parent['convId'] else None)
    roots._roots['reference'] = roots._make_root_state(str(reference), 'rw')
    roots._state['path'] = str(peer)
    before_global = deepcopy(roots.get_roots())
    delivery.ensure_execution_roots(parent)
    yield parent, canonical, selected, peer, reference, before_global


def _master(parent):
    return MasterOrchestrator(
        task_id=parent['id'], conv_id=parent['convId'], user_id=parent['_userId'],
        specs=[], project_path=parent['config']['projectPath'], model='fixture-model',
        all_tools=PROJECT_TOOLS, parent_config=parent['config'], parent_task=parent,
        output_dir='')


def _evict_parent(parent, peer):
    parent['status'] = 'done'
    roots.set_conv_roots('later-task', str(peer), [], [])
    assert delivery.execution_root_key(parent) not in roots._conv_roots


def _assert_borrowed(carrier, canonical, selected):
    assert carrier['_workspaceExecution']['repositories'] == [{
        'canonicalRoot': str(canonical), 'workspacePath': str(selected)}]
    for field in ('_workspaceDelivery', '_workspaceDeliveryEvidence',
                  '_workspaceDeliveryAcknowledgedOutcome',
                  '_workspaceDeliveryIntentPending', '_workspaceRegistration'):
        assert field not in carrier


def test_master_subagent_child_dispatch_keeps_fences_after_real_lru_eviction(
        workspace, runtime, monkeypatch):
    from lib.swarm.agent import SubAgent

    parent, canonical, selected, peer, reference, before_global = workspace
    master = _master(parent)
    _evict_parent(parent, peer)
    calls = [
        tool_call('readonly', 'write_file', json.dumps({
            'path': 'reference:notes.txt', 'content': 'unauthorized reference\n'})),
        tool_call('peer', 'write_file', json.dumps({
            'path': str(peer / 'f.txt'), 'content': 'unauthorized peer\n',
            'allow_outside_workspace': True})),
        tool_call('canonical-command', 'run_command', json.dumps({
            'command': f'printf unauthorized > {canonical / "f.txt"}',
            'working_dir': str(selected)})),
        tool_call('owned', 'write_file', json.dumps({
            'path': str(canonical / 'f.txt'), 'content': 'authorized child\n'})),
    ]
    final = ('Readonly reference and peer writes were refused. The selected workspace alone '
             'was updated; the canonical checkout remained unchanged, as required by the '
             'workspace execution policy.')
    # The ordinary intent-stall guard permits one confirmation after a tool
    # refusal; leave that policy running instead of stubbing the loop.
    scripted_provider(monkeypatch, runtime, [reply('', calls), reply(final), reply(final)])
    agent = master._make_agent(SubTaskSpec(
        id='workspace-child', role='general', objective='Check fences'))
    assert isinstance(agent, SubAgent)
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert len(runtime.children) == 1
    child = runtime.children[0]
    _assert_borrowed(master._parent_task_proxy, canonical, selected)
    _assert_borrowed(child, canonical, selected)
    assert child['_workspaceRootPolicy']['roots']['reference']['access'] == 'ro'
    assert master._parent_task_proxy['_workspaceRootPolicy']['roots']['reference']['access'] == 'ro'
    assert set(master._parent_task_proxy['_workspaceRootPolicy']['roots']) == {'reference', 'selected'}
    assert child['_workspaceRootPolicy'] is not master._parent_task_proxy['_workspaceRootPolicy']
    assert child['_workspaceExecution'] is not master._parent_task_proxy['_workspaceExecution']
    assert master._parent_task_proxy['_principalContext'] is parent['_principalContext']
    assert master._parent_task_proxy['_tool_env'] is parent['_tool_env']
    assert child['_principalContext'] == parent['_principalContext']
    assert master._parent_task_proxy['config']['routing'] == parent['config']['routing']
    assert child['config']['browserClientId'] == 'workspace-client'
    assert child['_pinned_provider_id'] == 'fixture-route'
    assert (reference / 'notes.txt').read_text() == 'readonly protected\n'
    assert (peer / 'f.txt').read_text() == 'canonical protected\n'
    assert (canonical / 'f.txt').read_text() == 'canonical protected\n'
    assert (selected / 'f.txt').read_text() == 'authorized child\n'
    tool_results = {message['tool_call_id']: message['content']
                    for message in runtime.calls[1]['messages']
                    if message.get('role') == 'tool'}
    assert 'READ-ONLY' in tool_results['readonly']
    assert 'workspace-isolation' in tool_results['peer']
    assert 'workspace-isolation' in tool_results['canonical-command']
    assert roots.get_roots() == before_global


def test_spawn_persist_rehydrate_roundtrip_detaches_execution_without_ownership(
        workspace, runtime, monkeypatch):
    parent, canonical, selected, peer, reference, before_global = workspace
    saved, sessions = [], []
    monkeypatch.setattr(_tools, '_get_session', lambda key: None)
    monkeypatch.setattr(_tools, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(_tools, '_set_session', lambda key, session, **kw: sessions.append(session))
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.run_in_background', lambda self: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session',
                        lambda key, **kw: saved.append(json.loads(json.dumps({'swarm_key': key, **kw}))))
    raw = _tools.execute_swarm_tool('spawn_agents', {
        'agents': [{'id': 'restored-child', 'role': 'general', 'objective': 'Check recovery'}]},
        task=parent, model='fixture-model', project_path=str(selected), all_tools=PROJECT_TOOLS)
    assert json.loads(raw)['status'] == 'async_launched', raw
    assert len(saved) == len(sessions) == 1
    snapshot = saved[0]['config']['workspace_execution_context']
    assert snapshot['schema'] == 1
    assert set(snapshot) == {'schema', '_workspaceExecution', '_workspaceRootPolicy'}
    _assert_borrowed(snapshot, canonical, selected)
    expected = deepcopy(snapshot)
    expected_config = deepcopy(saved[0]['config']['parent_cfg'])
    assert expected_config['_toolExecutionScope'] == 'conversation'
    assert expected_config['_executableToolCatalog'] == PROJECT_TOOLS
    assert expected_config['_toolDiscoveryPolicyByName'] == {'write_file': {'mode': 'visible'}}
    assert saved[0]['config']['principal_context'] == parent['_principalContext']
    assert saved[0]['config']['workspace_isolated'] is True
    parent['_workspaceDelivery']['repositories'][0]['workspacePath'] = str(peer)
    parent['_workspaceRootPolicy']['roots']['reference']['access'] = 'rw'
    parent['config']['routing']['nested']['selection'] = 'mutated'
    parent['config']['_executableToolCatalog'].clear()
    parent['config']['_toolDiscoveryPolicyByName']['write_file']['mode'] = 'mutated'
    assert snapshot == expected
    assert sessions[0]._parent_task_proxy['config'] == expected_config
    assert saved[0]['config']['parent_cfg'] == expected_config
    _evict_parent(parent, peer)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', lambda key, session, **kw: sessions.append(session))
    assert saved[0]['config']['tool_catalog'] == PROJECT_TOOLS
    assert _rehydrate._rebuild_tool_list(saved[0]['config']) == PROJECT_TOOLS
    assert _rehydrate._rebuild_tool_list(saved[0]['config']) is not saved[0]['config']['tool_catalog']
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'turnId': 'settled-parent', 'status': 'succeeded'}]})
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.rehydrate_in_background',
                        lambda self, agents: None)
    assert _rehydrate._rehydrate_one(saved[0])
    restored = sessions[-1]._parent_task_proxy
    _assert_borrowed(restored, canonical, selected)
    assert restored['_workspaceRootPolicy'] == expected['_workspaceRootPolicy']
    assert restored['_workspaceExecution'] is not snapshot['_workspaceExecution']
    assert restored['_workspaceRootPolicy'] is not snapshot['_workspaceRootPolicy']
    assert restored['config'] == expected_config
    assert restored['config']['_pinned_provider_id'] == 'fixture-route'
    assert restored['_principalContext'] == parent['_principalContext']
    assert restored['_principalContext'] is not saved[0]['config']['principal_context']
    assert restored['config']['browserClientId'] == 'workspace-client'
    assert restored['_userId'] == parent['_userId']
    _evict_parent(parent, peer)
    scripted_provider(monkeypatch, runtime, [reply('', [
        tool_call('recovered-readonly', 'write_file', json.dumps({
            'path': 'reference:notes.txt', 'content': 'unauthorized recovery\n'})),
        tool_call('recovered-owned', 'write_file', json.dumps({
            'path': str(canonical / 'f.txt'), 'content': 'authorized recovery\n'})),
    ]), reply('Recovered workspace update completed; readonly reference was left unchanged.'),
        reply('Recovered workspace update completed; readonly reference was left unchanged.')])
    result = sessions[-1]._make_agent(SubTaskSpec(
        id='recovered-child', role='general', objective='Check durable fences')).run()
    assert result.status == 'completed', result.error_message
    child = runtime.children[-1]
    _assert_borrowed(child, canonical, selected)
    assert child['_principalContext'] == restored['_principalContext']
    assert child['_workspaceRootPolicy']['roots']['reference']['access'] == 'ro'
    assert {'write_file', 'run_command'} <= {
        tool['function']['name'] for tool in child['_invocationTools']}
    assert restored['config']['_toolExecutionScope'] == 'conversation'
    assert (reference / 'notes.txt').read_text() == 'readonly protected\n'
    assert (canonical / 'f.txt').read_text() == 'canonical protected\n'
    assert (selected / 'f.txt').read_text() == 'authorized recovery\n'
    restored['_workspaceExecution']['repositories'][0]['workspacePath'] = str(peer)
    assert snapshot == expected
    assert roots.get_roots() == before_global


@pytest.mark.parametrize('damage', [
    'missing', 'legacy', 'linked-checkout', 'raw-config', 'schema', 'policy',
    'ownership', 'principal', 'missing-principal', 'missing-tools',
])
def test_isolated_recovery_without_declared_complete_snapshot_fails_closed(
        workspace, monkeypatch, damage):
    parent, canonical, selected, peer, reference, before_global = workspace
    config = _config._persist_config({}, 'fixture-model', None, str(selected),
                                    parent['config'], user_id=parent['_userId'],
                                    parent_task=_master(parent)._parent_task_proxy,
                                    tool_catalog=PROJECT_TOOLS)
    if damage == 'missing':
        config.pop('workspace_execution_context')
    elif damage == 'linked-checkout':
        config.pop('workspace_execution_context')
        config.pop('workspace_isolated')
        config['parent_cfg'].pop('_canonicalProjectPath', None)
        config['parent_cfg'].pop('_canonicalProjectPaths', None)
    elif damage == 'legacy':
        config.pop('workspace_execution_context')
        config.pop('workspace_isolated')
        config['parent_cfg']['canonicalProjectPath'] = str(canonical)
    elif damage == 'raw-config':
        config['parent_cfg']['workspace_execution_context'] = config.pop('workspace_execution_context')
    elif damage == 'schema':
        config['workspace_execution_context']['schema'] = 99
    elif damage == 'policy':
        config['workspace_execution_context'].pop('_workspaceRootPolicy')
    elif damage == 'missing-principal':
        config.pop('principal_context')
    elif damage == 'missing-tools':
        config.pop('tool_catalog')
    elif damage == 'principal':
        config['principal_context']['owner_user_id'] += 1
    else:
        config['workspace_execution_context']['_workspaceDelivery'] = parent['_workspaceDelivery']
    factory, inbox = Mock(), Mock()
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr('lib.agent_inbox.untombstone', inbox)
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'status': 'succeeded'}]})
    assert not _rehydrate._rehydrate_one({
        'swarm_key': parent['convId'], 'conv_id': parent['convId'],
        'task_id': parent['id'], 'status': 'running', 'config': config,
        'specs': [{'id': 'child', 'role': 'general', 'objective': 'Must not run'}],
        'agents': [{'agent_id': 'child', 'status': 'running'}],
    })
    factory.assert_not_called()
    inbox.assert_not_called()
    assert roots.get_roots() == before_global


def test_live_master_does_not_promote_workspace_policy_from_raw_config(workspace):
    parent, canonical, selected, peer, reference, before_global = workspace
    cfg = deepcopy(parent['config'])
    cfg['_workspaceExecution'] = deepcopy(parent['_workspaceDelivery'])
    cfg['_workspaceRootPolicy'] = deepcopy(parent['_workspaceRootPolicy'])
    master = MasterOrchestrator(
        task_id='raw-config-only', conv_id='raw-config-conv', user_id=parent['_userId'],
        specs=[], project_path=str(selected), model='fixture-model',
        all_tools=PROJECT_TOOLS, parent_config=cfg)
    assert '_workspaceExecution' not in master._parent_task_proxy
    assert '_workspaceRootPolicy' not in master._parent_task_proxy
    assert roots.get_roots() == before_global


def test_continue_intersects_new_subroot_access_in_real_resumed_child(workspace, runtime, monkeypatch):
    parent, canonical, selected, peer, reference, _ = workspace
    for checkout in (selected, peer):
        (checkout / 'protected').mkdir()
        (checkout / 'protected' / 'allowed').mkdir()
        (checkout / 'protected' / 'blocked.txt').write_text('protected\n')
        (checkout / 'protected' / 'allowed' / 'f.txt').write_text('exception\n')
    old = _config._persist_config(parent['config'], 'fixture-model', True,
        str(selected), parent['config'], user_id=parent['_userId'],
        parent_task=parent, tool_catalog=PROJECT_TOOLS)
    current = {**parent, 'config': {**parent['config'], 'projectPath': str(peer)}}
    current['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(canonical), 'workspacePath': str(peer)}]}
    current['_workspaceRootPolicy'] = {'primaryPath': str(peer), 'roots': {
        'work': {'path': str(peer), 'access': 'rw'},
        'protected': {'path': str(peer / 'protected'), 'access': 'ro'},
        'exception': {'path': str(peer / 'protected' / 'allowed'), 'access': 'rw'},
        'reference': {'path': str(reference), 'access': 'ro'}}}
    resumed = _rehydrate._continue_execution_parent(old, current)
    master = _master(resumed)
    policy = master._parent_task_proxy['_workspaceRootPolicy']
    assert roots.readonly_in_roots(str(selected / 'protected' / 'blocked.txt'), policy['roots'].values())
    assert not roots.readonly_in_roots(str(selected / 'protected' / 'allowed' / 'f.txt'), policy['roots'].values())
    calls = [tool_call('blocked', 'write_file', json.dumps({
                 'path': str(canonical / 'protected' / 'blocked.txt'), 'content': 'forbidden\n'})),
             tool_call('exception', 'write_file', json.dumps({
                 'path': str(canonical / 'protected' / 'allowed' / 'f.txt'), 'content': 'permitted\n'})),
             tool_call('sibling', 'write_file', json.dumps({
                 'path': str(canonical / 'f.txt'), 'content': 'permitted sibling\n'}))]
    final = 'Current restrictions were checked; only authorized old-checkout files were changed. ' * 3
    scripted_provider(monkeypatch, runtime, [reply('', calls), reply(final), reply(final)])
    result = master._make_agent(SubTaskSpec(id='continued', role='general', objective='Check new fences')).run()
    assert result.status == 'completed', result.error_message
    assert (selected / 'protected' / 'blocked.txt').read_text() == 'protected\n'
    assert (selected / 'protected' / 'allowed' / 'f.txt').read_text() == 'permitted\n'
    assert (selected / 'f.txt').read_text() == 'permitted sibling\n'
    assert (peer / 'protected' / 'allowed' / 'f.txt').read_text() == 'exception\n'
    assert (canonical / 'f.txt').read_text() == 'canonical protected\n'

"""Detached delegation policy preserves issued native identity and accounting."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.agent_core.native_process import NativeConfigurationError
from lib.tasks_pkg.child_invocation import resolve_child_config
from lib.tasks_pkg.native_parent import resolve_native_parent, share_native_parent
from tests.model_routing.test_native_route import principal, selection
from tests.tasks_pkg.test_native_invocation import binding

pytestmark = pytest.mark.unit


@pytest.fixture
def native_parent():
    loop = asyncio.new_event_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = binding(loop, executor)
        port._transient = True
        parent = dict(id='epoch', convId='conversation', status='running', _userId=7,
            _principalContext=principal().to_payload(), _nativePreparedInvocation=port,
            _turnId='turn', _attemptId='attempt',
            _model_routing_selection=selection(), config=dict(model='alpha',
                modelRef=selection().model.public_dict(), browserClientId='original'))
        _ensure_execution_budget(parent)
        try:
            yield parent, port
        finally:
            assert not port.repository.secret_calls
            assert not port.host._clients and port.host.image._image is None
            loop.run_until_complete(port.host.close())
            loop.close()


def test_frozen_policy_and_native_model_override_use_different_owned_carriers(native_parent):
    from lib.swarm.master import MasterOrchestrator
    from lib.tasks_pkg.requirement_source import requirement_source
    parent, port = native_parent
    master = MasterOrchestrator(task_id='original-session', conv_id=parent['convId'],
        user_id=7, specs=[], parent_task=parent, parent_config=parent['config'])
    carrier = master._parent_task_proxy
    assert master.task_id == 'original-session'
    assert carrier['id'] == parent['id']
    assert resolve_native_parent(carrier) == (port, parent)
    assert requirement_source(carrier) == requirement_source(parent)
    assert '_turnId' not in carrier and '_attemptId' not in carrier
    assert '_nativePreparedInvocation' not in carrier
    parent['config']['browserClientId'] = 'later'
    cfg = resolve_child_config(carrier, model='beta')
    assert cfg['browserClientId'] == 'original'
    assert cfg['modelRef'] == dict(creator_id='creator', model_id='beta')
    assert list(parent['_executionBudget']._active) == ['epoch']
    nested = {**carrier, 'config': deepcopy(carrier['config'])}
    share_native_parent(carrier, nested)
    assert resolve_native_parent(nested) == (port, parent)


@pytest.mark.parametrize('fault', [
    'owner', 'tenant', 'principal', 'conversation', 'identity', 'budget',
    'retired_port', 'forged_binding', 'dual_binding', 'retired_parent',
])
def test_changed_execution_authority_refuses_native_model_selection(native_parent, fault):
    parent, port = native_parent
    carrier = {**parent, '_principalContext': deepcopy(parent['_principalContext'])}
    share_native_parent(parent, carrier)
    if fault == 'owner': carrier['_userId'] = 8
    elif fault == 'tenant': carrier['_tenant_id'] = 'foreign'
    elif fault == 'principal': carrier['_principalContext'] = principal(8).to_payload()
    elif fault == 'conversation': carrier['convId'] = 'foreign'
    elif fault == 'identity': carrier['id'] = 'foreign'
    elif fault == 'budget': carrier['_executionBudget'] = object()
    elif fault == 'retired_port': parent['_nativePreparedInvocation'] = None
    elif fault == 'forged_binding': carrier['_nativeInvocationParent'] = {'task': parent}
    elif fault == 'dual_binding': carrier['_nativePreparedInvocation'] = port
    elif fault == 'retired_parent': parent['status'] = 'done'
    with pytest.raises(NativeConfigurationError):
        resolve_child_config(carrier, model='beta')


@pytest.mark.parametrize('delegate_mode', [False, True])
def test_chat_spawn_with_worker_model_override_borrows_the_live_native_parent(
        native_parent, monkeypatch, delegate_mode):
    import json
    from threading import Event
    from lib.swarm import master
    from lib.swarm.integration import execute_swarm_tool, get_active_session
    from tests.swarm.test_retained_assignment_authority import tool
    from tests.swarm.test_swarm_async import _FakeAgent, _reset_global_state
    parent, _ = native_parent
    if delegate_mode:
        parent['_delegationToolPolicy'] = {'task': {'_executable_tool_catalog': [tool('read_files')]}}
    observed, built, build = {}, Event(), master._build_sub_agent

    def factory(spec, **kwargs):
        worker = build(spec, **kwargs)
        observed.update(parent_task=worker.parent_task, model=worker.model)
        built.set()
        return _FakeAgent(spec)
    monkeypatch.setattr(master, '_build_sub_agent', factory)
    _reset_global_state(parent['convId'])
    try:
        receipt = json.loads(execute_swarm_tool('spawn_agents', {'agents': [{'id': 'reviewer',
            'role': 'general', 'objective': 'Review the change', 'model': 'beta'}]},
            task=parent, model='alpha', all_tools=[tool('read_files')]))
        assert receipt['status'] == 'async_launched', receipt
        assert built.wait(5), 'launched assignment did not reach its worker'
        assert observed['model'] == 'beta'
        cfg = resolve_child_config(observed['parent_task'], model=observed['model'])
        assert cfg['modelRef'] == dict(creator_id='creator', model_id='beta')
    finally:
        session = get_active_session(parent['convId'])
        if session is not None:
            session.abort()
            if session._driver_thread is not None:
                session._driver_thread.join(timeout=10)
                assert not session._driver_thread.is_alive()
        _reset_global_state(parent['convId'])


def test_native_swarm_session_keeps_the_live_parent_as_priced_fee_receiver(native_parent):
    import json
    from lib.agent_core.execution_budget import close_execution_budget, inherit_execution_budget
    from lib.swarm.master import MasterOrchestrator
    from tests.model_routing.test_native_issued_route import group
    from tests.model_routing.test_native_route import Repository
    parent, port = native_parent
    port._transient, port.pricing_resolver = False, object()
    port._profile = json.dumps(dict(json.loads(port._profile),
        terminal_persistence={'attempts': 2, 'ack_ms': 5000}))
    session = MasterOrchestrator(task_id='session', conv_id=parent['convId'], user_id=7,
        specs=[], parent_task=parent, parent_config=parent['config'])
    issued = group(Repository())
    child = {'id': 'worker', '_parentTaskId': 'epoch', 'status': 'pending', '_userId': 7,
        '_transientRuntime': True, '_model_routing_group': issued,
        '_model_routing_selection': selection(), '_pinned_provider_id': issued.pin_id, 'config': {}}
    inherit_execution_budget(session._parent_task_proxy, child)
    try:
        port.bind_transient_child(child, parent_task_id='epoch', parent_task=parent)
        assert child['_nativePreparedInvocation'].fee_transfer_target is parent
    finally:
        close_execution_budget(child)


@pytest.fixture
def late_swarm(native_parent, monkeypatch):
    """Drive a real spawn whose dependent worker unblocks after the parent settles.

    The prerequisite is a controlled stand-in that holds the live turn open;
    every dependent worker is the production SubAgent and child factory.
    Storage, auto-continuation and model transport are not reachable.
    """
    import json
    from threading import Event
    from lib.swarm import integration, master
    from lib.tasks_pkg import manager
    from tests.swarm.test_retained_assignment_authority import tool
    from tests.swarm.test_swarm_async import _FakeAgent, _reset_global_state
    parent, _ = native_parent
    started, release, build = Event(), Event(), master._build_sub_agent
    created, saved, woken = [], [], []

    class Prerequisite(_FakeAgent):
        def run(self):
            started.set()
            assert release.wait(10), 'parent settlement did not release the prerequisite'
            return self.result

    def factory(spec, **kwargs):
        return Prerequisite(spec) if spec.id == 'prerequisite' else build(spec, **kwargs)
    allocate = manager.create_task
    monkeypatch.setattr(master, '_build_sub_agent', factory)
    monkeypatch.setattr(manager, 'create_task',
        lambda *args, **kwargs: created.append(args) or allocate(*args, **kwargs))
    monkeypatch.setattr('lib.swarm.persistence.save_agent',
        lambda key, agent_id, **row: saved.append((agent_id, row)))
    monkeypatch.setattr(integration, '_maybe_autocontinue', lambda *args, **kwargs: woken.append(args))
    _reset_global_state(parent['convId'])

    def spawn(agents):
        receipt = json.loads(integration.execute_swarm_tool('spawn_agents', {'agents': agents},
            task=parent, model='alpha', all_tools=[tool('read_files')]))
        assert receipt['status'] == 'async_launched', receipt
        assert started.wait(5), 'prerequisite did not start during the live turn'
        return integration.get_active_session(parent['convId'])

    def settle(session):
        release.set()
        session._driver_thread.join(timeout=10)
        assert not session._driver_thread.is_alive()
        return {agent_id: row for agent_id, row in saved if row['result']}
    try:
        yield SimpleNamespace(parent=parent, spawn=spawn, settle=settle,
                              created=created, woken=woken)
    finally:
        release.set()
        session = integration.get_active_session(parent['convId'])
        if session is not None:
            session.abort()
            if session._driver_thread is not None:
                session._driver_thread.join(timeout=10)
                assert not session._driver_thread.is_alive()
        _reset_global_state(parent['convId'])


@pytest.mark.parametrize('port_released', [False, True])
@pytest.mark.parametrize('wave', ['initial', 'followup'])
def test_worker_unblocked_after_native_parent_settles_is_refused_before_allocation(
        late_swarm, wave, port_released):
    """Native authority is turn-scoped; a late start settles with a typed reason.

    The spawning turn's route, fee receiver and shared account end at its
    terminal stamp, before the issued port is released. A dependent that only
    unblocks afterwards must fail before carrier/account/route allocation and
    tell the parent conversation why, instead of an opaque binding error.
    """
    from lib import agent_inbox
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    parent = late_swarm.parent
    dependent = {'id': 'dependent', 'role': 'general', 'objective': 'Apply the evidence',
                 'depends_on': ['prerequisite']}
    prerequisite = {'id': 'prerequisite', 'role': 'general', 'objective': 'Collect the evidence'}
    if wave == 'initial':
        session = late_swarm.spawn([prerequisite, dependent])
    else:
        # A later wave freezes the current caller's grant through the shared
        # assignment boundary, then waits on the still-running first wave.
        session = late_swarm.spawn([prerequisite])
        assert late_swarm.spawn([dependent]) is session
    assert stamp_chat_task_terminal(parent, status='done', finish_reason='stop')
    if port_released:
        _release_heavy_task_state(parent)
        assert parent['_nativePreparedInvocation'] is None
    # Status projection of the unstarted worker never needs live authority.
    assert session.get_status()['dependent']['status'] == 'pending'
    settled = late_swarm.settle(session)
    refused = settled['dependent']
    assert refused['status'] == 'failed', refused
    assert refused['result']['recovery_reason'] == 'native_parent_settled', refused['result']
    assert refused['result']['error_message'].startswith('native_parent_settled: '), refused['result']
    assert refused['result']['total_tokens'] == 0 and refused['result']['tool_calls_made'] == 0
    assert session.get_status()['prerequisite']['status'] == 'completed'
    assert not late_swarm.created, 'late worker allocated a child carrier'
    assert list(parent['_executionBudget']._active) == ['epoch']
    updates = {item['agent_id']: item for item in agent_inbox.pending_items(
        parent['convId'], modes=['swarm-update'])}
    assert updates['dependent']['status'] == 'failed'
    assert '<error>native_parent_settled: ' in updates['dependent']['value']
    assert late_swarm.woken, 'settled swarm did not hand off to parent continuation'


@pytest.mark.parametrize('stopped', [False, True])
def test_late_worker_refusal_is_typed_unless_owner_stop_settled_the_issuer(native_parent, stopped):
    """Stop keeps precedence even when it lands after scheduler admission."""
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    parent, _ = native_parent
    carrier = {**parent, '_principalContext': deepcopy(parent['_principalContext'])}
    share_native_parent(parent, carrier)
    worker = SubAgent(SubTaskSpec(id='late', role='general', objective='Late work'),
        parent_task=carrier, all_tools=[], model='alpha',
        abort_check=lambda: bool(parent.get('aborted')))
    parent['aborted'] = stopped
    assert stamp_chat_task_terminal(parent, status='aborted' if stopped else 'done',
                                    finish_reason='aborted' if stopped else 'stop')
    result = worker.run()
    assert result.status == ('cancelled' if stopped else 'failed')
    assert result.recovery_reason == ('' if stopped else 'native_parent_settled')
    assert result.error_message.startswith('native_parent_settled: ')
    assert list(parent['_executionBudget']._active) == ['epoch']


@pytest.mark.parametrize('port_released', [False, True])
def test_settled_issuer_keeps_identity_for_projection_but_refuses_child_entry(
        native_parent, monkeypatch, port_released):
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    from lib.tasks_pkg.native_parent import NativeParentSettled
    parent, _ = native_parent
    carrier = {**parent, '_principalContext': deepcopy(parent['_principalContext'])}
    share_native_parent(parent, carrier)
    assert stamp_chat_task_terminal(parent, status='done', finish_reason='stop')
    if port_released:
        _release_heavy_task_state(parent)
    nested = {**carrier, 'config': deepcopy(carrier['config'])}
    share_native_parent(carrier, nested)
    assert nested['_nativeInvocationParent'] is carrier['_nativeInvocationParent']
    allocated = []
    monkeypatch.setattr(manager, 'create_task', lambda *args, **kwargs: allocated.append(args))
    for source in (parent, carrier, nested):
        for attempt in (lambda: resolve_child_config(source, model='beta'),
                        lambda: create_child_invocation(source, model='alpha', tools=[],
                            objective='Late work', event_sink=lambda event: None,
                            abort_check=lambda: False)):
            with pytest.raises(NativeParentSettled, match='^native_parent_settled: ') as refused:
                attempt()
            assert isinstance(refused.value, NativeConfigurationError)
            assert refused.value.reason == 'native_parent_settled'
    assert not allocated
    assert list(parent['_executionBudget']._active) == ['epoch']
    # Exact identity precedes any lifecycle disclosure to a changed carrier.
    carrier['_userId'] = 8
    with pytest.raises(NativeConfigurationError) as foreign:
        resolve_native_parent(carrier)
    assert not isinstance(foreign.value, NativeParentSettled)


def test_request_config_cannot_issue_native_binding(native_parent):
    parent, port = native_parent
    carrier = dict(config={'_nativeInvocationParent': parent,
                           '_nativePreparedInvocation': port})
    assert resolve_native_parent(carrier) == (None, carrier)
    with pytest.raises(ValueError, match='distinct'):
        share_native_parent(parent, parent)

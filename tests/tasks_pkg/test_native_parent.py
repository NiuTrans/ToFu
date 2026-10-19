"""Detached delegation policy preserves issued native identity and accounting."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

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


def test_request_config_cannot_issue_native_binding(native_parent):
    parent, port = native_parent
    carrier = dict(config={'_nativeInvocationParent': parent,
                           '_nativePreparedInvocation': port})
    assert resolve_native_parent(carrier) == (None, carrier)
    with pytest.raises(ValueError, match='distinct'):
        share_native_parent(parent, parent)

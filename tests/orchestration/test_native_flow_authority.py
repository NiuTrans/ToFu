"""Native graph admission uses the exact live parent before any role callback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.identity import PrincipalContext
from lib.orchestration.engine import FlowExecutor,FlowExecutionError
from tests.orchestration import test_orchestration_engine as oracle
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.tasks_pkg.test_native_invocation import binding,principal

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('fault',[None,'cloned_actor','foreign_principal','retired','conflicting_library'])
def test_graph_admission_requires_original_actor_and_scheduler(fault,native_library):
    async def run():
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            port=binding(asyncio.get_running_loop(),callbacks)
            port.flow_scheduler=native_library
            parent=dict(id='epoch',_userId=7,status='running',
                _principalContext=principal().to_payload(),_nativePreparedInvocation=port)
            _ensure_execution_budget(parent)
            options={}
            if fault=='cloned_actor':parent=dict(parent)
            elif fault=='foreign_principal':
                parent['_principalContext']=PrincipalContext.user(subject_id='foreign',owner_user_id=7).to_payload()
            elif fault=='retired':parent['status']='done'
            elif fault=='conflicting_library':options['native_flow_library']=object()
            calls=[]
            def role(node,*args):calls.append(node['id']);return {'output':'done'}
            try:
                if fault:
                    with pytest.raises(FlowExecutionError):
                        FlowExecutor(oracle.LinearTest()._defn(),parent_task=parent,agent_runner=role,**options)
                    assert not calls
                else:
                    result=FlowExecutor(oracle.LinearTest()._defn(),parent_task=parent,agent_runner=role).run()
                    assert result['ok'] and calls==['w']
                assert not native_library._graphs and not port.host._clients
            finally:await port.host.close()
    asyncio.run(run())

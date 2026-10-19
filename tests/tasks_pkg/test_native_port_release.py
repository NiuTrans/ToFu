"""Durable terminal carriers release native application owners without losing Stop."""
import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.tasks_pkg.manager._persist import _release_heavy_task_state
from tests.tasks_pkg.test_native_invocation import binding
from tests.model_routing.test_native_route import selection

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('status,pending',[('done',False),('error',False),('aborted',False),
                                         ('done',True),('running',False)])
def test_native_owner_release_waits_for_durable_terminal_acceptance(status,pending):
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            host=weakref.ref(port.host)
            task={'status':status,'_terminalPersistencePending':pending,
                  '_nativePreparedInvocation':port,'_model_routing_selection':selection()}
            del port
            _release_heavy_task_state(task)
            accepted=status!='running' and not pending
            assert (task['_nativePreparedInvocation'] is None) is accepted
            assert (task['_model_routing_selection'] is None) is accepted
            gc.collect()
            assert (host() is None) is accepted
            if not accepted:await host().close()
    asyncio.run(run())

"""Worker bridge preserves prepared context, loop ownership and terminal authority."""
import asyncio
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import NativeConfigurationError
from lib.model_routing.native_route import Result
from lib.tasks_pkg.native_host import NativeHost
from lib.tasks_pkg.native_invocation import NativePreparedInvocation
from tests.agent_core.test_native_process import budget
from tests.model_routing.test_native_route import principal,selection,Repository
from tests.tasks_pkg.test_native_prepared import prepared

pytestmark=pytest.mark.unit


def binding(loop,executor,*,token_count=False):
    image=NativeArchiveImage(archive=Path('/missing'),sha256='0'*64,application_version='fixture',budget=budget())
    return NativePreparedInvocation(host=NativeHost(image,token_count=token_count),loop=loop,principal=principal(),repository=Repository(),
        selection=selection(),configuration={'epoch':'epoch'},budget=budget(),executor=executor,
        working_directory=Path('/tmp'),pending_calls=8,pending_bytes=4096,
        resource_policy={'frame_maximum':65536})


def test_resume_invoice_refusal_precedes_process_acquisition_and_keeps_native_authority():
    import copy
    from tests.billing.test_native_task_cost import task as quoted_task
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            port.pricing_resolver=object()
            request=prepared();quote=quoted_task()
            request.task.update(_billingUserId='wallet-owner',apiRounds=quote['apiRounds'],
                _checkpointApiRounds=copy.deepcopy(quote['apiRounds']),
                _executionBudget=quote['_executionBudget'])
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task') as native:
                with pytest.raises(ValueError,match='resume lineage'):
                    await asyncio.to_thread(port.run,request)
                native.assert_not_called()
            assert request.task['_nativePricingRequired'] is True
            assert not port.host._clients and port.host.image._image is None
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['invalid_credits', 'changed_prefix'])
def test_legacy_invoice_validation_refuses_without_mutating_either_history(fault):
    import copy
    from types import SimpleNamespace
    from tests.billing.test_native_task_cost import task as quoted_task
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            port.pricing_resolver=object()
            request=prepared();quote=quoted_task()
            if fault=='invalid_credits':
                quote['apiRounds'][0]['nativeCredits']['total'] += 1
            prefix=copy.deepcopy(quote['apiRounds'])
            request.task.update(_billingUserId='wallet-owner',apiRounds=quote['apiRounds'],
                _checkpointApiRounds=prefix,_executionBudget=quote['_executionBudget'])
            if fault=='changed_prefix':
                request.task['apiRounds'][0]['round'] += 1
            rows_before=copy.deepcopy(request.task['apiRounds'])
            prefix_before=copy.deepcopy(prefix)
            with patch('lib.conversation_sync.dispatch_contract.read_accepted_resume_lineage',
                       return_value=SimpleNamespace(source_task_id='previous-executor')), \
                 patch('lib.tasks_pkg.native_invocation.run_native_prepared_task') as native:
                with pytest.raises(ValueError):
                    await asyncio.to_thread(port.run,request)
                native.assert_not_called()
            assert request.task['apiRounds'] is quote['apiRounds']
            assert request.task['_checkpointApiRounds'] is prefix
            assert request.task['apiRounds']==rows_before
            assert prefix==prefix_before
            assert request.task['_nativePricingRequired'] is True
            assert not port.host._clients and port.host.image._image is None
            await port.host.close()
    asyncio.run(run())


def test_worker_bridge_uses_serving_loop_and_closes_owned_client_scope():
    async def run():
        scope=ContextVar('native-worker-scope',default='loop')
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();clients=[]
            async def native(client,repo,selected,configuration,**kwargs):
                assert asyncio.get_running_loop() is port.loop
                assert scope.get()=='worker'
                assert kwargs['request'] is request
                assert kwargs['request'].messages is request.messages
                assert kwargs['executor'] is executor
                assert configuration['rounds'] is None
                clients.append(client);request.task['status']='done'
                return Result({'type':'done','status':'done'},{})
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=native):
                def execute():scope.set('worker');return port.run(request)
                result=await asyncio.to_thread(execute)
            assert result.event['status']=='done'
            assert scope.get()=='loop'
            assert clients[0]._closed and not port.host._clients
            await port.host.close()
    asyncio.run(run())


def test_loop_thread_and_foreign_owner_refuse_before_native_dispatch():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared()
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task') as native:
                with pytest.raises(NativeConfigurationError):port.run(request)
                request.task['_userId']=8
                with pytest.raises(NativeConfigurationError):await asyncio.to_thread(port.run,request)
                native.assert_not_called()
            await port.host.close()
    asyncio.run(run())


def test_same_bounded_callback_pool_refuses_before_loop_submission():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task') as native:
                with pytest.raises(NativeConfigurationError):
                    await asyncio.wrap_future(executor.submit(port.run,prepared()))
                native.assert_not_called()
            await port.host.close()
    asyncio.run(run())


def test_native_error_propagates_after_client_cleanup_without_reexecution():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();calls=[]
            failure=OSError('terminal delivery failed')
            async def native(*args,**kwargs):calls.append(args[0]);request.task['status']='error';raise failure
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=native):
                with pytest.raises(OSError) as caught:await asyncio.to_thread(port.run,request)
                assert caught.value is failure
            assert len(calls)==1 and calls[0]._closed and not port.host._clients
            await port.host.close()
    asyncio.run(run())


def test_native_cancellation_keeps_its_type_after_joined_worker_bridge():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();clients=[]
            async def native(client,*args,**kwargs):
                clients.append(client);request.task['status']='aborted';raise asyncio.CancelledError
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=native):
                with pytest.raises(asyncio.CancelledError):await asyncio.to_thread(port.run,request)
            assert request.task['status']=='aborted'
            assert clients[0]._closed and not port.host._clients
            await port.host.close()
    asyncio.run(run())


def test_worker_interruption_signals_stop_and_joins_native_scope_before_propagating():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();entered=threading.Event();joined=[]
            failure=KeyboardInterrupt('worker interrupted')
            schedule=asyncio.run_coroutine_threadsafe
            class Future:
                def __init__(self,actual):self.actual=actual;self.first=True
                def done(self):return self.actual.done()
                def result(self):
                    if self.first:
                        self.first=False;assert entered.wait(3);raise failure
                    return self.actual.result()
            async def native(client,*args,**kwargs):
                listener=request.task['abort_event'].subscribe_async();entered.set()
                try:await listener.future
                finally:listener.close();joined.append(client)
                request.task['status']='aborted'
                return Result({'type':'aborted','status':'aborted'},{})
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=native),\
                    patch('asyncio.run_coroutine_threadsafe',side_effect=lambda work,loop:Future(schedule(work,loop))):
                with pytest.raises(KeyboardInterrupt) as caught:await asyncio.to_thread(port.run,request)
                assert caught.value is failure
            assert request.task['abort_event'].is_set()
            assert len(joined)==1 and joined[0]._closed and not port.host._clients
            await port.host.close()
    asyncio.run(run())


def test_checkpoint_factory_cancellation_joins_worker_and_closes_policy_before_client_scope():
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared()
            entered,release=threading.Event(),threading.Event();policies=[]
            loop_thread=threading.get_ident()
            def factory(request,owner,callback_executor,profile):
                assert threading.get_ident()!=loop_thread
                entered.set();assert release.wait(3)
                policy=NativeCheckpointPolicy(principal=owner,task=request.task,storage=SelectedStorage(),
                    executor=callback_executor,epoch=profile['epoch'],binding='binding',token='lease',
                    header_bytes=4096,snapshot_bytes=65536,identity_bytes=1024,io_timeout_ms=5000,
                    restore=lambda *args:None)
                policies.append(policy)
                return policy
            port.checkpoint_factory=factory
            work=asyncio.create_task(port._run(request))
            try:
                assert await asyncio.to_thread(entered.wait,3)
                work.cancel();await asyncio.sleep(0)
                assert not work.done()
            finally:release.set()
            with pytest.raises(asyncio.CancelledError):await work
            assert policies[0].closed and policies[0].storage is None
            assert not port.host._clients
            await port.host.close()
    asyncio.run(run())


def test_checkpoint_factory_policy_closes_when_prepared_route_fails():
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();policies=[]
            def factory(request,owner,callback_executor,profile):
                policy=NativeCheckpointPolicy(principal=owner,task=request.task,storage=SelectedStorage(),
                    executor=callback_executor,epoch=profile['epoch'],binding='binding',token='lease',
                    header_bytes=4096,snapshot_bytes=65536,identity_bytes=1024,io_timeout_ms=5000,
                    restore=lambda *args:None)
                policies.append(policy)
                return policy
            port.checkpoint_factory=factory
            failure=OSError('issued route changed')
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=failure):
                with pytest.raises(OSError) as caught:await asyncio.to_thread(port.run,request)
                assert caught.value is failure
            assert policies[0].closed and policies[0].storage is None
            assert not port.host._clients
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('foreign_task',[False,True])
def test_checkpoint_factory_owns_stable_epoch_and_refuses_foreign_task_before_launch(foreign_task):
    import json
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);request=prepared();policies=[]
            original=json.loads(port._profile)
            def factory(request,owner,callback_executor,profile):
                policy=NativeCheckpointPolicy(principal=owner,task=dict(request.task) if foreign_task else request.task,
                    storage=SelectedStorage(),executor=callback_executor,epoch='accepted-turn-epoch',
                    binding='binding',token='lease',header_bytes=4096,snapshot_bytes=65536,
                    identity_bytes=1024,io_timeout_ms=5000,restore=lambda *args:None)
                policies.append(policy);return policy
            port.checkpoint_factory=factory
            async def native(client,repository,selection,configuration,**kwargs):
                assert configuration['epoch']=='accepted-turn-epoch'
                assert kwargs['checkpoint_policy'] is policies[0]
                return Result({'type':'done','status':'done'},{})
            with patch('lib.tasks_pkg.native_invocation.run_native_prepared_task',side_effect=native) as launch:
                if foreign_task:
                    with pytest.raises(NativeConfigurationError,match='ownership differs'):
                        await asyncio.to_thread(port.run,request)
                    launch.assert_not_called();assert port.host.image._image is None
                else:await asyncio.to_thread(port.run,request)
            assert policies[0].closed and not port.host._clients
            assert json.loads(port._profile)==original
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['owner','parent','lifetime','pin','selection','tenant'])
def test_native_child_binding_rejects_foreign_issued_authority_before_image(fault):
    from tests.model_routing.test_native_issued_route import group
    from lib.model_routing.domain import NativeModelSelection,ModelRef
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            port._transient=True
            repository=Repository();issued=group(repository)
            child={'id':'child','_parentTaskId':'epoch','status':'pending','_userId':7,'_transientRuntime':True,
                '_model_routing_group':issued,'_model_routing_selection':selection(),'_pinned_provider_id':issued.pin_id,'config':{}}
            if fault=='owner':child['_userId']=8
            elif fault=='parent':child['_parentTaskId']='foreign'
            elif fault=='lifetime':child['_transientRuntime']=False
            elif fault=='pin':child['_pinned_provider_id']='foreign'
            elif fault=='selection':child['_model_routing_selection']=NativeModelSelection(ModelRef('other','other'),None)
            else:
                from types import MappingProxyType
                issued.route_identity=MappingProxyType(dict(issued.route_identity,tenant_id='foreign'))
            with pytest.raises(NativeConfigurationError):port.bind_transient_child(child,parent_task_id='epoch')
            assert '_nativePreparedInvocation' not in child
            assert port.host.image._image is None and not port.host._clients
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('unsupported', ['journal', 'foreign_receiver'])
def test_priced_child_recovery_scope_refuses_before_provider_or_image(unsupported):
    from tests.model_routing.test_native_issued_route import group
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port = binding(asyncio.get_running_loop(), executor)
            port.pricing_resolver = object()
            if unsupported == 'journal':
                port.checkpoint_factory = lambda *args: None
            else:
                port.fee_transfer_target = {'id': 'ancestor'}
            issued = group(Repository())
            child = {'id': 'child', '_parentTaskId': 'epoch', 'status': 'pending',
                '_userId': 7, '_transientRuntime': True, '_model_routing_group': issued,
                '_model_routing_selection': selection(), '_pinned_provider_id': issued.pin_id,
                'config': {}}
            with pytest.raises(NativeConfigurationError, match='not admitted'):
                port.bind_transient_child(child, parent_task_id='epoch', parent_task={'id': 'epoch'})
            assert '_nativePreparedInvocation' not in child
            assert not port.host._clients and port.host.image._image is None
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('read_fails', [False, True])
def test_cancelled_invoice_source_read_joins_worker_before_native_acquisition(read_fails):
    from contextvars import ContextVar
    from types import SimpleNamespace
    async def run():
        context = ContextVar('invoice-context', default='missing')
        context.set('owned-invoice')
        entered = threading.Event(); release = threading.Event(); settled = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            port = binding(asyncio.get_running_loop(), executor)
            port.pricing_resolver = object()
            request = prepared()
            row = {'taskId': 'paid-child', 'billableTaskId': 'source'}
            request.task.update(_billingUserId='opaque-wallet', apiRounds=[row], _checkpointApiRounds=[row])
            def read(*args, **kwargs):
                assert context.get() == 'owned-invoice'
                entered.set()
                assert release.wait(3)
                settled.append(True)
                if read_fails:raise ValueError('source retired during cancellation')
            with patch('lib.billing.native_settlement.restore_accepted_invoice_prefix', side_effect=read), \
                    patch('lib.conversation_sync.dispatch_contract.read_accepted_resume_lineage',
                        return_value=SimpleNamespace(source_task_id='source')), \
                    patch('lib.tasks_pkg.native_invocation.run_native_prepared_task') as native:
                pending = asyncio.create_task(port._run(request))
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    pending.cancel()
                    await asyncio.sleep(.02)
                    assert not pending.done() and not settled
                    assert not port.host._clients and port.host.image._image is None
                    release.set()
                    with pytest.raises(asyncio.CancelledError):await pending
                    assert settled == [True]
                    native.assert_not_called()
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                        try:await pending
                        except asyncio.CancelledError:pass
                    await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', [None, 'owner', 'tenant', 'epoch', 'retired',
    'cloned_parent', 'foreign_repository', 'ambiguous_identity', 'unauthorized', 'wire_id', 'unqualified_checkpoint'])
def test_child_model_selection_uses_exact_owned_catalog_before_secret_or_process(fault):
    import copy
    from lib.agent_core.execution_budget import _ensure_execution_budget
    from lib.model_routing.domain import ModelRef, NativeModelSelection
    from lib.model_routing.repository import OwnerBoundary
    from lib.tasks_pkg.child_invocation import resolve_child_config
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor)
            port._transient=True
            parent=dict(id='epoch',status='running',_userId=7,
                _principalContext=principal().to_payload(),_nativePreparedInvocation=port,
                _model_routing_selection=selection(),config=dict(model='alpha',
                    modelRef=selection().model.public_dict()))
            _ensure_execution_budget(parent)
            model='beta'
            if fault=='owner':parent['_userId']=8
            elif fault=='tenant':parent['_principalContext']['tenant_id']='foreign'
            elif fault=='epoch':parent['id']='foreign'
            elif fault=='retired':parent['status']='done'
            elif fault=='cloned_parent':parent=dict(parent)
            elif fault=='foreign_repository':port.repository.boundary=OwnerBoundary.create(8)
            elif fault=='ambiguous_identity':
                port.repository.document['creators'].append(dict(creator_id='other',name='Other'))
                row=copy.deepcopy(port.repository.document['models'][1])
                row['creator_id']='other';port.repository.document['models'].append(row)
            elif fault=='unauthorized':
                for credential in port.repository.document['credentials']:
                    credential['authorization']['models']=[selection().model.public_dict()]
            elif fault=='wire_id':model='beta-wire-a'
            elif fault=='unqualified_checkpoint':port.checkpoint_factory=lambda *args:None
            if fault:
                with pytest.raises((NativeConfigurationError,ValueError)):
                    resolve_child_config(parent,model=model)
            else:
                cfg=resolve_child_config(parent,model=model)
                assert cfg['modelRef']==dict(creator_id='creator',model_id='beta')
                assert cfg['routing']['preferred_provider_id']=='provider-a'
                assert port.resolve_child_model_selection(parent,'beta')==NativeModelSelection(
                    ModelRef('creator','beta'),None,preferred_provider_id='provider-a')
                assert parent['config']['modelRef']==selection().model.public_dict()
            assert not port.repository.secret_calls
            assert not port.host._clients and port.host.image._image is None
            await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('supported', [False,True])
def test_descendant_model_switch_checks_the_original_root_checkpoint_owner(supported):
    import json
    from lib.agent_core.execution_budget import _ensure_execution_budget
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            root_port=binding(asyncio.get_running_loop(),executor)
            root_port.pricing_resolver=object()
            root_port.checkpoint_factory=lambda *args:pytest.fail('selection created a checkpoint')
            root_port._profile=json.dumps(dict(epoch='epoch',checkpoint_child_routes=supported))
            root=dict(id='epoch',status='running',_userId=7,
                _principalContext=principal().to_payload(),_nativePreparedInvocation=root_port,config={})
            account=_ensure_execution_budget(root)
            descendant_port=binding(asyncio.get_running_loop(),executor)
            descendant_port._transient=True
            descendant_port._profile=json.dumps(dict(epoch='descendant'))
            descendant_port.fee_transfer_target=root
            descendant=dict(id='descendant',status='running',_userId=7,
                _principalContext=principal().to_payload(),_nativePreparedInvocation=descendant_port,
                _parentTaskId='epoch',_executionBudget=account,config={})
            account.attach(descendant,parent_task_id='epoch')
            if supported:
                assert descendant_port.resolve_child_model_selection(descendant,'beta').model.model_id=='beta'
            else:
                with pytest.raises(NativeConfigurationError,match='explicit checkpoint support'):
                    descendant_port.resolve_child_model_selection(descendant,'beta')
            assert not descendant_port.repository.secret_calls
            assert not root_port.host._clients and not descendant_port.host._clients
            await descendant_port.host.close();await root_port.host.close()
    asyncio.run(run())

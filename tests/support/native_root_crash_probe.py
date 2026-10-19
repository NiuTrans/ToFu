"""Private real ROOT subprocess used by the SIGKILL recovery contract.

Entry: python -m tests.support.native_root_crash_probe <configuration.json>.
Uses the supplied embedded authority, archive and loopback provider. The parent
owns process termination; no user store or live application is opened.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import uuid


async def run(values):
    from lib.agent_core.abort_signal import AbortSignal
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.identity import PrincipalContext
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
    from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
    from lib.model_routing.repository import OwnerBoundary
    from lib.resource_control.runtime import launch_policy
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_host import NativeHost
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.tasks_pkg.orchestrator._run import execute_task_invocation, ROOT_INVOCATION_POLICY
    from lib.turn_lifecycle import create_turn_pair, create_attempt, bind_task, get_turn, get_attempt, recover_running_attempts
    from lib.conversation_sync.command_service import _resume_executor_config
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository, selection
    from tests.support.native_process_probe import config
    from tofu_db.embedded import EmbeddedRuntime

    root=Path(values['directory']);archive=Path(values['archive'])
    priced_tree=values.get('priced_tree',False)
    file_tools=values.get('file_tools',False)
    child_model=values.get('child_model','alpha')
    owner=2 if priced_tree else 1
    epoch=values.get('original_task_id',values['task_id']) if priced_tree else 'root-crash-epoch'
    principal=PrincipalContext.user(subject_id='root-crash-fixture',owner_user_id=owner)
    task_id,conv_id=values['task_id'],values['conversation_id']
    settings=dict(model='alpha',maxTokens=64,thinkingEnabled=False,memoryEnabled=False,
        projectEnabled=False,searchMode='off',tools={'programmaticCalling':'off'},
        executionLimits={'max_tokens':100000,'max_tool_calls':3,'deadline':values['deadline']})
    if file_tools:
        (root/'project').mkdir(exist_ok=True)
        settings.update(projectEnabled=True,projectPath=str(root/'project'),autoApply=True)
        from lib.project_mod import modifications
        from lib.project_mod.write_tools import _ops
        modifications.SESSIONS_DIR=str(root/'file-sessions')
        _ops._touch_for_vscode=lambda *args:None
    budget=Budget(1,16<<20,1<<20,1<<20,5)
    manifest=verify(archive)
    with ThreadPoolExecutor(max_workers=2 if priced_tree else 1) as executor:
        with EmbeddedRuntime(Path(values['library']),root/'authority',owner_id=owner,tenant_id=1,
                             secret=b'x'*32) as native:
            assert native.build_identity()['source_sha256']==values['source_sha256']
            storage=EmbeddedStorageV2Client(native,b'x'*32,owner_id=owner,tenant_id=1)
            service.storage_runtime=lambda:SimpleNamespace(client=lambda **options:storage)
            wallet=None
            if priced_tree:
                from lib.billing import users,deposit,reserve,get_balance
                from lib.billing.request_flow import settle_task
                import lib.relay_config
                lib.relay_config.billing_enabled=lambda:True
                if values['resume']:
                    wallet=users.get_user(values['wallet_id'])
                    if values.get('fresh_executor'):
                        reserve(wallet.id,100,ref_id=task_id,note='fresh executor crash hold')
                else:
                    wallet=users.create_user(uuid.uuid4().hex+'@paid-crash.example')
                    assert wallet.owner_user_id==owner
                    deposit(wallet.id,10000,ref_id='seed-'+task_id)
                    reserve(wallet.id,100,ref_id=task_id,note='owned crash hold')
                assert wallet is not None
            turn=None
            if values['resume']:
                assert recover_running_attempts()==1
                turn=get_turn(conv_id,values['turn_id'],user_id=owner)
                assert turn['status']=='interrupted'
                settings.update(_resume_executor_config('continue',turn['projection']))
                if values.get('public_http'):
                    from tests.support.native_file_http_resume import resume_file_root_http
                    await resume_file_root_http(values,storage,turn,principal,settings)
                    storage.close()
                    return
                if values['phase']=='inflight':
                    from lib.turn_lifecycle import LifecycleConflict
                    from lib.agent_core.native_process import CheckpointRequest
                    try:
                        create_attempt(conv_id,turn['turnId'],command_id=uuid.uuid4().hex,
                            operation='continue',expected_projection_revision=turn['projectionRevision'],
                            config=settings,user_id=owner)
                    except LifecycleConflict:
                        pass
                    else:
                        raise AssertionError('empty inflight Turn admitted Continue')
                    async def reject_restore(request,state):
                        raise AssertionError('inflight journal exposed an executable snapshot')
                    probe=NativeCheckpointPolicy(principal=principal,task={'id':task_id,'_userId':owner},
                        storage=storage,executor=executor,epoch=epoch,
                        binding='same-model-tools-account',token=uuid.uuid4().hex,header_bytes=4096,
                        snapshot_bytes=1<<20,identity_bytes=1024,io_timeout_ms=30000,
                        restore=reject_restore,recover_abandoned=True)
                    try:
                        await probe.load(CheckpointRequest(principal,task_id,'load',0,
                            epoch,'same-model-tools-account',threading.Event()))
                    except ValueError as error:
                        assert 'ineligible' in str(error)
                    else:
                        raise AssertionError('inflight journal admitted recovery')
                    finally:
                        await probe.close()
                    receipt={'status':'refused','attempt_status':turn['status'],'turn_status':turn['status'],
                        'paid_tokens':0,'tool_calls':0,'restored_frontiers':[], 'checklist_updates':0,
                        'fee_rounds':0,'error':'inflight journal ineligible','joined_cleanup':probe.closed,
                        'pending_model_requests':0,'public_continue_refused_before_prepare':True}
                    if priced_tree:
                        receipt.update(wallet_balance=get_balance(wallet.id),wallet_hold_retained=True)
                    Path(values['result']).write_text(json.dumps(receipt)+'\n')
                    storage.close()
                    return
                pair=create_attempt(conv_id,turn['turnId'],command_id=uuid.uuid4().hex,
                    operation='continue',expected_projection_revision=turn['projectionRevision'],
                    config=settings,user_id=owner)
            else:
                pair=create_turn_pair(conv_id,command_id=uuid.uuid4().hex,
                    input_projection={'content':'question'},config=settings,user_id=owner,
                    conversation_defaults={'allowCreate':True,'title':'ROOT crash fixture','settings':{}})
            attempt_id=pair['attempt']['attemptId'];turn_id=pair['turn']['turnId']
            bind_task(attempt_id,task_id,user_id=owner)
            task=dict(id=task_id,convId=conv_id,_turnId=turn_id,_attemptId=attempt_id,
                _userId=owner,_principalContext=principal.to_payload(),status='running',aborted=False,
                content='',thinking='',toolRounds=[],segments=[] if turn is None else turn['projection']['segments'],
                config=settings,created_at=time.time(),abort_event=AbortSignal(),content_lock=threading.Lock(),
                events=[],events_lock=threading.Lock(),_attended=True,_dispatch_heartbeat=0.,_t_last_event=0.,
                messages=[{'role':'user','content':'question'}])
            if values.get('delivery_review'):
                from tests.support.native_delivery_review import prepare
                review_preparation = prepare(task, values)
            if priced_tree:
                task.update(_billingUserId=wallet.id,_billing_reservation_micro=100)
            account=ExecutionBudget(task);task['_executionBudget']=account
            repo=Repository();repo.boundary=OwnerBoundary.create(owner)
            repo.document['connections'][1]['enabled']=False
            repo.document['connections'][0]['base_url']=values['endpoint']
            group=mint_routed_slot_group(repo,repo.boundary,selection(),owner_tag='task:'+task_id)
            task['_model_routing_group'],task['_pinned_provider_id']=group,group.pin_id
            if priced_tree:
                selected=selection()
                task['_requestedModelRef']=selected.model.public_dict()
                task['_model_routing_selection']=selected
                settings['modelRef']=selected.model.public_dict()
                settings['routing']={'preferred_provider_id':selected.preferred_provider_id}
            restored=[]
            async def restore(request,state):
                restored.append(state)
            def hold(phase,request):
                receipt={'phase':phase,'attempt_id':attempt_id,'turn_id':turn_id,
                    'paid_tokens':account.consumption_snapshot()[0],
                    'tool_calls':account.consumption_snapshot()[1],
                    'checkpoint':request.state if phase=='completed' else None}
                if file_tools:
                    receipt.update(file_operations=[row['executionEffect']['operationId'] for row in task['toolRounds']],
                        fee_credits=[row['nativeCredits']['total'] for row in task.get('apiRounds',[])])
                if values.get('delivery_review'):
                    from tests.support.native_delivery_review import before_crash
                    receipt['delivery_review'] = before_crash(task, values)
                stat=Path('/proc/self/stat').read_text()
                writer={'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        'pid_namespace':Path('/proc/self/ns/pid').stat().st_ino,
                        'pid':int(stat.split()[0]),'start_ticks':int(stat.rsplit(')',1)[1].split()[19])}
                receipt['writer_process_json_bytes']=len(json.dumps(writer,sort_keys=True,separators=(',',':')).encode())
                if phase=='completed':
                    receipt['checkpoint_json_bytes']=len(json.dumps(request.state,sort_keys=True,separators=(',',':')).encode())
                    rows=request.state.get('host_fee_projection',{}).get('child_rounds',[])
                    receipt['child_route_metadata_json_bytes']=sum(len(json.dumps(row,sort_keys=True,separators=(',',':')).encode())-len(json.dumps({key:value for key,value in row.items() if key!='route'},sort_keys=True,separators=(',',':')).encode()) for row in rows if 'route' in row)
                if priced_tree:
                    receipt.update(wallet_id=wallet.id,wallet_balance=get_balance(wallet.id))
                    if request.state is not None:
                        capsule = request.state.get('host_fee_projection', {}).get('wallet_settlement')
                        receipt['wallet_checkpoint_invoice'] = capsule
                        receipt['wallet_checkpoint_invoice_bytes'] = len(json.dumps(capsule,separators=(',',':')).encode())
                    receipt['terminal_invoice_frozen'] = '_nativeBillingSettlement' in task
                destination=Path(values['ready'])
                temporary=destination.with_suffix('.tmp')
                temporary.write_text(json.dumps(receipt)+'\n')
                temporary.replace(destination)
                assert threading.Event().wait(60),'parent did not terminate owned crash worker'
            class CrashCheckpoint(NativeCheckpointPolicy):
                async def load(self,request):
                    result=await super().load(request)
                    if values.get('crash_after_handoff'):
                        hold('handoff_loaded',request)
                    return result
                async def begin(self,request):
                    result=await super().begin(request)
                    if not values['resume'] and values['phase']=='inflight':hold('inflight',request)
                    return result
                async def complete(self,request):
                    if not values['resume'] and values['phase']=='file_pending':hold('file_pending',request)
                    result=await super().complete(request)
                    if not values['resume'] and values['phase']=='completed':hold('completed',request)
                    return result
            from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
            recovery=NativeFileToolRecovery(principal=principal,task=task,executor=executor) if file_tools else None
            checkpoint=CrashCheckpoint(principal=principal,task=task,storage=storage,executor=executor,
                epoch=epoch,binding='same-model-tools-account',token=uuid.uuid4().hex,
                header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=1024,io_timeout_ms=30000,
                restore=restore,recover_abandoned=values['resume'] and not values.get('fresh_executor'),
                handoff_from_accepted=values['resume'] and values.get('fresh_executor',False),tool_recovery=recovery)
            profile=config('ignored',task_id)
            for key in ({'version','type','owner_user_id','task_id','messages','tools','rounds','deadline_ms',
                         'tool_calls','event_bytes','output_bytes','request_bytes','retained_bytes',
                         'feedback_bytes','tool_frame_bytes'} | ROUTE_OVERRIDE_FIELDS):
                profile.pop(key,None)
            profile.update(epoch=epoch,checkpoint_shared_account=True,
                checkpoint_frame_bytes=1<<20,model_control={'ack_ms':5000,'pricing':True},
                model_counter={'input_bytes':1<<20,'text_bytes':1<<20,'tool_json_bytes':1<<20,
                               'depth':128,'worker_stack_bytes':1<<20},
                approval_control={'receipts':1<<20,'identity_bytes':1<<20},
                terminal_persistence={'operation_id':uuid.uuid4().hex,'attempts':2,'ack_ms':5000,'frame_bytes':1<<20})
            if priced_tree:profile['checkpoint_tree_usage']=True
            if child_model!='alpha':profile['checkpoint_child_routes']=True
            child_results=[]
            if priced_tree and not values['resume']:
                from lib.tasks_pkg.native_gateway import NativeGateway
                original_execute=NativeGateway._execute
                def delegate(gateway,request,approved):
                    if request.name=='todo_write' and not gateway.task.get('_parentTaskId') and not child_results:
                        from lib.tasks_pkg.child_invocation import create_child_invocation,run_child_invocation
                        child=create_child_invocation(task,model=child_model,tools=[],objective='priced child',
                            event_sink=lambda _event:None,abort_check=lambda:task['abort_event'].is_set())
                        child_results.append(child)
                        def execute_children():
                            grandchild=create_child_invocation(child,model=child_model,tools=[],objective='priced grandchild',
                                event_sink=lambda _event:None,abort_check=lambda:task['abort_event'].is_set())
                            child_results.append(grandchild)
                            run_child_invocation(grandchild);run_child_invocation(child)
                            assert grandchild['status']==child['status']=='done'
                        with ThreadPoolExecutor(max_workers=1) as child_worker:
                            child_worker.submit(execute_children).result()
                    return original_execute(gateway,request,approved)
                NativeGateway._execute=delegate
            image=NativeArchiveImage(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                application_version=manifest['build']['application_version'],budget=budget)
            host=NativeHost(image)
            task['_nativePreparedInvocation']=NativePreparedInvocation(host=host,loop=asyncio.get_running_loop(),
                principal=principal,repository=repo,selection=selection(),configuration=profile,budget=budget,
                executor=executor,working_directory=root,pending_calls=8,pending_bytes=1<<20,
                resource_policy=launch_policy({}),checkpoint_policy=checkpoint,
                pricing_resolver=NativeRoutePricing(principal=principal,margin=0,
                    lookup=lambda owner,model,provider,**k:({'input':1,'output':2} if model=='beta' else {'input':2.5,'output':10}),rates=lambda p:7.24))
            error=None
            try:
                await asyncio.to_thread(execute_task_invocation,task,policy=ROOT_INVOCATION_POLICY)
            except Exception as failure:
                error=type(failure).__name__
                assert values['resume'] and values['phase']=='inflight',str(failure)
            finally:
                dispose_routed_slot_group(group)
                await host.close()
            final=get_turn(conv_id,turn_id,user_id=owner)
            receipt={'status':task['status'],'attempt_status':get_attempt(attempt_id,user_id=owner)['status'],
                'turn_status':final['status'],'paid_tokens':account.consumption_snapshot()[0],
                'tool_calls':account.consumption_snapshot()[1],'restored_frontiers':[v['next_round'] for v in restored],
                'checklist_updates':(final['projection'].get('todoState') or {}).get('update_count',0),
                'fee_rounds':len(task.get('apiRounds') or []),'error':error,
                'joined_cleanup':not host._clients and checkpoint.closed and image._closed,
                'pending_model_requests':account.continuity_snapshot()['pending_model_requests']}
            if file_tools:
                receipt.update(fee_credits=[row['nativeCredits']['total'] for row in task.get('apiRounds',[])],
                    task_error=task.get('error'),reconciliation_started=recovery.reconciliation_started,
                    restored_pending=[v.get('tools_pending',False) for v in restored],
                    producer_tasks=[v['tool_recovery']['producer_task_id'] for v in restored],
                    fee_executor_ids=[row['taskId'] for row in task.get('apiRounds',[])],
                    task_id=task_id,
                    recovery_state_bytes=[len(json.dumps(v,separators=(',',':')).encode()) for v in restored],
                    operation_metadata_bytes=[len(json.dumps(v['tool_recovery'],separators=(',',':')).encode()) for v in restored],
                    restored_operations=[[row['inspection']['operationId'] for row in v['tool_recovery']['calls']] for v in restored])
            if priced_tree:
                from lib.billing import settle as actual_settle
                import lib.billing
                charge=task['_nativeBillingSettlement']['componentsMicro']['totalMicro']
                def lost_wallet_ack(*args,**kwargs):
                    actual_settle(*args,**kwargs)
                    raise OSError('committed wallet acknowledgement lost')
                lib.billing.settle=lost_wallet_ack
                try:
                    settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                except OSError:pass
                else:raise AssertionError('lost wallet ACK did not escape')
                lib.billing.settle=actual_settle
                settled=settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                assert settled['cost_micro']==charge
                retired_invoices = []
                source_charge = 0
                if values.get('fresh_executor'):
                    from lib.billing.native_settlement import _retired_invoice, recover
                    from lib.billing import wallet_janitor
                    from unittest.mock import patch
                    for retired_id in values['retired_task_ids']:
                        invoice = _retired_invoice(storage, owner=owner, task_id=retired_id)
                        assert invoice is not None
                        retired_invoices.append(invoice)
                        source_charge += invoice['componentsMicro']['totalMicro']
                    import statistics
                    samples = []
                    for _ in range(21):
                        started = time.perf_counter_ns()
                        assert _retired_invoice(storage, owner=owner, task_id=retired_invoices[0]['task_id']) == retired_invoices[0]
                        samples.append((time.perf_counter_ns() - started) / 1000000)
                    receipt['retired_invoice_query_median_ms'] = statistics.median(samples[1:])
                    receipt['retired_invoice_capsule_bytes'] = [len(json.dumps(invoice,separators=(',',':')).encode()) for invoice in retired_invoices]
                    lost = [False]
                    def lose_first_source_ack(*args, **kwargs):
                        result = actual_settle(*args, **kwargs)
                        if not lost[0]:
                            lost[0] = True
                            raise OSError('source invoice committed ACK lost')
                        return result
                    future = time.time() + 3600
                    with patch('lib.billing.wallet_janitor.time', SimpleNamespace(time=lambda: future)):
                        with patch('lib.billing.settle', lose_first_source_ack):
                            sweep = wallet_janitor.sweep_stale_reserves(ttl_seconds=60)
                        assert sweep['errors'] == 1 and sweep['reclaimed'] == 0, sweep
                        assert sweep['settled_native'] == len(retired_invoices) - 1, sweep
                        assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['candidates'] == 0
                    before_retry = get_balance(wallet.id)
                    for invoice in retired_invoices:
                        assert recover(wallet_user_id=wallet.id, task_id=invoice['task_id'], held_micro=100)
                    assert get_balance(wallet.id) == before_retry
                    receipt.update(retired_wallet_invoices=retired_invoices, source_wallet_sweep=sweep,
                        source_wallet_ack_retry_idempotent=True, wallet_current_charge=charge,
                        wallet_source_charge=source_charge)
                receipt['restored_wallet_checkpoint_invoices'] = [
                    state.get('host_fee_projection', {}).get('wallet_settlement') for state in restored]
                receipt.update(wallet_charge=charge+source_charge,wallet_balance=get_balance(wallet.id),
                    wallet_ack_retry_idempotent=True,fee_executor_ids=[row['taskId'] for row in task['apiRounds']],
                    root_tokens=(account.consumption_snapshot()[0] if file_tools else task['usage']['total_tokens']),fee_models=[row['model'] for row in task['apiRounds']],
                    original_child_quotes=[row['nativeCredits']['total'] for row in task['apiRounds'] if row.get('billableTaskId')])
            if values.get('delivery_review'):
                from tests.support.native_delivery_review import after_continue
                receipt['delivery_review'] = after_continue(task, values)
                if values.get('revise_accepted_output'):
                    receipt['delivery_review']['after_requirement_change'] = review_preparation
            Path(values['result']).write_text(json.dumps(receipt)+'\n')
            storage.close()


if __name__=='__main__':
    asyncio.run(run(json.loads(Path(sys.argv[1]).read_text())))

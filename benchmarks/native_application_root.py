"""Priced/counted ordinary HTTP ROOT comparability over explicit disposable authority.

CLI starts independent Python/native workers. Work crosses canonical HTTP task
birth, route admission, serving workers, model protocol and Task/Turn commit.
Only fixture repository/pricing inputs are supplied; no execution result is mocked.
Single observations diagnose equivalent work and never admit timing/defaults.
"""
import argparse
import asyncio
from contextlib import ExitStack, AsyncExitStack, nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def comparable_work(observation):
    """Exclude telemetry/provenance tags, retaining exact request and finances."""
    from lib.cost import normalize_usage
    evidence=observation['evidence']
    rounds=[]
    for row in evidence['api_rounds']:
        cost=row['cost'];quote=cost['pricingSnapshot']
        rounds.append(dict(round=row['round'],model=row['model'],usage=normalize_usage(row['usage']),
            cost={key:value for key,value in cost.items() if key not in {'pricingSource','pricingSnapshot'}},
            quote={key:quote[key] for key in ('currency','model','provider','rates','selectedPromptTokens')}))
    return dict(request=evidence['request'],content=evidence['content'],usage=normalize_usage(evidence['usage']),
        api_rounds=rounds,budget_consumption=evidence['budget_consumption'],
        **{key:evidence[key] for key in ('task_status','attempt_status','turn_status','replay_status','route_retired','worker_retired')})


from benchmarks.native_application_authority import APPLICATION_BACKENDS, COMPLETION_OBSERVERS, check_usage_summary, check_terminal_reference, check_token_library


def verify_durable_observation(row):
    evidence=row['evidence']
    expected={'task_status':'done','attempt_status':'completed','turn_status':'completed','replay_status':'done'}
    if (any(evidence.get(key)!=value for key,value in expected.items())
            or evidence.get('route_retired') is not True or evidence.get('worker_retired') is not True
            or not evidence.get('api_rounds') or len(evidence['budget_consumption'])!=3
            or evidence['budget_consumption'][2] is not True):
        raise ValueError('HTTP ROOT observations require successful durable/financial completion')
    source='native_resolved_price' if row['arm']=='native' else 'resolved_price'
    if any(entry['cost'].get('pricingSource')!=source for entry in evidence['api_rounds']):
        raise ValueError('HTTP ROOT observations require resolved prices, never estimates')


def compare(observations):
    if ([row.get('arm') for row in observations]!=['python','native']
            or any(row.get('joined_cleanup') is not True for row in observations)):
        raise ValueError('HTTP ROOT comparison requires two independent joined arms')
    token_count=observations[0].get('native_token_count',False)
    for row in observations:check_token_library(row,token_count)
    if observations[0].get('token_library_sha256') != observations[1].get('token_library_sha256'):
        raise ValueError('HTTP ROOT token library pin differs')
    selected=observations[0].get('native_usage_summary',False)
    for row in observations:check_usage_summary(row,selected)
    reference=observations[0].get('native_terminal_reference',False)
    for row in observations:check_terminal_reference(row,reference)
    observers=[row.get('completion_observer','poll') for row in observations]
    if any(type(value) is not str or value not in COMPLETION_OBSERVERS for value in observers) or len(set(observers))!=1:
        raise ValueError('HTTP ROOT completion observer differs or is invalid')
    for row in observations:verify_durable_observation(row)
    if len({row['archive_sha256'] for row in observations})!=1:
        raise ValueError('HTTP ROOT observations use different pinned archives')
    identities=[(row.get('image_source','archive'), row.get('executable_sha256')) for row in observations]
    if identities[0] != identities[1]:
        raise ValueError('HTTP ROOT observations use different executable admission sources')
    authorities=[row.get('storage_authority') for row in observations]
    if any(authorities):
        if any(not isinstance(authority,dict) for authority in authorities) or authorities[0]!=authorities[1]:
            raise ValueError('HTTP ROOT observations require one identical storage authority')
        if authorities[0]['backend']=='tofudb':
            pin=authorities[0].get('engine_sha256')
            if not isinstance(pin,str) or len(pin)!=64 or any(c not in '0123456789abcdef' for c in pin):
                raise ValueError('HTTP ROOT native authority requires an exact engine pin')
    work=[comparable_work(row) for row in observations]
    signatures=[digest(row) for row in work]
    return dict(equivalent_work=signatures[0]==signatures[1],work_sha256=signatures,
        measurement_permitted=signatures[0]==signatures[1],performance_admitted=False,
        default_selection_admitted=False,
        comparison_scope='Exact decoded request/content/statuses; normalized canonical usage, round amounts/quote and shared consumption',
        excluded_fields='Provider telemetry and pricing provenance tags remain in raw observations',
        limits=['Quart in-process test-client ingress with TESTING auth, explicit fixture repository/pricing and loopback provider',
            'Explicit disposable SQLite or verified Rust daemon/embedded; text only, no paid wallet debit, external inference or concurrency/startup admission',
            'One first-operation clock per arm is diagnostic, not a timing cohort'])



def compare_native_reference_pair(baseline,candidate):
    """Same native warm ROOT; reference off/on with exact durable/paid work."""
    import statistics
    from benchmarks.native_agent_root_measure import quantiles
    from benchmarks.native_application_authority import check_completion_observer,check_usage_summary,check_worker_reuse,check_image_preparation
    for row,selected in ((baseline,False),(candidate,True)):
        if row.get('arm')!='native' or row.get('mode')!='warm' or row.get('joined_cleanup') is not True:
            raise ValueError('native reference pair requires joined native warm workers')
        verify_durable_observation(row)
        check_terminal_reference(row,selected)
        check_completion_observer(row,baseline.get('completion_observer','poll'))
        check_usage_summary(row,baseline.get('native_usage_summary',False))
        check_worker_reuse(row,baseline.get('worker_reuse',False))
        check_token_library(row,baseline.get('native_token_count',False))
        check_image_preparation(row,baseline.get('native_image_preparation',False))
        if (row.get('profiler_enabled') or row.get('tracemalloc_enabled')
                or row.get('diagnostic_stages_enabled') or row.get('diagnostic_stages')
                or row.get('diagnostic_storage_metrics')):
            raise ValueError('instrumented native reference pair cannot grant timing')
        if not row.get('raw') or any(type(clock.get(key)) is not int or clock[key]<=0
                for clock in row['raw'] for key in ('wall_ns','cpu_ns')):
            raise ValueError('invalid native reference clocks')
        if row.get('work_sha256')!=digest(comparable_work(row)):
            raise ValueError('native reference work signature differs')
    if (not baseline.get('process_identity') or not candidate.get('process_identity')
            or baseline['process_identity']==candidate['process_identity']
            or any(baseline.get(key) is None or baseline.get(key)!=candidate.get(key)
                for key in ('backend','scenario','archive_sha256','storage_authority','clock_scope','warmups','work_sha256'))
            or baseline.get('image_source','archive')!=candidate.get('image_source','archive')
            or baseline.get('executable_sha256')!=candidate.get('executable_sha256')
            or baseline.get('token_library_sha256')!=candidate.get('token_library_sha256')
            or baseline.get('affinity')!=candidate.get('affinity')
            or len(baseline['raw'])!=len(candidate['raw'])):
        raise ValueError('native reference identity, authority, clock or work differs')
    authority=baseline['storage_authority']
    if authority.get('backend')=='tofudb':
        pin=authority.get('engine_sha256')
        if not isinstance(pin,str) or len(pin)!=64 or any(c not in '0123456789abcdef' for c in pin):
            raise ValueError('native reference authority requires an exact engine pin')
    before=quantiles([r['wall_ns'] for r in baseline['raw']]);after=quantiles([r['wall_ns'] for r in candidate['raw']])
    ratios={key:after[key]/before[key] for key in ('p50','p95','p99')}
    ratios['cpu_mean']=statistics.mean(r['cpu_ns'] for r in candidate['raw'])/statistics.mean(r['cpu_ns'] for r in baseline['raw'])
    return dict(scope='same-native durable warm ROOT final-reference off/on',ratios=ratios,
        measurement_permitted=True,performance_admitted=False,default_selection_admitted=False)

def worker(args):
    for key in list(os.environ):
        if key.startswith(('TOFU_STORAGE_','TOFU_SUPERVISOR_','TOFUDB_','TOFU_DB_','_TOFU_')) or key in {
                'PORT','TOFU_DATA_DIR','TOFU_PROJECT_ROOT','TOFU_PROJECT_PATH',
                'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS','TOFU_RESOURCE_BUDGET_POLICY_VERSION'}:
            os.environ.pop(key,None)
    with tempfile.TemporaryDirectory(prefix='tofu-http-root-') as temporary:
        os.environ.update(TOFU_DATA_DIR=temporary,TOFU_CODEX_SHARED_LOGIN='0',
            TOFU_PERSONAL_STORAGE_BACKEND='sqlite' if args.backend=='sqlite' else 'tofudb',
            TOFUDB_TRANSPORT='embedded' if args.backend=='embedded' else 'daemon',TOFUDB_OWNER_ID='1',TOFUDB_TENANT_ID='1')
        from lib.storage import StorageRuntime,StorageSupervisor,service
        from benchmarks.native_application_authority import authority_witness,observe_runtime_authority
        witness=authority_witness(args.backend,args.storage_engine,ROOT)
        if args.backend!='sqlite':
            os.environ['TOFUDB_BIN']=str(args.storage_engine.resolve())
            os.environ['TOFUDB_EXPECTED_SHA256']=witness['engine_sha256']
        runtime=StorageRuntime(StorageSupervisor(project_root=Path(temporary),
            backend=os.environ['TOFU_PERSONAL_STORAGE_BACKEND'],startup_timeout=60),auto_restart=False)
        service.install_runtime_for_test(runtime)
        if args.diagnostic_stages and args.backend=='sqlite':
            from benchmarks.native_sidecar_stages import diagnostic_sidecar_launch
            launch_scope=diagnostic_sidecar_launch()
        else:
            launch_scope=nullcontext()
        with launch_scope:runtime.start()
        expected_backend='sqlite' if args.backend=='sqlite' else 'tofudb'
        try:
            authority=runtime.client().health()
            assert authority['backend']==expected_backend,'fixture selected a different storage authority'
            observed=observe_runtime_authority(runtime.status(),witness)
            result=asyncio.run(trial(args,runtime.client(),Path(temporary)))
            assert observe_runtime_authority(runtime.status(),witness)==observed
            result['storage_runtime']=observed
            from lib.log import _drain_audit_queue
            from lib import log as audit
            _drain_audit_queue(timeout=5)
            assert audit._audit_queue is None or audit._audit_queue.unfinished_tasks==0
        finally:service.stop_storage()
        stopped=runtime.status()
        assert stopped['state']=='stopped' and stopped['ready'] is False and stopped['pid'] is None
        result['storage_runtime_closed']=True
        import psutil
        assert not psutil.Process().children(recursive=True),'worker leaked children'
        result['joined_cleanup']=True
        result['storage_authority']=witness
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')


async def trial(args,storage,directory):
    from concurrent.futures import ThreadPoolExecutor
    from benchmarks.native_agent_root import RootWorkload,cpu_ns
    from lib.agent_core.native_image import NativeArchiveImage, NativeExecutableImage
    from lib.agent_core.native_process import Budget
    from lib.app.app_assembly import create_application
    from lib.model_routing.repository import OwnerBoundary
    from lib.resource_control.runtime import launch_policy
    from lib.server.server_loop_runtime import ServingLoopRuntime
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.turn_lifecycle import get_turn,get_attempt
    from lib.conversation_sync import task_start
    from scripts.build_native_runtime import verify, SPEC
    from tests.model_routing.test_native_route import Repository
    from tests.tasks_pkg.test_native_application import profile
    import psutil
    process=psutil.Process()
    def cpu_clock():
        total=cpu_ns()
        for child in process.children(recursive=True):
            clocks=child.cpu_times()
            total+=int((clocks.user+clocks.system+clocks.children_user+clocks.children_system)*1e9)
        return total
    source=args.archive.resolve();manifest=verify(source)
    budget=Budget(1,16<<20,1<<20,1<<20,5)
    image=NativeArchiveImage(archive=source,sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],budget=budget)
    installed = getattr(args, 'installed_executable', None)
    if installed is not None:
        image = NativeExecutableImage(binary=installed.resolve(),
            sha256=manifest['files']['bin/tofu-native']['sha256'],
            application_version=manifest['build']['application_version'], budget=budget)
    token_library = getattr(args, 'installed_token_library', None)
    token_pin = None
    if token_library is not None:
        if installed is None or SPEC['optional_token_count']['contract_member'] not in manifest['files']:
            raise ValueError('installed token library requires installed executable and declared archive extension')
        token_pin = manifest['files'][SPEC['optional_token_count']['member']]['sha256']
        image = NativeExecutableImage(binary=image.binary,sha256=image.sha256,
            application_version=image.application_version,budget=budget,
            token_library=token_library.resolve(),token_library_sha256=token_pin)
    rates={'input':5.,'output':10.,'currency':'USD','unit':'per_million_tokens'}
    created=[];serving=None
    with ThreadPoolExecutor(max_workers=1) as provider_executor,ExitStack() as overrides:
        provider=RootWorkload(storage=storage,host=None,executor=provider_executor,
            directory=directory,budget=budget,resource_policy=launch_policy({}),scenario='text')
        overrides.callback(provider.close)
        control=None
        if args.memory_control_fd is not None:
            if args.mode!='memory':raise ValueError('memory control requires an isolated memory worker')
            control=socket.socket(fileno=args.memory_control_fd);control.settimeout(60)
            overrides.callback(control.close)
        repo=Repository();repo.boundary=OwnerBoundary.create(1)
        repo.document['connections'][1]['enabled']=False
        repo.document['connections'][0]['base_url']=f'http://127.0.0.1:{provider.server.server_port}/v1'
        overrides.enter_context(patch('lib.model_routing.ModelRoutingRepository',lambda:repo))
        overrides.enter_context(patch('lib.pricing.lookup_pricing',return_value=rates))
        overrides.enter_context(patch('lib.billing.pricing.get_default_margin',return_value=0.))
        stages=None
        if args.diagnostic_stages:
            from benchmarks.native_application_stages import Stages
            stages=Stages().install(overrides,provider)
        original_create=task_start.create_task
        def create(*a,**kw):
            task=original_create(*a,**kw);created.append((task,task['_model_routing_group']));return task
        overrides.enter_context(patch.object(task_start,'create_task',create))
        reference_selected=getattr(args,'native_terminal_reference',False)
        reference_frames=0
        from lib.agent_core.native_process import _TerminalControl
        original_reference=_TerminalControl.resolve_reference
        def observe_reference(control,frame):
            nonlocal reference_frames
            result=original_reference(control,frame)
            reference_frames+=1
            return result
        overrides.enter_context(patch.object(_TerminalControl,'resolve_reference',observe_reference))
        summary_selected=getattr(args,'native_usage_summary',False)
        summary_witness={'requests':0,'paid_frozen_requests':0}
        from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
        original_terminal_persist=NativeTaskTerminalPolicy.persist
        async def observe_terminal(policy,request):
            if request.usage_summary_json is not None:
                summary_witness['requests']+=1
                if policy.task.get('_nativeFrozenCostProjection') is True:
                    summary_witness['paid_frozen_requests']+=1
            return await original_terminal_persist(policy,request)
        overrides.enter_context(patch.object(NativeTaskTerminalPolicy,'persist',observe_terminal))
        configuration=profile()
        if summary_selected:configuration['terminal_persistence']['usage_summary']=True
        if reference_selected:configuration['terminal_persistence']['completion_reference']=True
        for key in ('input_bytes','text_bytes','tool_json_bytes'):configuration['model_counter'][key]=1<<20
        options=NativeRootConfiguration(configuration=configuration,working_directory=directory,
            worker_reuse=getattr(args,'reuse_native_workers',False),image_preparation=getattr(args,'prepare_native_image',False),
            repository_factory=lambda principal:repo,
            pricing_factory=lambda principal,task:NativeRoutePricing(principal=principal,margin=0.,
                lookup=lambda *a,**kw:rates,rates=lambda principal:7.24))
        async def start():
            nonlocal serving
            serving=ServingLoopRuntime(asyncio.get_running_loop(),threading.Event(),environ={
                'TOFU_TASK_CLEANUP_INTERVAL':'0','TOFU_EXECUTOR_IDLE_SECONDS':'0',
                'TOFU_SYNC_WORKERS':'2','TOFU_AGENT_WORKERS':'2'}).start()
            app.extensions['tofu_serving_loop_lifecycle']={'runtime':serving}
        async def stop():
            if serving is not None:await serving.stop()
        app=create_application(__name__,static_dir=str(directory),logger=logging.getLogger(__name__),
            secret_key='fixture',config={'TESTING':True},native_image=image,
            native_root=options if args.arm=='native' else None,
            native_token_count=token_library is not None and args.arm=='native',
            startup_handlers=[('fixture.serving',start)],shutdown_handlers=[('fixture.serving',stop)])
        try:
            async with app.test_app(), AsyncExitStack() as observation_scope:
                completion=None
                if getattr(args,'completion_observer','poll')=='worker-future':
                    from benchmarks.native_completion import observe_workers
                    completion=await observation_scope.enter_async_context(observe_workers(serving.agent_executor))
                async def operation():
                    created.clear();provider.bodies.clear()
                    conv_id=uuid.uuid4().hex
                    storage.command('conversation.create',dict(conv_id=conv_id,user_id=1,title='Measured ROOT',
                        created_at=1,updated_at=1,settings={}),uuid.uuid4().hex)
                    body=dict(commandId=uuid.uuid4().hex,inputTurn={'content':'question'},config={
                        'modelRef':{'creator_id':'creator','model_id':'alpha'},'maxTokens':64,
                        'executionLimits':{'max_tokens':100000,'max_tool_calls':1000,'deadline':None},
                        'thinkingEnabled':False,'memoryEnabled':False,'projectEnabled':False,
                        'searchMode':'off','tools':{'programmaticCalling':'off'}})
                    cpu_start,wall_start=cpu_clock(),time.perf_counter_ns()
                    response=await app.test_client().post(f'/api/v4/conversations/{conv_id}/turns',json=body)
                    payload=await response.get_json()
                    assert response.status_code==200 and payload['ok'] is True,payload
                    attempt_id,turn_id=payload['attempt']['attemptId'],payload['turn']['turnId']
                    deadline=time.monotonic()+30
                    if completion is not None:
                        assert len(created)==1,'ROOT birth did not publish one task'
                        task_id=created[0][0]['id']
                        await completion.wait(task_id,timeout=max(.001,deadline-time.monotonic()))
                        assert not app.extensions['tofu_native_host']._clients,'native client did not join'
                        assert all(group.disposed for _,group in created),'ROOT route did not retire'
                        assert serving.agent_executor.scheduling_snapshot(task_id)['taskState']=='unknown','ROOT worker did not retire'
                        clocks=dict(wall_ns=time.perf_counter_ns()-wall_start,cpu_ns=cpu_clock()-cpu_start)
                        # Completion is a joined worker boundary, not a durable
                        # state substitute. Verify the same authority afterward.
                        attempt=await asyncio.to_thread(get_attempt,attempt_id,user_id=1)
                    else:
                        while True:
                            with stages.span('fixture.attempt_poll',cpu=False) if stages is not None else nullcontext():
                                attempt=await asyncio.to_thread(get_attempt,attempt_id,user_id=1)
                            if attempt['status'] in {'completed','failed','interrupted'}:break
                            assert time.monotonic()<deadline,'HTTP ROOT did not finish'
                            await asyncio.sleep(.01)
                        while (app.extensions['tofu_native_host']._clients
                                or any(not group.disposed for _,group in created)
                                or serving.agent_executor.scheduling_snapshot(attempt['taskId'])['taskState']!='unknown'):
                            assert time.monotonic()<deadline,'ROOT resources did not join'
                            await asyncio.sleep(.01)
                        clocks=dict(wall_ns=time.perf_counter_ns()-wall_start,cpu_ns=cpu_clock()-cpu_start)
                    if stages is not None:stages.record_operation_clock(wall_start,clocks['wall_ns'])
                    turn=await asyncio.to_thread(get_turn,conv_id,turn_id,user_id=1)
                    replay=await asyncio.to_thread(storage.query,'task_results.replay_get',{'key':attempt['taskId'],'user_id':1})
                    assert attempt['status']==turn['status']=='completed',turn
                    assert replay['status']=='done' and turn['projection']['content']=='durable fixture answer'
                    assert len(provider.bodies)==len(created)==1
                    task,group=created[0]
                    rounds=turn['projection']['apiRounds'];assert len(rounds)==1
                    assert task.get('_nativePreparedInvocation') is None
                    budget_state=task['_executionBudget'].consumption_snapshot()
                    evidence=dict(request=provider.bodies[0],content=turn['projection']['content'],
                        usage=turn['projection']['usage'],api_rounds=rounds,budget_consumption=budget_state,
                        task_status=task['status'],attempt_status=attempt['status'],turn_status=turn['status'],
                        replay_status=replay['status'],route_retired=group.disposed,worker_retired=True)
                    from lib.tasks_pkg.event_delivery import retained_snapshot_bytes
                    retained=retained_snapshot_bytes(*(task.get(key) for key in
                        ('messages','content','thinking','events','segments','toolRounds','apiRounds','cost')))
                    return dict(arm=args.arm,evidence=evidence,clocks=clocks,retained_task_bytes=retained,
                        clock_scope='HTTP request through durable terminal and joined worker/route/native scope; startup/readback excluded',
                        archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        image_source='installed_executable' if installed is not None else 'archive',
                        executable_sha256=manifest['files']['bin/tofu-native']['sha256'],
                        measurement_permitted=False,performance_admitted=False,default_selection_admitted=False)
                if control is not None:
                    control.sendall(b'R')
                    if control.recv(1)!=b'G':raise ValueError('memory sampling readiness was not acknowledged')
                from lib.token_counter import tiktoken_counter
                token_witness=dict(before_first_operation=app.extensions['tofu_native_host']._token_counter is not None,
                    python_encoders_before=sorted(tiktoken_counter._encoders))
                image_preparation_witness=dict(before_first_operation=image.admission_started)
                result=await operation()
                image_preparation_witness['after_first_operation']=image.admission_started
                token_counter=app.extensions['tofu_native_host']._token_counter
                token_witness.update(after_first_operation=token_counter is not None,
                    encodings=sorted(token_counter._handles) if token_counter is not None else [],
                    python_encoders_after=sorted(tiktoken_counter._encoders))
                signatures={digest(comparable_work(result))};raw=[]
                if args.mode!='cold':
                    for _ in range(args.warmups):
                        row=await operation();signatures.add(digest(comparable_work(row)))
                    for _ in range(args.samples):
                        row=await operation();signatures.add(digest(comparable_work(row)));raw.append(row['clocks'])
                assert len(signatures)==1,'HTTP fixture work changed within worker'
                descriptor_bytes=0
                if image._image is not None:descriptor_bytes=os.fstat((await image._image).fileno()).st_size
                result.update(native_token_count=token_library is not None,token_library_sha256=token_pin,token_library_witness=token_witness,
                    native_terminal_reference=reference_selected,terminal_reference_frames=reference_frames,image_preparation_witness=image_preparation_witness,native_image_preparation=getattr(args,'prepare_native_image',False),image_admission_started=image.admission_started,native_usage_summary=summary_selected,usage_summary_witness=summary_witness.copy(),completion_observer=getattr(args,'completion_observer','poll'),worker_reuse=getattr(args,'reuse_native_workers',False),image_preparation=getattr(args,'prepare_native_image',False),first_operation=result['clocks'],raw=raw,work_sha256=signatures.pop(),
                    process_identity=f'{process.pid}:{process.create_time()}',warmups=args.warmups if args.mode!='cold' else 0,
                    mode=args.mode,backend=args.backend,scenario='text',sealed_image_bytes=descriptor_bytes,
                    affinity=sorted(os.sched_getaffinity(0)),runtime=sys.version,
                    hardware=dict(launch_policy=serving.resource_controller.launch_policy),
                    diagnostic_stages_enabled=stages is not None,
                    diagnostic_stages=stages.snapshot() if stages is not None else [],
                    profiler_enabled=stages is not None or sys.getprofile() is not None,
                    tracemalloc_enabled=bool(sys.modules.get('tracemalloc') and sys.modules['tracemalloc'].is_tracing()))
                if getattr(args,'reuse_native_workers',False) and args.arm=='native':
                    pool=app.extensions['tofu_native_host']._worker_pool
                    result['worker_reuse_witness']={
                        'physical_workers':len(pool.workers),'idle_workers':len(pool.idle),
                        'pids':sorted(worker.process.pid for worker in pool.workers),
                        'invocations':sum(worker.sequence for worker in pool.workers),
                        'all_ready':all(worker.current is None and not worker.retired for worker in pool.workers)}
                if stages is not None:
                    # Joined diagnostic readback is outside operation clocks.
                    # Aggregate counters carry no request/credential values.
                    metrics=await asyncio.to_thread(storage.metrics)
                    result['diagnostic_storage_metrics']={
                        key:metrics.get(key) for key in ('writer','rpc','attempt_events',
                            'diagnostic_sidecar_stages','diagnostic_sidecar_origin_ns',
                            'diagnostic_sidecar_cprofile')}
                if control is not None:
                    control.sendall(b'D')
                    if control.recv(1)!=b'S':raise ValueError('memory observer did not join')
            assert image._closed
            token_witness['closed']=token_counter is None or (token_counter._state=='closed'
                and token_counter._image.closed and token_counter._library is None)
            return result
        finally:overrides.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-executable',type=Path,help='installed file pinned by the checked archive manifest')
    parser.add_argument('--installed-token-library',type=Path)
    parser.add_argument('--worker',action='store_true');parser.add_argument('--arm',choices=('python','native'))
    parser.add_argument('--python-command',type=json.loads,default=[sys.executable])
    parser.add_argument('--mode',choices=('cold','warm','memory'),default='cold')
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--warmups',type=int,default=20)
    parser.add_argument('--backend',choices=APPLICATION_BACKENDS,default='sqlite')
    parser.add_argument('--storage-engine',type=Path)
    parser.add_argument('--scenario',choices=('text',),default='text')
    parser.add_argument('--memory-control-fd',type=int)
    parser.add_argument('--completion-observer',choices=COMPLETION_OBSERVERS,default='poll')
    parser.add_argument('--native-usage-summary',action='store_true')
    parser.add_argument('--native-terminal-reference',action='store_true')
    parser.add_argument('--diagnostic-stages',action='store_true')
    parser.add_argument('--reuse-native-workers',action='store_true')
    parser.add_argument('--prepare-native-image',action='store_true')
    args=parser.parse_args()
    if args.worker:return worker(args)
    observations=[]
    with tempfile.TemporaryDirectory(prefix='tofu-http-root-receipts-') as temporary:
        for arm in ('python','native'):
            target=Path(temporary)/(arm+'.json')
            result=subprocess.run(args.python_command+[str(Path(__file__).resolve()),'--worker','--arm',arm,
                '--archive',str(args.archive.resolve()),'--output',str(target),
                '--mode',args.mode,'--samples',str(args.samples),'--warmups',str(args.warmups),'--completion-observer',args.completion_observer]
                +['--backend',args.backend]
                +(['--storage-engine',str(args.storage_engine.resolve())] if args.storage_engine else [])
                +(['--native-usage-summary'] if args.native_usage_summary else [])
                +(['--native-terminal-reference'] if args.native_terminal_reference else [])
                +(['--diagnostic-stages'] if args.diagnostic_stages else [])
                +(['--reuse-native-workers'] if args.reuse_native_workers else [])
                +(['--prepare-native-image'] if args.prepare_native_image else [])
                +(['--installed-executable',str(args.installed_executable.resolve())] if args.installed_executable else [])
                +(['--installed-token-library',str(args.installed_token_library.resolve())] if args.installed_token_library else []),capture_output=True,text=True)
            if result.returncode:raise RuntimeError('HTTP ROOT worker failed: '+result.stdout[-1000:]+result.stderr[-4500:])
            observations.append(json.loads(target.read_text()))
    args.output.write_text(json.dumps(dict(scope=__doc__,observations=observations,
        comparison=compare(observations),performance_admitted=False,default_selection_admitted=False),indent=2,allow_nan=False)+'\n')
    print(json.dumps(compare(observations),sort_keys=True),flush=True)


if __name__=='__main__':main()

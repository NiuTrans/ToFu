"""Fresh workers over actual ROOT tasks; loopback model, disposable authority.

Entry points: worker and CLI. Depends on RootWorkload and existing storage/image
owners. Raw warm/cold clocks stay separate from RSS workers. Every observation
must perform identical provider and durable work. No full-project admission.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def quantiles(samples):
    ordered = sorted(samples)
    return dict(p50=statistics.median(ordered), p95=ordered[int((len(ordered)-1)*.95)],
                p99=ordered[int((len(ordered)-1)*.99)])


def worker(args):
    # No inherited live storage/process authority reaches these disposable runs.
    for key in list(os.environ):
        if key.startswith(('TOFU_STORAGE_', 'TOFU_SUPERVISOR_', '_TOFU_')) or key in {
                'PORT', 'TOFU_DATA_DIR', 'TOFU_PROJECT_ROOT', 'TOFU_PROJECT_PATH',
                'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS', 'TOFU_RESOURCE_BUDGET_POLICY_VERSION'}:
            os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='tofu-root-cohort-') as temporary:
        os.environ.update(TOFU_DATA_DIR=temporary, TOFU_CODEX_SHARED_LOGIN='0',
            TOFU_PERSONAL_STORAGE_BACKEND='sqlite' if args.backend=='sqlite' else 'tofudb',
            TOFUDB_TRANSPORT='embedded' if args.backend=='embedded' else 'daemon',
            TOFUDB_OWNER_ID='1', TOFUDB_TENANT_ID='1')
        import psutil
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage import service
        from lib.agent_core.native_image import NativeArchiveImage
        from lib.agent_core.native_process import Budget
        from lib.tasks_pkg.native_host import NativeHost
        from lib.resource_control.runtime import launch_policy
        from scripts.build_native_runtime import verify
        from benchmarks.native_agent_root import RootWorkload, cpu_ns
        from lib.log import _drain_audit_queue
        if args.backend!='sqlite':
            from scripts.check_tofudb_engine_drift import resolve_deployment_binary
            os.environ['TOFUDB_BIN']=resolve_deployment_binary(ROOT)
        runtime=StorageRuntime(StorageSupervisor(project_root=Path(temporary),
            backend=os.environ['TOFU_PERSONAL_STORAGE_BACKEND'], startup_timeout=60),auto_restart=False)
        service.install_runtime_for_test(runtime)
        process=psutil.Process()
        control_fd=getattr(args,'memory_control_fd',None)
        if control_fd is not None and args.mode!='memory':
            raise ValueError('external memory supervision requires a separate memory worker')
        control=socket.socket(fileno=control_fd) if control_fd is not None else None
        if control is not None:control.settimeout(30)
        def cpu_clock():
            # Waited native children plus live storage trees, never coordinator CPU.
            total=cpu_ns()
            for child in process.children(recursive=True):
                clocks=child.cpu_times()
                total+=int((clocks.user+clocks.system+clocks.children_user+clocks.children_system)*1e9)
            return total
        peak=[0];peak_pss=[0];peak_members=[[]];rss_samples=[0];stop=threading.Event();errors=[]
        def sample_memory():
            while not stop.is_set():
                try:
                    def member(p):
                        memory=p.memory_full_info()
                        return dict(pid=p.pid,name=p.name(),rss=memory.rss,pss=memory.pss,uss=memory.uss)
                    members=[member(process)]
                    for child in process.children(recursive=True):
                        try:members.append(member(child))
                        except psutil.NoSuchProcess:pass  # Short-lived native CLI already joined.
                    total=sum(row['rss'] for row in members)
                    peak_pss[0]=max(peak_pss[0],sum(row['pss'] for row in members))
                    if total>peak[0]:peak[0]=total;peak_members[0]=members
                    rss_samples[0]+=1
                except BaseException as error:errors.append(type(error).__name__);return
                stop.wait(.001)
        async def sequence():
            archive=args.archive.resolve();manifest=verify(archive)
            budget=Budget(1,16<<20,1<<20,1<<20,5)
            image=NativeArchiveImage(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                application_version=manifest['build']['application_version'],budget=budget)
            host=NativeHost(image);sampler=None
            runtime.start()
            baseline={(p.pid,p.create_time()) for p in process.children(recursive=True)}
            policy=launch_policy({})
            with ThreadPoolExecutor(max_workers=1) as executor:
                workload=RootWorkload(storage=runtime.client(),host=host,executor=executor,
                    directory=temporary,budget=budget,resource_policy=policy,
                    scenario=args.scenario,cpu_clock=cpu_clock)
                try:
                    if args.mode=='memory':
                        if control is not None:
                            control.sendall(b'R')
                            if control.recv(1)!=b'G':raise ValueError('external memory start was not acknowledged')
                        else:
                            sampler=threading.Thread(target=sample_memory);sampler.start()
                    signatures=set();retained=[]
                    async def operation():
                        row=await workload.run(args.arm)
                        signatures.add(digest(dict(requests=row['requests'],status=row['status'],
                            content=row['content'],usage={k:row['usage'][k] for k in
                                ('prompt_tokens','completion_tokens','total_tokens')})))
                        retained.append(row['retained_task_bytes'])
                        workload.bodies.clear()  # Evidence reduced after full domain assertions.
                        assert {(p.pid,p.create_time()) for p in process.children(recursive=True)}==baseline
                        return row['clocks']
                    first=await operation();rows=[]
                    if args.mode!='cold':
                        for _ in range(args.warmups):await operation()
                        for _ in range(args.samples):rows.append(await operation())
                    assert len(signatures)==1,'fixture work changed inside worker'
                    descriptor_bytes=0
                    if image._image is not None:
                        descriptor_bytes=os.fstat((await image._image).fileno()).st_size
                    return dict(process_identity=f'{os.getpid()}:{process.create_time()}',
                        first_operation=first,raw=rows,work_sha256=signatures.pop(),
                        retained_task_bytes=max(retained),sealed_image_bytes=descriptor_bytes,
                        retained_scope='task messages/content/events/segments/tool rounds; sealed image recorded separately',
                        peak_process_tree_rss_bytes=peak[0] if sampler else None,
                        peak_process_tree_pss_bytes=peak_pss[0] if sampler else None,
                        peak_process_members=peak_members[0],
                        executable_bytes=Path(sys.executable).stat().st_size+descriptor_bytes,
                        rss_samples=rss_samples[0],warmups=args.warmups if args.mode!='cold' else 0,
                        arm=args.arm,backend=args.backend,scenario=args.scenario,mode=args.mode,
                        affinity=sorted(os.sched_getaffinity(0)),runtime=sys.version,
                        hardware=dict(machine=platform.machine(),kernel=platform.release(),launch_policy=policy),
                        profiler_enabled=sys.getprofile() is not None,
                        tracemalloc_enabled=bool(sys.modules.get('tracemalloc') and sys.modules['tracemalloc'].is_tracing()))
                finally:
                    stop.set()
                    if sampler:sampler.join(timeout=5);assert not sampler.is_alive() and not errors
                    try:
                        if control is not None:
                            control.sendall(b'D')
                            if control.recv(1)!=b'S':raise ValueError('external memory completion was not acknowledged')
                    finally:
                        workload.close();await host.close()
        try:
            result=asyncio.run(sequence())
            _drain_audit_queue(timeout=5)
            from lib import log as audit
            assert audit._audit_queue is None or audit._audit_queue.unfinished_tasks==0
        finally:
            try:service.stop_storage()
            finally:
                if control is not None:control.close()
        assert not process.children(recursive=True),'worker leaked children'
        result['joined_cleanup']=True
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')


def summarize(report):
    from benchmarks.system_performance import CONTRACT
    rules=CONTRACT['comparison'];limits=CONTRACT['admission'];issues=[];ratios={}
    warm=report['warm'];cold=report['cold'];memory=report['memory']
    identities=[];signatures=set()
    for row in warm+cold+memory:
        identities.append(row['process_identity']);signatures.add(row['work_sha256'])
        if row.get('joined_cleanup') is not True:issues.append('cleanup incomplete')
        if row.get('profiler_enabled') is not False or row.get('tracemalloc_enabled') is not False:
            issues.append('invalid instrumented observation')
        if row['mode']=='cold' and (row.get('raw') or row.get('warmups')!=0):issues.append('invalid cold operation scope')
        if row['mode']=='memory' and row.get('rss_samples',0)<=0:issues.append('missing memory samples')
        if row['mode']=='memory' and any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) or row[k]<=0
                for k in ('peak_process_tree_rss_bytes','retained_task_bytes')):issues.append('invalid memory metrics')
        clocks=[row['first_operation']]+row['raw']
        if any(type(r.get(k)) not in (int,float) or not math.isfinite(r[k]) or r[k]<=0
                for r in clocks for k in ('wall_ns','cpu_ns')):issues.append('invalid clocks')
        if row['backend']!=report['backend'] or row['scenario']!=report['scenario']:issues.append('changed worker fixture')
    if len(set(identities))!=len(identities):issues.append('worker identity reused')
    if len(signatures)!=1:issues.append('unequal provider or durable work')
    for mode in (warm,memory):
        for pair in range(len(mode)//2):
            if {r['arm'] for r in mode[pair*2:pair*2+2]}!={'python','native'}:issues.append('invalid paired arms')
    if len(warm)<rules['minimum_independent_pairs']*2:issues.append('warm pairs incomplete')
    if len(memory)<rules['minimum_independent_pairs']*2:issues.append('memory pairs incomplete')
    for row in warm:
        if len(row['raw'])<rules['minimum_samples_per_variant'] or row['warmups']<rules['steady_state_warmup_operations_per_variant']:
            issues.append('warm samples incomplete')
    for arm in ('python','native'):
        if sum(row['arm']==arm for row in cold)<rules['minimum_cold_samples_per_variant']:
            issues.append('cold samples incomplete')
    if any(x.startswith('invalid') or x in {'worker identity reused','unequal provider or durable work','changed worker fixture'} for x in issues):
        return dict(issues=sorted(set(issues)),ratios={},verdict='invalid',whole_project_admitted=False,default_selection_admitted=False)
    def compare(name,reference,candidate,bound):
        ratio=candidate/reference;ratios[name]=ratio
        if ratio>bound:issues.append('regression: '+name)
    for pair in range(len(warm)//2):
        rows={row['arm']:row for row in warm[pair*2:pair*2+2]}
        for clock in ('wall_ns','cpu_ns'):
            values={arm:[r[clock]/1e6 for r in row['raw']] for arm,row in rows.items()}
            if clock=='wall_ns':
                metrics={arm:quantiles(v) for arm,v in values.items()}
                for q in ('p50','p95','p99'):
                    compare(f'warm_{pair}_{q}',metrics['python'][q],metrics['native'][q],limits['maximum_latency_ratio'])
            else:compare(f'warm_{pair}_cpu',statistics.mean(values['python']),statistics.mean(values['native']),limits['maximum_cpu_ratio'])
    if cold:
        for clock in ('wall_ns','cpu_ns'):
            values={arm:[r['first_operation'][clock]/1e6 for r in cold if r['arm']==arm] for arm in ('python','native')}
            if all(values.values()):
                for q in ('p50','p95','p99') if clock=='wall_ns' else ('mean',):
                    metric=lambda v:statistics.mean(v) if q=='mean' else quantiles(v)[q]
                    compare('cold_'+clock+'_'+q,metric(values['python']),metric(values['native']),
                        limits['maximum_latency_ratio'] if clock=='wall_ns' else limits['maximum_cpu_ratio'])
    for pair in range(len(memory)//2):
        rows={r['arm']:r for r in memory[pair*2:pair*2+2]}
        compare(f'memory_{pair}_rss',rows['python']['peak_process_tree_rss_bytes'],rows['native']['peak_process_tree_rss_bytes'],limits['maximum_peak_rss_ratio'])
        compare(f'memory_{pair}_retained',rows['python']['retained_task_bytes'],
            rows['native']['retained_task_bytes'],limits['maximum_retained_bytes_ratio'])
    primary=[value for name,value in ratios.items() if name.startswith('warm_') and name.endswith('_p50')]
    gain=bool(primary) and all(value<=1-limits['minimum_primary_improvement_fraction'] for value in primary)
    return dict(issues=sorted(set(issues)),ratios=ratios,
        verdict='regression' if any(x.startswith('regression:') for x in issues) else 'incomplete' if issues else 'qualified_component' if gain else 'no_gain',
        whole_project_admitted=False,default_selection_admitted=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--backend',choices=('sqlite','daemon','embedded'),default='sqlite')
    parser.add_argument('--scenario',choices=('text','discovery_execute','mixed_direct_first','mixed_gateway_first'),default='text')
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--cold-pairs',type=int,default=100);parser.add_argument('--warmups',type=int,default=20)
    parser.add_argument('--python-command',type=json.loads,default=[sys.executable])
    parser.add_argument('--worker',action='store_true');parser.add_argument('--arm',choices=('python','native'))
    parser.add_argument('--mode',choices=('warm','cold','memory'))
    parser.add_argument('--memory-control-fd',type=int)
    args=parser.parse_args()
    if args.worker:return worker(args)
    assert args.samples>0 and args.pairs>0 and args.cold_pairs>=0 and args.warmups>=0
    source_names=['benchmarks/native_agent_root_measure.py','benchmarks/native_agent_root.py',
        'lib/tasks_pkg/native_prepared.py','lib/tasks_pkg/native_gateway.py','lib/tasks_pkg/native_tool_projection.py',
        'lib/llm/tool_wire.py','lib/tasks_pkg/orchestrator/_run.py','lib/agent_core/native_image.py',
        'lib/agent_core/native_artifact.py','lib/agent_core/native_process.py',
        'tests/model_routing/test_native_route.py','tests/model_routing/test_model_routing_contract.py']
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_names}
    report=dict(scope=__doc__,sources=sources,archive_sha256=hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        backend=args.backend,scenario=args.scenario,warm=[],cold=[],memory=[],
        clocks='durable Turn creation through joined ROOT return; common imports/store/server startup and verification readback excluded',
        limitations=['loopback fixed Chat model; no live inference, web ingress or additional task features',
            '1ms wait between procfs memory probes; probe time extends sampling intervals and short peaks can be missed',
            'retained charge covers named task graphs and sealed image, not every application cache'])
    with tempfile.TemporaryDirectory(prefix='tofu-root-results-') as temporary:
        for mode,pairs in (('warm',args.pairs),('memory',args.pairs),('cold',args.cold_pairs)):
            for pair in range(pairs):
                for arm in (('python','native') if pair%2==0 else ('native','python')):
                    for name,sha in sources.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha,name
                    destination=Path(temporary)/'worker.json'
                    command=args.python_command+[str(Path(__file__).resolve()),'--worker','--output',str(destination),
                        '--archive',str(args.archive.resolve()),'--backend',args.backend,'--scenario',args.scenario,
                        '--samples',str(args.samples),'--warmups',str(args.warmups),'--arm',arm,'--mode',mode]
                    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
                    if result.returncode:raise RuntimeError('root worker failed: '+result.stdout[-6000:])
                    report[mode].append(json.loads(destination.read_text()))
                    report['summary']=summarize(report)
                    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
                print(mode,pair+1,'settled',flush=True)
    print(json.dumps(report['summary'],sort_keys=True),flush=True)


if __name__=='__main__':main()

"""Independent stopped-VM memory workers over the retained real ROOT workload.

Entry CLI --archive/--output. Warm/cold clocks are not sampled or admitted here.
Pidfds scope pauses to the disposable tree; stopped kernel comparison groups
address spaces. Original process sums remain alongside unique-VM RSS/PSS.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from benchmarks.process_vm_memory import StoppedTree,VmSamplingError


def join_failed_worker(process,*,grace_seconds=30):
    """A closed control socket lets ordinary failure teardown join first."""
    import psutil
    import signal
    try:process.wait(timeout=grace_seconds);return
    except subprocess.TimeoutExpired:pass
    owner=psutil.Process(process.pid);sampler=StoppedTree(owner)
    with sampler.stopped() as handles:
        for handle in reversed(list(handles.values())):
            try:sampler.kernel.send(handle.fd,signal.SIGKILL)
            except ProcessLookupError:pass
        process.wait(timeout=10)
        pending={handle.fd for handle in handles.values()}
        deadline=time.monotonic()+10
        while pending:
            ready=select.select(list(pending),[],[],max(0,deadline-time.monotonic()))[0]
            pending.difference_update(ready)
            if pending and time.monotonic()>=deadline:raise VmSamplingError('disposable child exit did not join')



def trial(args,arm,directory):
    import psutil
    parent,child=socket.socketpair();parent.settimeout(60)
    output=directory/'worker.json';log=directory/'worker.log'
    worker_script='native_application_root.py' if getattr(args,'entry','prebound')=='application' else 'native_agent_root_measure.py'
    command=args.python_command+[str(ROOT/'benchmarks'/worker_script),'--worker',
        '--output',str(output),'--archive',str(args.archive.resolve()),'--backend',args.backend,
        '--scenario',args.scenario,'--arm',arm,'--mode','memory',
        '--samples',str(args.samples),'--warmups',str(args.warmups),'--memory-control-fd',str(child.fileno())]
    if getattr(args,'entry','prebound')=='application' and getattr(args,'storage_engine',None) is not None:
        command+=['--storage-engine',str(args.storage_engine.resolve())]
    if getattr(args,'entry','prebound')=='application':
        command+=['--completion-observer',getattr(args,'completion_observer','poll')]
    if getattr(args,'reuse_native_workers',False):command+=['--reuse-native-workers']
    if getattr(args,'installed_executable',None) is not None:
        command+=['--installed-executable',str(args.installed_executable.resolve())]
    if getattr(args,'installed_token_library',None) is not None:
        command+=['--installed-token-library',str(args.installed_token_library.resolve())]
    process=None
    try:
        with log.open('wb') as stream:
            process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,pass_fds=(child.fileno(),))
            child.close()
            if parent.recv(1)!=b'R':raise VmSamplingError('memory worker did not complete readiness')
            owner=psutil.Process(process.pid);sampler=StoppedTree(owner)
            parent.sendall(b'G');deadline=time.monotonic()+args.timeout
            snapshots=[]
            while True:
                if time.monotonic()>=deadline:raise VmSamplingError('memory worker exceeded observation deadline')
                if select.select([parent],[],[],args.interval)[0]:
                    if parent.recv(1)!=b'D':raise VmSamplingError('memory worker lost its completion channel')
                    parent.sendall(b'S');break
                snapshots.append(sampler.snapshot())
            process.wait(timeout=30)
        if process.returncode:raise VmSamplingError('memory worker failed: '+log.read_text()[-6000:])
        result=json.loads(output.read_text())
        if not snapshots:raise VmSamplingError('memory worker has no stopped observation')
        for row in snapshots:
            if not row['all_threads_stopped'] or not row['kernel_vm_comparison']:
                raise VmSamplingError('memory identity is not certified')
        peaks={field:max(snapshots,key=lambda row:row[field]) for field in
            ('raw_process_rss_bytes','raw_process_pss_bytes','unique_vm_rss_bytes','unique_vm_pss_bytes')}
        result.update(memory_scope='stopped kernel-confirmed unique address spaces',memory_snapshots=len(snapshots),
            peak_snapshots=peaks,pause_ns_total=sum(row['pause_ns'] for row in snapshots),
            maximum_pause_ns=max(row['pause_ns'] for row in snapshots),
            timing_admitted=False,joined_memory_observer=True)
        return result
    finally:
        parent.close();child.close()
        if process is not None and process.poll() is None:
            join_failed_worker(process)


def summary(rows,samples,warmups,*,storage_authority=None,worker_reuse=None,completion_observer=None,image_source="archive",native_token_count=False):
    from benchmarks.system_performance import CONTRACT
    rules=CONTRACT['comparison'];limit=CONTRACT['admission']['maximum_peak_rss_ratio']
    issues=[];ratios={};identities=set();work=set();image_identities=set()
    if image_source not in ("archive", "installed_executable"):
        issues.append("invalid executable admission source")
    if completion_observer is None and any('completion_observer' in row for row in rows):
        completion_observer='poll'  # Legacy callers cannot implicitly select a new boundary.
    for row in rows:
        from benchmarks.native_application_authority import check_token_library
        try:check_token_library(row,native_token_count)
        except ValueError as error:issues.append('invalid '+str(error))
        actual_source=row.get('image_source','archive')
        if actual_source != image_source or actual_source not in ('archive','installed_executable'):
            issues.append('invalid executable admission source mixture')
        executable_pin=row.get('executable_sha256')
        pin_valid=(isinstance(executable_pin,str) and len(executable_pin)==64
            and all(ch in '0123456789abcdef' for ch in executable_pin))
        if not pin_valid and (image_source=='installed_executable' or executable_pin is not None):
            issues.append('invalid installed executable pin')
        elif actual_source in ('archive','installed_executable'):
            library_pin=row.get('token_library_sha256')
            if library_pin is not None and not isinstance(library_pin,str):
                issues.append('invalid token library pin')
            else:image_identities.add((actual_source,executable_pin,library_pin))
        if row.get('native_terminal_reference',False) is not False:
            issues.append('invalid unqualified native terminal reference memory selection')
        if row.get('native_usage_summary',False) is not False:
            issues.append('invalid unqualified native usage summary memory selection')
        if completion_observer is not None:
            from benchmarks.native_application_authority import check_completion_observer
            try:check_completion_observer(row,completion_observer)
            except ValueError as error:issues.append(str(error))
        if worker_reuse is not None:
            from benchmarks.native_application_authority import check_worker_reuse
            try:check_worker_reuse(row,worker_reuse)
            except ValueError as error:issues.append(str(error))
        if storage_authority is not None:
            from benchmarks.native_application_authority import check_authority
            try:check_authority(row,storage_authority)
            except ValueError:issues.append('changed storage authority')
        identity=row['process_identity'];work.add(row['work_sha256'])
        if identity in identities:issues.append('worker identity reused')
        identities.add(identity)
        if row.get('joined_cleanup') is not True or row.get('joined_memory_observer') is not True:
            issues.append('incomplete cleanup')
        if (row.get('memory_snapshots',0)<=0 or len(row.get('raw',[]))<max(samples,rules['minimum_samples_per_variant'])
                or row.get('warmups',0)<max(warmups,rules['steady_state_warmup_operations_per_variant'])):
            issues.append('incomplete memory observations')
        if row.get('timing_admitted') is not False:issues.append('memory clocks cannot grant timing admission')
        if row.get('memory_scope')!='stopped kernel-confirmed unique address spaces':issues.append('invalid memory scope')
        for metric in ('unique_vm_rss_bytes','unique_vm_pss_bytes'):
            peak=(row.get('peak_snapshots') or {}).get(metric,{})
            value=peak.get(metric)
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                issues.append('invalid memory metric')
            if peak.get('all_threads_stopped') is not True or peak.get('kernel_vm_comparison') is not True:
                issues.append('invalid running memory comparison')
    if len(image_identities)>1:issues.append('invalid executable pin mixture')
    if len(work)!=1:issues.append('different provider or durable work')
    if len(rows)<rules['minimum_independent_pairs']*2:issues.append('fewer than three independent memory pairs')
    if any(value.startswith('invalid') or value in {'worker identity reused','different provider or durable work','changed storage authority','changed native worker reuse policy','changed completion observer'} for value in issues):
        return dict(verdict='invalid',issues=sorted(set(issues)),ratios={},performance_admitted=False,default_selection_admitted=False)
    for pair in range(len(rows)//2):
        arms={r['arm']:r for r in rows[pair*2:pair*2+2]}
        if set(arms)!={'python','native'}:issues.append('invalid paired arms');continue
        for metric in ('unique_vm_rss_bytes','unique_vm_pss_bytes'):
            values={arm:row['peak_snapshots'][metric][metric] for arm,row in arms.items()}
            ratios[f'{pair}_{metric}']=values['native']/values['python']
            if ratios[f'{pair}_{metric}']>limit:issues.append('memory regression: '+metric)
    return dict(verdict='regression' if any(x.startswith('memory regression:') for x in issues)
        else 'incomplete' if issues else 'qualified_memory_component',issues=sorted(set(issues)),ratios=ratios,
        performance_admitted=False,default_selection_admitted=False)


def main():
    from benchmarks.native_application_authority import COMPLETION_OBSERVERS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-executable',type=Path)
    parser.add_argument('--installed-token-library',type=Path)
    parser.add_argument('--storage-engine',type=Path)
    parser.add_argument('--reuse-native-workers',action='store_true')
    parser.add_argument('--completion-observer',choices=COMPLETION_OBSERVERS,default='poll')
    parser.add_argument('--entry',choices=('prebound','application'),default='prebound')
    parser.add_argument('--backend',choices=('sqlite','daemon','embedded'),default='sqlite')
    parser.add_argument('--scenario',choices=('text','discovery_execute','mixed_direct_first','mixed_gateway_first'),default='text')
    parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--warmups',type=int,default=20);parser.add_argument('--interval',type=float,default=.05)
    parser.add_argument('--timeout',type=float,default=300);parser.add_argument('--python-command',type=json.loads,default=[sys.executable])
    args=parser.parse_args();assert args.pairs>0 and args.samples>0 and args.warmups>=0 and args.interval>0 and args.timeout>0
    if args.entry!='application' and args.completion_observer!='poll':
        raise ValueError('worker-future observer requires application entry')
    if args.installed_token_library is not None and args.installed_executable is None:
        raise ValueError('installed token library requires installed executable')
    if args.installed_executable is not None and args.entry!='application':
        raise ValueError('installed executable requires application entry')
    names=['lib/storage/runtime.py','lib/storage/supervisor.py','tofu_db/embedded.py','serverctl_pkg/native_engine.py','lib/tasks_pkg/invocation.py','lib/tasks_pkg/native_gateway.py','lib/agent_core/native_token_count.py','lib/token_counter/native.py','lib/token_counter/tiktoken_counter.py','benchmarks/native_root_memory.py','benchmarks/process_vm_memory.py','benchmarks/native_agent_root_measure.py',
        'benchmarks/native_agent_root.py','lib/tasks_pkg/native_context.py','lib/tasks_pkg/native_prepared.py','lib/tasks_pkg/native_tool_projection.py',
        'lib/agent_core/native_process.py','lib/agent_core/native_image.py','lib/agent_core/native_artifact.py',
        'benchmarks/native_completion.py','benchmarks/native_application_authority.py']
    if args.reuse_native_workers and args.entry!='application':raise ValueError('worker reuse requires application entry')
    witness=None
    if args.entry=='application':
        if args.backend not in ('sqlite','daemon','embedded') or args.scenario!='text':raise ValueError('application memory requires an explicit SQLite/native transport and text workload')
        from benchmarks.native_application_authority import authority_witness,check_authority_files,check_authority
        witness=authority_witness(args.backend,args.storage_engine,ROOT)
        names+=['benchmarks/native_application_authority.py','benchmarks/native_application_root.py','lib/tasks_pkg/native_application.py',
            'lib/tasks_pkg/native_host.py','lib/agent_core/native_worker_pool.py',
            'lib/tasks_pkg/manager/_persist.py','lib/tasks_pkg/orchestrator/_post_commit.py']
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report=dict(scope=__doc__,archive_sha256=hashlib.sha256(args.archive.read_bytes()).hexdigest(),sources=sources,
        image_source='installed_executable' if args.installed_executable else 'archive',
        native_token_count=args.installed_token_library is not None,
        completion_observer=args.completion_observer if args.entry=='application' else None,entry=args.entry,worker_reuse=args.reuse_native_workers,backend=args.backend,storage_authority=witness,scenario=args.scenario,memory=[],limits=['sampling pauses are exclusive to separate memory workers',
        '50ms default wait plus probe time may miss short resident peaks','whole project and timing/cold admission remain separate'])
    with tempfile.TemporaryDirectory(prefix='tofu-stopped-memory-') as temporary:
        for pair in range(args.pairs):
            for arm in ('python','native') if pair%2==0 else ('native','python'):
                for name,sha in sources.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha,name
                if witness is not None:check_authority_files(args.storage_engine,witness)
                row=trial(args,arm,Path(temporary))
                if witness is not None:check_authority(row,witness)
                if row.get('archive_sha256')!=report['archive_sha256']:
                    raise ValueError('archive changed during memory cohort')
                report['memory'].append(row)
                report['summary']=summary(report['memory'],args.samples,args.warmups,storage_authority=witness,worker_reuse=args.reuse_native_workers if args.entry=='application' else None,
                    completion_observer=args.completion_observer if args.entry=='application' else None,image_source=report['image_source'],native_token_count=report['native_token_count'])
                args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
            print(pair+1,'memory pair completed',flush=True)
    print(json.dumps(report['summary'],sort_keys=True),flush=True)


if __name__=='__main__':main()

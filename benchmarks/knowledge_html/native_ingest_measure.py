"""Paired complete ingestion clocks with first-use HTML activation included.

Compare the current Python owner with the installed or archived Rust projector.
Five actual ingestion cases, fresh cold workers, independent complete memory
cohorts and explicit source/exit witnesses. Unqualified memory remains missing;
no collector result alone activates application defaults or certifies HTTP cancel.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from benchmarks.knowledge_html.native_ingest_worker import CASES,CPU_SCOPE,digest,measure
from benchmarks.knowledge_html.native_ingest_provenance import CLOCK_SCOPE, DatabaseProvenance

if not __debug__:
    raise RuntimeError('ingestion evidence requires enabled validation checks')


def receipt(path):
    path=Path(path)
    return dict(path=str(path.resolve()),bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def sources():
    names=[]
    for folder in ('lib/knowledge','lib/pdf_parser','lib/doc_parser','lib/storage','tofu_db','benchmarks/knowledge_html'):
        names.extend(str(p.relative_to(ROOT)) for p in (ROOT/folder).rglob('*.py'))
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'lib/agent_core').glob('native*.py')]
    names += ['packages/tofu-runtime/src/'+n for n in ('html_projection.rs','html_tokenizer.rs','generated_html_projection.rs','html_projection_abi.rs','generated_html_projection_abi.rs')]
    names += ['benchmarks/system_performance.py','benchmarks/system_performance_contract.json',
              'benchmarks/process_exit_memory.py','benchmarks/process_memory_counters.py',
              'benchmarks/repository_index/child_memory.py','benchmarks/repository_index/record_journal.py',
              'lib/orchestration/native_flow_contract.py',
              'serverctl_pkg/native_engine.py','runtime_guards.py']
    return {name:receipt(ROOT/name)['sha256'] for name in sorted(set(names))}


def validate_worker(row,config,seen,affinity):
    assert row['clock_scope']==CLOCK_SCOPE and row['database_provenance']==config['database_provenance']
    assert (row['variant'],row['case'],row['mode'])==(config['variant'],config['case'],config['mode'])
    identity=row['process_identity'];assert identity not in seen
    assert int(identity.split(':')[1])==row['pid'];seen.add(identity)
    assert row['affinity']==affinity and row['joined_cleanup']
    assert row['python']==sys.version
    assert row['cpu_scope']==CPU_SCOPE and not row['profiler_enabled'] and not row['tracemalloc_enabled']
    assert row['warmup_operations']==(0 if config['mode']=='cold' else 20)
    count=1 if config['mode']=='cold' else config['samples']
    assert len(row['raw_clocks'])==len(row['wall_samples_ms'])==len(row['cpu_samples_ms'])==count
    for raw,wall,cpu in zip(row['raw_clocks'],row['wall_samples_ms'],row['cpu_samples_ms']):
        assert raw['wall_ns']>0 and raw['worker_cpu_ns']>0 and raw['reaped_children_cpu_ns']>=0
        assert raw['cpu_ns']==raw['worker_cpu_ns']+raw['reaped_children_cpu_ns']
        assert wall==raw['wall_ns']/1e6 and cpu==raw['cpu_ns']/1e6
    expected=config['variant']=='candidate' and config['case']!='pdf'
    assert bool(row['html_profile'])==expected and (row['projection_calls']>0)==expected
    operations=1 if config['mode']=='cold' else 21+config['samples']
    assert row['projection_calls']==(operations*(3 if config['case']=='bounded_memory' else 1) if expected else 0)
    if expected:
        for name,pin in row['html_profile']['native_sources'].items():
            assert receipt(ROOT/'packages/tofu-runtime/src'/name)['sha256']==pin
    if config['case']=='cancellation':
        producers=row['producer_receipts'];assert len(producers)==(1 if config['mode']=='cold' else 21+config['samples'])
        assert len({r['producer_pid'] for r in producers})==len(producers)
        assert all(r['producer_returncode']==-9 for r in producers)
        assert all(r['projection_calls']==int(expected) for r in producers)
        for producer in producers:
            identity=producer['producer_identity'];assert identity not in seen
            assert int(identity.split(':')[1])==producer['producer_pid'];seen.add(identity)
            assert producer['html_profile']==row['html_profile']
            if config['mode']=='memory':assert producer['producer_child_memory_receipts']


def qualify_memory(worker,observer):
    """Refuse unsupported exit topologies; never substitute a sampled peak."""
    assert worker['mode']=='memory' and worker['clock_records_materialized_after_memory_counters']
    assert observer['observation_valid'] and observer['all_tracees_joined']
    assert observer['root_returncode']==0 and observer['child_memory_complete']
    assert worker['pid']==observer['root_pid']
    boot,pid,ticks=worker['process_identity'].split(':')
    assert boot==observer['boot_id'] and int(pid)==worker['pid']
    roots=[r for r in observer['tracees'] if r['pid']==worker['pid']]
    assert len(roots)==1 and roots[0]['start_ticks']==ticks
    if worker.get('case')=='cancellation':
        from benchmarks.knowledge_html.native_ingest_memory import child_tree_peak
        peak=child_tree_peak(worker,observer)
        counters=worker['process_image_counters']
        assert counters['Pid']==counters['Tgid']==worker['pid'] and counters['VmHWM']>0
        return dict(peak_rss_bytes=counters['VmHWM']+peak+worker['sealed_html_image_bytes'],
            retained_bytes=worker['retained_bytes'],retention_baseline_rss_bytes=worker['retention_baseline_rss_bytes'],
            post_cleanup_rss_bytes=worker['post_cleanup_rss_bytes'],
            memory_scope='parent post-exec VmHWM plus maximum observed sequential descendant path of process-group exit peaks plus sealed HTML bytes; shared threads count once; independent peaks/shared pages can overlap; not peak PSS')
    assert not observer.get('allow_threaded_children',False)
    children={r['pid']:r for r in observer['tracees'] if r['tgid']!=worker['pid']}
    waited={r['pid']:r for r in worker['child_memory_receipts']}
    assert children and children.keys()==waited.keys(), 'non-direct descendants require their own exit receipts'
    peaks=[]
    for pid,row in children.items():
        assert row['joined'] and row['pid']==row['tgid'] and row['creator_tgid']==worker['pid']
        assert len(row['execs'])==1 and row['returncode']==waited[pid]['returncode']
        assert os.waitstatus_to_exitcode(row['exit_event_status'])==row['returncode']
        assert row['wait4_peak_rss_bytes']==waited[pid]['peak_rss_bytes']
        memory=row['memory'];status=memory['status']
        assert status['Pid']==pid and status['PPid']==worker['pid'] and status['Threads']==1
        assert memory['peak_rss_estimate']==max(status['VmHWM'],memory['exit_smaps']['Rss'])>0
        peaks.append(memory['peak_rss_estimate'])
    counters=worker['process_image_counters']
    assert counters['Pid']==counters['Tgid']==worker['pid'] and counters['VmHWM']>0
    return dict(peak_rss_bytes=counters['VmHWM']+max(peaks)+worker['sealed_html_image_bytes'],
        retained_bytes=worker['retained_bytes'],retention_baseline_rss_bytes=worker['retention_baseline_rss_bytes'],
        post_cleanup_rss_bytes=worker['post_cleanup_rss_bytes'],
        memory_scope='parent post-exec VmHWM plus maximum sequential direct-child post-exec exit peak plus sealed HTML bytes; independent peaks/shared pages can overlap; not peak PSS')


def run(command,env,output,timeout):
    with subprocess.Popen(command,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,start_new_session=True) as process:
        try:stdout,stderr=process.communicate(timeout=timeout)
        except BaseException:
            # This session contains only this owned worker/observer and its children.
            os.killpg(process.pid,signal.SIGKILL);process.communicate();raise
        output.write_text(stderr)
        if process.returncode:raise RuntimeError('owned measurement failed: '+stderr[-3000:])
        return process.pid,stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-config',type=Path);parser.add_argument('--worker-output',type=Path)
    parser.add_argument('--check-config',type=Path)
    parser.add_argument('--engine',type=Path);parser.add_argument('--pdf',type=Path)
    parser.add_argument('--installation',type=Path);parser.add_argument('--archive',type=Path)
    parser.add_argument('--python-command',type=Path)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--cold-pairs',type=int,default=100)
    parser.add_argument('--cases',nargs='+',choices=CASES,default=CASES)
    args=parser.parse_args()
    if args.check_config:
        from benchmarks.knowledge_html.native_ingest_check import check_workloads
        result=check_workloads(json.loads(args.check_config.read_text()))
        args.worker_output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');return
    if args.worker_config:
        result=measure(json.loads(args.worker_config.read_text()))
        args.worker_output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');return
    assert args.engine and args.pdf and args.python_command and args.output_dir
    assert bool(args.installation)!=bool(args.archive)
    assert min(args.samples,args.pairs,args.cold_pairs)>0 and len(set(args.cases))==len(args.cases)
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    python_command=json.loads(args.python_command.read_text())
    assert isinstance(python_command,list) and python_command and all(type(v) is str for v in python_command)
    if args.installation:
        installed=json.loads(args.installation.read_text());artifact=receipt(installed['html_library'])
        assert artifact['sha256']==installed['html_library_sha256'] and artifact['bytes']==installed['bytes']
        binding=dict(kind='installed',path=artifact['path'],sha256=artifact['sha256'],bytes=artifact['bytes'])
    else:
        from lib.agent_core.native_artifact import inspect_archive
        manifest,_=inspect_archive(args.archive)
        artifact=receipt(args.archive)
        binding=dict(kind='archive',path=artifact['path'],sha256=artifact['sha256'],
            bytes=manifest['files']['lib/libtofu_runtime.so']['bytes'],application_version=manifest['build']['application_version'])
    from serverctl_pkg.native_engine import source_fingerprint
    from benchmarks.system_performance import compare
    from runtime_guards import resource_budget_manifest,probe_system_resources
    provenance=DatabaseProvenance.capture(ROOT,args.engine)
    db_source=provenance.source_sha256
    frozen=sources();files={name:receipt(path) for name,path in
        [('engine',args.engine),('database_library',args.engine.with_name('libtofu_db.so')),('pdf',args.pdf),('artifact',Path(binding['path'])),('launcher',args.python_command)]}
    affinity=sorted(os.sched_getaffinity(0));seen=set()
    env=dict(os.environ,PYTHONHASHSEED='0')
    runtime=dict(python=sys.version,platform=platform.platform(),affinity=affinity,python_command=python_command,cpu_scope=CPU_SCOPE,clock_scope=CLOCK_SCOPE)
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    reports={arm:dict(format='tofu.system-performance/v1',scope=__doc__,sources=frozen,artifacts=files,
        clock_scope=CLOCK_SCOPE,database_provenance=provenance.receipt(),
        workloads=[],default_selection_admitted=False,whole_project_admitted=False) for arm in ('reference','candidate')}
    def frozen_check():
        assert sources()==frozen
        assert source_fingerprint(ROOT)==db_source
        assert all(receipt(value['path'])==value for value in files.values())
    check_config=out/'checks.input.json';check_output=out/'checks.json'
    check_config.write_text(json.dumps(dict(engine=str(args.engine.resolve()),pdf=str(args.pdf.resolve()),
        cases=args.cases,binding=binding,python_command=python_command),indent=2)+'\n')
    run([*python_command,__file__,'--check-config',str(check_config),'--worker-output',str(check_output)],
        env,out/'checks.stderr.log',3700)
    checks=json.loads(check_output.read_text());assert checks['passed']
    frozen_check()
    for report in reports.values():report['correctness_receipt']=receipt(check_output)
    def save():
        frozen_check()
        for arm,report in reports.items():(out/(arm+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        (out/'verdict.json').write_text(json.dumps(compare(reports['reference'],reports['candidate']),indent=2)+'\n')
    def invoke(arm,case,mode,label):
        frozen_check();config=dict(variant=arm,case=case,mode=mode,samples=args.samples,
            engine=str(args.engine.resolve()),pdf=str(args.pdf.resolve()),binding=binding,python_command=python_command,
            database_provenance=provenance.receipt())
        request=out/(label+'.input.json');target=out/(label+'.json')
        request.write_text(json.dumps(config,indent=2)+'\n')
        command=[*python_command,__file__,'--worker-config',str(request),'--worker-output',str(target)]
        observer=out/(label+'.exit.json')
        if mode=='memory':command=[*python_command,str(ROOT/'benchmarks/process_exit_memory.py'),'--receipt',str(observer),'--timeout','3600',
                                  *(['--allow-threaded-children'] if case=='cancellation' else []),'--',*command]
        try:pid,_=run(command,env,out/(label+'.stderr.log'),3700)
        except RuntimeError:
            if mode!='memory' or not observer.exists():raise
            refused=json.loads(observer.read_text())
            if refused['observation_valid'] or not refused['all_tracees_joined']:raise
            frozen_check()
            return None,refused
        row=json.loads(target.read_text());validate_worker(row,config,seen,affinity)
        assert row['db_identity']==dict(source_sha256=db_source)
        if mode!='memory':assert row['pid']==pid
        frozen_check()
        return row,json.loads(observer.read_text()) if mode=='memory' else None
    for case in args.cases:
        rows={}
        for arm,report in reports.items():
            checked=checks['cases'][case]
            row=dict(domain='ingest',case=case,scope='domain_integration',correctness_passed=checked['correctness_passed'],
                failure_and_cleanup_passed=checked['failure_and_cleanup_passed'],
                cancellation_scope=checked['cancellation_scope'],
                identity=dict(source_sha256=digest(dict(sources=frozen,arm=arm,binding=binding)),runtime=runtime,hardware=hardware,fixture_sha256=digest(dict(case=case,pdf=files['pdf'],samples=args.samples))),
                measurements=[],cold_start=dict(scope='first_operation_per_fresh_process',wall_samples_ms=[],cpu_samples_ms=[],process_identities=[],profiler_enabled=False,tracemalloc_enabled=False))
            report['workloads'].append(row);rows[arm]=row
        for pair in range(args.pairs):
            order=('reference','candidate') if pair%2==0 else ('candidate','reference')
            warm={arm:invoke(arm,case,'warm',f'{case}-warm-{pair}-{arm}')[0] for arm in order}
            assert warm['reference']['output_trace_sha256']==warm['candidate']['output_trace_sha256']
            assert warm['reference']['first_operation']['output_sha256']==warm['candidate']['first_operation']['output_sha256']==checked['rows']['reference']['output_sha256']
            for arm in order:
                resident,observer=invoke(arm,case,'memory',f'{case}-memory-{pair}-{arm}')
                if resident is None:
                    warm[arm].update(memory_separate_process=False,
                        memory_refusal=observer['error'],
                        memory_observation=receipt(out/f'{case}-memory-{pair}-{arm}.exit.json'))
                    rows[arm].setdefault('unqualified_measurements',[]).append(warm[arm])
                    continue
                assert resident['output_trace_sha256']==warm[arm]['output_trace_sha256']
                assert resident['first_operation']['output_sha256']==warm[arm]['first_operation']['output_sha256']
                try:metrics=qualify_memory(resident,observer)
                except (AssertionError,KeyError) as error:
                    warm[arm]['memory_refusal']=str(error) or 'unsupported child exit topology'
                    metrics={}
                warm[arm].update(metrics,memory_separate_process=True,memory_process_identity=resident['process_identity'],memory_observation=receipt(out/f'{case}-memory-{pair}-{arm}.json'))
                destination='measurements' if metrics else 'unqualified_measurements'
                rows[arm].setdefault(destination,[]).append(warm[arm])
            save();print(case,'warm pair',pair,{m:warm['candidate'][m]/warm['reference'][m] for m in ('p50_ms','p95_ms','p99_ms','cpu_ms_per_operation')},flush=True)
        for pair in range(args.cold_pairs):
            order=('reference','candidate') if pair%2==0 else ('candidate','reference')
            cold={arm:invoke(arm,case,'cold',f'{case}-cold-{pair}-{arm}')[0] for arm in order}
            assert cold['reference']['output_trace_sha256']==cold['candidate']['output_trace_sha256']
            assert cold['reference']['output_trace_sha256']==checked['rows']['reference']['output_sha256']
            for arm in order:
                target=rows[arm]['cold_start']
                for key in ('wall_samples_ms','cpu_samples_ms'):target[key].extend(cold[arm][key])
                target['process_identities'].append(cold[arm]['process_identity'])
            save()
            if (pair+1)%10==0 or pair+1==args.cold_pairs:print(case,'cold pairs',pair+1,'joined',flush=True)
    print(json.dumps(compare(reports['reference'],reports['candidate'])),flush=True)


if __name__=='__main__':main()

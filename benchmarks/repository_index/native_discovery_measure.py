"""Explicit Rust discovery experiment through the real repository tool lifecycle.

Both arms use the same installed native ranking. Every fixture is a real Git
repository; only this owned child replaces list_files. No application selection.
Fresh cold workers, separate full memory cohorts, raw CPU including Git children,
source/package/executable receipts and exact publication hashes are mandatory.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
import native_domain_measure as collector
import native_domain_worker as worker
from child_memory import observe_children, child_receipts
from exit_memory import MEMORY_SCOPE, qualify
from lib.repo_map import outline


def sources(root=ROOT):
    result = dict(collector.source_receipts(), **{
        str(Path(__file__).relative_to(ROOT)): hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'benchmarks/repository_index/child_memory.py': hashlib.sha256((Path(__file__).parent/'child_memory.py').read_bytes()).hexdigest()})
    for name in ('benchmarks/process_exit_memory.py', 'benchmarks/repository_index/exit_memory.py'):
        result[name] = hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    if root != ROOT:
        return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in result}
    return result


def measure(arm, case, mode, samples, check=False, transport="ffi", *, post_exec_memory=False):
    original_init, original_list = worker.Workload.__init__, outline.list_files
    calls = 0
    def initialize(self, root, case):
        original_init(self, root, case)
        subprocess.run(['git', '-C', str(root), 'init', '-q'],
                       capture_output=True, check=True, timeout=20)
    def discover(root, limit=100_000):
        nonlocal calls
        if transport == 'process':
            from tofu_repo_kernel import load_discovery_process
            result = load_discovery_process().list_files(root)
        else:
            from tofu_repo_kernel import load_discovery
            result = load_discovery().list_files(root)
        calls += 1
        return sorted({path.decode('utf8', 'replace') for path in result})
    try:
        worker.Workload.__init__ = initialize
        if arm == 'rust': outline.list_files = discover
        if check:
            from native_domain_check import check as check_domain
            result = check_domain()
            assert (calls > 0) == (arm == 'rust')
            result.update(discovery_arm=arm, discovery_transport=transport, native_discovery_calls=calls, sources=sources())
            return result
        with observe_children() if mode == 'memory' else nullcontext() as children:
            result = worker.measure('candidate', case, mode, samples, post_exec_memory=post_exec_memory)
        if mode == 'memory':
            receipts = child_receipts(children)
            # One Git initialization, then first use + 20 warmups + the cohort.
            assert len(receipts) == (samples + 22 if arm == 'git' or transport == 'process' else 1)
            assert all(row['returncode'] == 0 for row in receipts)
            image_bytes = 0
            if arm == 'rust' and transport == 'process':
                from tofu_repo_kernel import load_discovery_process
                image_bytes = os.fstat(load_discovery_process()._image.fileno()).st_size
            child_peak = max(row['peak_rss_bytes'] for row in receipts)
            result.update(parent_peak_rss_bytes=result['peak_rss_bytes'],
                          discovery_child_peak_rss_bytes=child_peak,
                          retained_executable_image_bytes=image_bytes,
                          child_memory_receipts=receipts,
                          child_record_journal=children.journal_receipt,
                          child_records_materialized_after_worker=True,
                          memory_scope='parent peak RSS + maximum joined child peak RSS + sealed image bytes; conservative sum; wait4 includes pre-exec inherited RSS and shared pages may be counted twice')
            result['peak_rss_bytes'] += child_peak + image_bytes
        expected = 1 if mode == 'cold' else samples + 21
        assert calls == (expected if arm == 'rust' else 0)
        result.update(discovery_arm=arm, discovery_transport=transport, native_discovery_calls=calls, sources=sources())
        return result
    finally:
        outline.list_files = original_list
        worker.Workload.__init__ = original_init


def aggregate_cold(values, metric, quantile):
    """Use the common admission mean for CPU and quantiles for latency."""
    ordered = sorted(values)
    if metric == 'cpu_ms_per_operation':
        return statistics.mean(ordered)
    return statistics.median(ordered) if quantile is None else ordered[int((len(ordered)-1)*quantile)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-package',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--cold-pairs',type=int,default=100)
    parser.add_argument('--cases',nargs='+',choices=worker.CASES,default=worker.CASES)
    parser.add_argument('--worker',choices=('git','rust'))
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--transport',choices=('ffi','process'),default='ffi')
    parser.add_argument('--post-exec-memory',action='store_true',help='Internal memory worker counter selection; requires external exit observation')
    parser.add_argument('--memory-observer',choices=('wait4','exit'),default='wait4')
    parser.add_argument('--reference-root',type=Path,help='Frozen reference checkout; omit for discovery-only comparison')
    parser.add_argument('--mode',choices=('warm','memory','cold'))
    args=parser.parse_args()
    if args.worker:
        row=measure(args.worker,args.cases[0],args.mode,args.samples,args.check,args.transport,post_exec_memory=args.post_exec_memory)
        args.output_dir.write_text(json.dumps(row,indent=2,allow_nan=False)+'\n')
        return
    if args.post_exec_memory:raise ValueError('post-exec counters are selected only in a worker')
    if args.memory_observer == 'exit' and args.transport != 'process':
        raise ValueError('exit memory observation requires process transport')
    assert min(args.samples,args.pairs,args.cold_pairs)>0
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    native=args.native_package.resolve();receipt=collector.package_receipt(native);source=sources()
    roots={'git':args.reference_root.resolve() if args.reference_root else ROOT,'rust':ROOT}
    variant_sources={arm:sources(root) for arm,root in roots.items()}
    # Both arms must use exactly the installed package this collector certified.
    package_sources={name:value for name,value in source.items() if name.startswith('packages/tofu-repo-kernel/')}
    assert all({name:value for name,value in values.items() if name in package_sources}==package_sources
               for values in variant_sources.values())
    executables=worker.discovery_executables();packages=worker.packages()
    affinity=sorted(os.sched_getaffinity(0)); seen=set(); cases=[]; failures=[]
    config=out/'git-global-config';config.write_text('')
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(PYTHONHASHSEED='0',PYTHONPATH=str(native)+os.pathsep+os.environ.get('PYTHONPATH',''),
               GIT_CONFIG_GLOBAL=str(config),GIT_CONFIG_NOSYSTEM='1')
    from runtime_guards import probe_system_resources, resource_budget_manifest
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    def invoke(arm,case,mode,pair):
        assert sources(roots[arm])==variant_sources[arm] and collector.package_receipt(native)==receipt
        assert worker.discovery_executables()==executables
        target=out/f'{case}-{mode}-{pair}-{arm}.json'
        post_exec = mode == 'memory' and args.memory_observer == 'exit'
        raw_target = target.with_name(target.stem+'-worker.json') if post_exec else target
        command = [args.python,str(roots[arm]/'benchmarks/repository_index/native_discovery_measure.py'),
            '--native-package',str(native),'--output-dir',str(raw_target),'--worker',arm,
            '--mode',mode,'--samples',str(args.samples),'--cases',case,'--transport',args.transport]
        if post_exec:
            observation = target.with_name(target.stem+'-observer.json')
            command += ['--post-exec-memory']
            command = [args.python,str(ROOT/'benchmarks/process_exit_memory.py'),
                       '--receipt',str(observation),'--timeout','150','--',*command]
        with tempfile.TemporaryDirectory(prefix='data-',dir=out) as data:
            run=subprocess.run(command,cwd=roots[arm],env=dict(env,TOFU_DATA_DIR=data),capture_output=True,text=True,timeout=180)
        target.with_suffix('.log').write_text(run.stdout+run.stderr)
        assert run.returncode==0,run.stderr[-3000:]
        row=json.loads(raw_target.read_text())
        if post_exec:
            row = qualify(row,json.loads(observation.read_text()))
            row['raw_memory_artifacts'] = dict(worker=collector.file_receipt(raw_target),
                                               observer=collector.file_receipt(observation))
            target.write_text(json.dumps(row,indent=2,allow_nan=False)+'\n')
        collector._assert_observation(row,'candidate',case,mode,args.samples,seen,packages,affinity,executables)
        assert row['sources']==variant_sources[arm] and row['discovery_arm']==arm and row['discovery_transport']==args.transport
        if mode == 'memory':
            assert row['peak_rss_bytes'] == (row['parent_peak_rss_bytes'] + row['discovery_child_peak_rss_bytes']
                                             + row['retained_executable_image_bytes'])
            assert row['child_memory_receipts'] and all(r['returncode'] == 0 for r in row['child_memory_receipts'])
            assert row['child_records_materialized_after_worker'] is True
            journal = row['child_record_journal']
            assert journal['storage'] == 'unlinked_temporary_file' and journal['records'] == args.samples + 22
            assert journal['bytes'] > 0 and len(journal['sha256']) == 64
        expected=1 if mode=='cold' else args.samples+21
        assert row['native_discovery_calls']==(expected if arm=='rust' else 0)
        assert all((clock['reaped_children_cpu_ns']==0 if arm=='rust' and args.transport=='ffi' else clock['reaped_children_cpu_ns']>0)
                   for clock in row['raw_clocks'])
        return row
    def ratio(case,cohort,metric,left,right):
        value=right/left if left else (1.0 if right==0 else None)
        if value is None or (value<1/1.05 if metric=='throughput_per_second' else value>1.05):
            failures.append(dict(case=case,cohort=cohort,metric=metric,git=left,rust=right,ratio=value))
        return dict(git=left,rust=right,ratio=value)
    checks={}
    for arm in ('git','rust'):
        target=out/f'check-{arm}.json'
        with tempfile.TemporaryDirectory(prefix='check-data-',dir=out) as data:
            run=subprocess.run([args.python,str(roots[arm]/'benchmarks/repository_index/native_discovery_measure.py'), '--native-package',str(native),
                '--output-dir',str(target),'--worker',arm,'--check','--transport',args.transport],cwd=roots[arm],
                env=dict(env,TOFU_DATA_DIR=data),capture_output=True,text=True,timeout=180)
        target.with_suffix('.log').write_text(run.stdout+run.stderr)
        assert run.returncode==0,run.stderr[-3000:]
        checks[arm]=json.loads(target.read_text())
        assert checks[arm]['sources']==variant_sources[arm] and checks[arm]['joined_cleanup']
        assert checks[arm]['foreign_root_refused_once'] and checks[arm]['unrelated_task_preserved']
    assert checks['git']['fixture_output_sha256']==checks['rust']['fixture_output_sha256']
    for case in args.cases:
        pairs=[]
        for pair in range(args.pairs):
            order=('git','rust') if pair%2==0 else ('rust','git')
            warm={arm:invoke(arm,case,'warm',pair) for arm in order}
            memory={arm:invoke(arm,case,'memory',pair) for arm in order}
            rows=list(warm.values())+list(memory.values())
            assert len({r['output_trace_sha256'] for r in rows})==1
            assert len({r['first_operation']['output_sha256'] for r in rows})==1
            values={}
            for metric in ('p50_ms','p95_ms','p99_ms','cpu_ms_per_operation','throughput_per_second',
                           'peak_rss_bytes','retained_bytes'):
                selected=memory if metric in ('peak_rss_bytes','retained_bytes') else warm
                values[metric]=ratio(case,f'warm-{pair}',metric,selected['git'][metric],selected['rust'][metric])
            pairs.append(values); print(case,'warm pair',pair,'joined',flush=True)
        cold={arm:[] for arm in ('git','rust')}
        for pair in range(args.cold_pairs):
            order=('git','rust') if pair%2==0 else ('rust','git')
            rows={arm:invoke(arm,case,'cold',pair) for arm in order}
            assert rows['git']['output_trace_sha256']==rows['rust']['output_trace_sha256']
            for arm in cold: cold[arm].append(rows[arm])
            if (pair+1)%10==0 or pair+1==args.cold_pairs: print(case,'cold pairs',pair+1,'joined',flush=True)
        cold_metrics={}
        for metric,field,quantile in [('p50_ms','p50_ms',None),('p95_ms','p50_ms',.95),
                                     ('p99_ms','p50_ms',.99),('cpu_ms_per_operation','cpu_ms_per_operation',None),
                                     ('peak_rss_bytes','peak_rss_bytes',None)]:
            values={arm:sorted(r[field] for r in cold[arm]) for arm in cold}
            aggregates={arm:aggregate_cold(v,metric,quantile) for arm,v in values.items()}
            cold_metrics[metric]=ratio(case,'cold',metric,aggregates['git'],aggregates['rust'])
        cases.append(dict(case=case,warm=pairs,cold=cold_metrics))
    improvements=all(all(pair['p50_ms']['ratio'] <= .9 for pair in case['warm']) for case in cases)
    complete=args.samples>=100 and args.pairs>=3 and args.cold_pairs>=100
    report=dict(discovery_transport=args.transport,scope='real transient repository publication on Git fixtures; same native ranking both arms',
        sources=source,variant_sources=variant_sources,source_roots={arm:str(root) for arm,root in roots.items()},
        comparison_includes_source_changes=roots['git']!=roots['rust'],package=receipt,checks=checks,executables=executables,hardware=hardware,affinity=affinity,
        environment=dict(python=sys.version,packages=packages,cpu_scope=worker.CPU_SCOPE),
        samples=args.samples,pairs=args.pairs,cold_pairs=args.cold_pairs,worker_processes=len(seen),
        cases=cases,regressions=failures,all_outputs_equal=True,all_workers_joined=True,
        verdict=('regression' if failures else 'pass' if improvements else 'insufficient_improvement') if complete else 'pilot_only',
        primary_improvement_at_least_ten_percent=improvements,
        memory_observer=args.memory_observer,
        memory_scope=('separate memory cohorts: '+MEMORY_SCOPE+'; cold RSS remains parent-only rusage'
                      if args.memory_observer == 'exit' else
                      'separate memory cohorts: parent peak RSS + maximum wait4 joined child peak RSS + retained sealed image bytes; conservative sum; wait4 includes pre-exec inherited RSS and shared pages may be counted twice; cold RSS remains parent-only'),
        lifecycle_limit=('cooperative Rust deadline cannot interrupt a blocked filesystem call' if args.transport=='ffi' else
                         'parent terminates and joins its owned child on timeout, cancellation or caller failure'),
        child_memory_observed=True,child_memory_admitted=False,production_discovery_selected=False,default_selection_admitted=False,whole_project_admitted=False)
    (out/'summary.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps(dict(verdict=report['verdict'],workers=len(seen),regressions=failures)),flush=True)


if __name__=='__main__': main()

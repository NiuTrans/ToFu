"""Installed Rust repository-domain integration admission with fresh cold workers.

CLI: --native-package PATH --output-dir OUT [--samples 100 --pairs 3
--cold-pairs 100]. Run a 2-sample/1-pair/1-cold-pair pilot first. Actual transient
registry/handler/scan/rank/render/publication and audit completion are measured;
HTTP, durable conversation and model execution remain outside this boundary.
Independent memory workers use the same cohort, with GC only outside their
memory observations. All source/package identities, failures and raw clocks are
retained. This driver cannot admit the other five system domains or defaults.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from native_domain_worker import CASES, CPU_SCOPE, digest, discovery_executables, fixture, measure, packages
from benchmarks.system_performance import compare


def file_receipt(path):
    path = Path(path)
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def package_receipt(native):
    package = native/'tofu_repo_kernel'
    receipt = json.loads((package/'build.json').read_text())
    owner = ROOT/'packages/tofu-repo-kernel'
    if receipt['source_sha256'] != file_receipt(owner/'rust/kernel.rs')['sha256']:
        raise ValueError('installed repository Rust source differs')
    for name in ('__init__.py', '_kernel.py', '__main__.py'):
        if (package/name).read_bytes() != (owner/'tofu_repo_kernel'/name).read_bytes():
            raise ValueError('installed repository Python source differs: '+name)
    if file_receipt(package/receipt['library'])['sha256'] != receipt['library_sha256']:
        raise ValueError('installed repository library differs')
    if 'discovery' in receipt:
        discovery = receipt['discovery']
        if discovery['abi_version'] != 1:
            raise ValueError('installed discovery ABI differs')
        if file_receipt(package/discovery['library'])['sha256'] != discovery['library_sha256']:
            raise ValueError('installed discovery library differs')
        if (package/'_discovery.py').read_bytes() != (owner/'tofu_repo_kernel/_discovery.py').read_bytes():
            raise ValueError('installed discovery Python source differs')
        names = ('Cargo.toml', 'Cargo.lock', 'lib.rs')
        if 'worker' in discovery:
            names += ('main.rs',)
            worker = discovery['worker']
            if worker['abi_version'] != 1 or file_receipt(package/worker['executable'])['sha256'] != worker['sha256']:
                raise ValueError('installed discovery executable differs')
            if (package/'_process_discovery.py').read_bytes() != (owner/'tofu_repo_kernel/_process_discovery.py').read_bytes():
                raise ValueError('installed discovery process adapter differs')
        for name in names:
            if discovery['sources'][name] != file_receipt(owner/'rust/discovery'/name)['sha256']:
                raise ValueError('installed discovery Rust source differs: '+name)
    return receipt


def source_receipts():
    names = [str(p.relative_to(ROOT)) for p in (ROOT/'lib/repo_map').glob('*.py')]
    names += ['benchmarks/repository_index/'+name for name in (
        'native_domain_measure.py', 'native_domain_worker.py', 'native_domain_check.py',
        'domain_fixtures.py', 'publication_port.py', 'record_journal.py')]
    names += ['lib/tasks_pkg/handlers/project.py', 'lib/tasks_pkg/executor/_registry.py',
              'lib/tasks_pkg/executor/_finalize.py', 'lib/tasks_pkg/manager/_registry.py',
              'lib/tasks_pkg/manager/_events.py', 'lib/tasks_pkg/manager/runtime.py',
              'lib/agent_core/task_runtime.py', 'lib/agent_core/execution_session.py',
              'lib/identity.py', 'lib/log.py', 'lib/project_mod/config.py', 'runtime_guards.py',
              'lib/project_dispatch/__init__.py', 'lib/project_dispatch/repository.py',
              'lib/project_dispatch/scope.py', 'lib/project_dispatch/admission.py',
              'contracts/project_dispatch_v1.schema.json',
              'benchmarks/system_performance.py', 'benchmarks/system_performance_contract.json',
              'benchmarks/process_memory_counters.py']
    names += ['packages/tofu-repo-kernel/'+name for name in (
        'rust/kernel.rs', 'tofu_repo_kernel/__init__.py', 'tofu_repo_kernel/_kernel.py',
        'tofu_repo_kernel/__main__.py', 'setup.py', 'pyproject.toml')]
    names += ['packages/tofu-repo-kernel/'+name for name in (
        'rust/discovery/lib.rs', 'rust/discovery/Cargo.toml', 'rust/discovery/Cargo.lock',
        'tofu_repo_kernel/_discovery.py', 'rust/discovery/main.rs',
        'tofu_repo_kernel/_process_discovery.py')]
    return {name:file_receipt(ROOT/name)['sha256'] for name in names}


def _assert_observation(row, variant, case, mode, samples, seen, environment, affinity, executables):
    assert (row['variant'],row['case'],row['mode']) == (variant,case,mode)
    identity=row['process_identity'];assert isinstance(identity,str) and identity and identity not in seen
    seen.add(identity)
    assert row['joined_cleanup'] and row['audit_queue_settled'] and row['weighted_ranking_required']
    assert row['native_backend_selected'] == (variant=='candidate')
    assert row['loaded_packages']==environment and row['affinity']==affinity
    assert row['discovery_executables']==executables
    assert row['profiler_enabled'] is False and row['tracemalloc_enabled'] is False
    assert row['cpu_scope'] == CPU_SCOPE
    assert row['memory_separate_process']==(mode=='memory')
    if mode == 'memory':
        assert row['clock_records_materialized_after_memory_counters'] is True
        journal = row['clock_record_journal']
        assert journal['storage'] == 'unlinked_temporary_file' and journal['records'] == samples + 1
        assert journal['bytes'] > 0 and len(journal['sha256']) == 64
    assert row['warmup_operations']==(0 if mode=='cold' else 20)
    assert len(row['raw_clocks'])==len(row['wall_samples_ms'])==len(row['cpu_samples_ms'])==(1 if mode=='cold' else samples)
    for raw,wall,cpu in zip(row['raw_clocks'],row['wall_samples_ms'],row['cpu_samples_ms']):
        assert raw['wall_ns']>0 and raw['cpu_ns']>0 and raw['wall_ns']/1e6==wall and raw['cpu_ns']/1e6==cpu
        assert type(raw['worker_cpu_ns']) is int and raw['worker_cpu_ns']>0
        assert type(raw['reaped_children_cpu_ns']) is int and raw['reaped_children_cpu_ns']>=0
        assert raw['cpu_ns']==raw['worker_cpu_ns']+raw['reaped_children_cpu_ns']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-package',type=Path)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--python',default=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--cold-pairs',type=int,default=100)
    parser.add_argument('--cases',nargs='+',choices=CASES,default=CASES)
    parser.add_argument('--worker',choices=('reference','candidate'))
    parser.add_argument('--mode',choices=('warm','memory','cold'))
    parser.add_argument('--case',choices=CASES)
    parser.add_argument('--worker-output',type=Path)
    args=parser.parse_args()
    if args.worker:
        assert args.mode and args.case and args.worker_output
        args.worker_output.write_text(json.dumps(measure(args.worker,args.case,args.mode,args.samples),indent=2,allow_nan=False)+'\n')
        return
    assert args.native_package and args.output_dir and min(args.samples,args.pairs,args.cold_pairs)>0
    assert len(set(args.cases))==len(args.cases)
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    native=args.native_package.resolve();receipt=package_receipt(native);sources=source_receipts()
    base_paths=[p for p in os.environ.get('PYTHONPATH','').split(os.pathsep) if p and Path(p).resolve()!=native]
    envs={v:dict(os.environ,PYTHONHASHSEED='0',PYTHONPATH=os.pathsep.join(([str(native)] if v=='candidate' else [])+base_paths)) for v in ('reference','candidate')}
    common_packages=packages();affinity=sorted(os.sched_getaffinity(0))
    executables=discovery_executables()
    from runtime_guards import probe_system_resources,resource_budget_manifest
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    environment=dict(python=sys.version,platform=platform.platform(),packages=common_packages,affinity=affinity,
                     python_hash_seed=0,cpu_scope=CPU_SCOPE,discovery_executables=executables)
    checks={};seen=set()
    def verify_sources():
        assert source_receipts()==sources
        assert package_receipt(native)==receipt
        assert discovery_executables()==executables
    for variant in envs:
        with tempfile.TemporaryDirectory(prefix='check-data-',dir=out) as data:
            run=subprocess.run([args.python,str(Path(__file__).with_name('native_domain_check.py'))],
                cwd=ROOT,env=dict(envs[variant],TOFU_DATA_DIR=data),capture_output=True,text=True,timeout=120)
        (out/f'check-{variant}.stderr.log').write_text(run.stderr)
        if run.returncode:raise RuntimeError('repository integration checks failed: '+run.stderr[-3000:])
        checks[variant]=json.loads(run.stdout)
        assert checks[variant]['native_backend_selected']==(variant=='candidate')
        assert checks[variant]['joined_cleanup'] and checks[variant]['weighted_ranking_required']
    assert checks['reference']['fixture_output_sha256']==checks['candidate']['fixture_output_sha256']
    reports={variant:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,
        native_package_receipt=receipt,check_receipts=checks,python_launcher=file_receipt(args.python),
        workloads=[],whole_project_admitted=False,default_selection_admitted=False) for variant in envs}
    def save():
        verify_sources()
        for variant,report in reports.items():
            (out/(variant+'.json')).write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        (out/'verdict.json').write_text(json.dumps(compare(reports['reference'],reports['candidate']),indent=2,allow_nan=False)+'\n')
    def invoke(variant,case,mode,label):
        verify_sources();target=out/(label+'.json');assert not target.exists()
        with tempfile.TemporaryDirectory(prefix='worker-data-',dir=out) as data:
            result=subprocess.run([args.python,__file__,'--worker',variant,'--case',case,'--mode',mode,
                '--samples',str(args.samples),'--worker-output',str(target)],cwd=ROOT,
                env=dict(envs[variant],TOFU_DATA_DIR=data),capture_output=True,text=True,timeout=300)
        (out/(label+'.stderr.log')).write_text(result.stderr)
        if result.returncode:raise RuntimeError('repository observation failed: '+result.stderr[-4000:])
        row=json.loads(target.read_text())
        _assert_observation(row,variant,case,mode,args.samples,seen,common_packages,affinity,executables)
        return row
    for case in args.cases:
        rows={}
        for variant in reports:
            row=dict(domain='repository',case=case,scope='domain_integration',correctness_passed=True,
                failure_and_cleanup_passed=True,identity=dict(source_sha256=digest(dict(sources=sources,variant=variant,artifact=receipt)),
                    runtime=environment,hardware=hardware,fixture_sha256=digest(fixture(case))),measurements=[],
                cold_start=dict(scope='first_operation_per_fresh_process',wall_samples_ms=[],cpu_samples_ms=[],
                    process_identities=[],profiler_enabled=False,tracemalloc_enabled=False,observations=[]))
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            order=('reference','candidate') if pair%2==0 else ('candidate','reference')
            observations={v:invoke(v,case,'warm',f'{case}-warm-{pair}-{v}') for v in order}
            memory={v:invoke(v,case,'memory',f'{case}-memory-{pair}-{v}') for v in order}
            assert observations['reference']['output_trace_sha256']==observations['candidate']['output_trace_sha256']
            assert observations['reference']['first_operation']['output_sha256']==observations['candidate']['first_operation']['output_sha256']
            for variant in order:
                timed=observations[variant];resident=memory[variant]
                assert timed['output_trace_sha256']==resident['output_trace_sha256']
                timed.update({k:resident[k] for k in ('peak_rss_bytes','retained_bytes','retention_baseline_rss_bytes','post_cleanup_rss_bytes')})
                timed.update(memory_process_identity=resident['process_identity'],memory_separate_process=True,
                    memory_observation=file_receipt(out/f'{case}-memory-{pair}-{variant}.json'))
                rows[variant]['measurements'].append(timed)
            save();print(case,'warm pair',pair,'joined',flush=True)
        for pair in range(args.cold_pairs):
            order=('reference','candidate') if pair%2==0 else ('candidate','reference')
            observations={v:invoke(v,case,'cold',f'{case}-cold-{pair}-{v}') for v in order}
            assert observations['reference']['output_trace_sha256']==observations['candidate']['output_trace_sha256']
            for variant in order:
                observed=observations[variant];cold=rows[variant]['cold_start']
                for key in ('wall_samples_ms','cpu_samples_ms'):cold[key].extend(observed[key])
                cold['process_identities'].append(observed['process_identity'])
                cold['observations'].append(file_receipt(out/f'{case}-cold-{pair}-{variant}.json'))
            save()
            if (pair+1)%10==0 or pair+1==args.cold_pairs:print(case,'cold pairs',pair+1,'joined',flush=True)
        print(case,'complete',flush=True)
    print(json.dumps(compare(reports['reference'],reports['candidate']),sort_keys=True),flush=True)


if __name__=='__main__':main()

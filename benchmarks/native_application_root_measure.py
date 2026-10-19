"""Independent warm/cold cohorts for priced/counted ordinary HTTP ROOT.

Owns disposable application worker processes, alternating arms and raw clocks.
Memory is measured separately by native_root_memory --entry application. This
collector reuses existing admission thresholds; missing memory stays incomplete.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks.native_application_root import compare,comparable_work,digest
from benchmarks.native_agent_root_measure import summarize
from benchmarks.native_application_authority import authority_witness,check_authority_files,check_authority,check_worker_reuse,check_completion_observer,check_usage_summary,check_terminal_reference,check_image_preparation,check_token_library,APPLICATION_BACKENDS,COMPLETION_OBSERVERS


def timing_summary(report):
    signatures=set();image_identities=set()
    if report.get("image_source", "archive") not in ("archive", "installed_executable"):
        raise ValueError("HTTP cohort executable admission source invalid")
    for mode in ('warm','cold'):
        rows=report[mode]
        for row in rows:
            if row.get('image_source','archive') != report.get('image_source','archive'):
                raise ValueError('HTTP cohort executable admission source differs')
            executable_pin=row.get('executable_sha256')
            if report.get('image_source')=='installed_executable' and (not isinstance(executable_pin,str) or len(executable_pin)!=64
                    or any(ch not in '0123456789abcdef' for ch in executable_pin)):
                raise ValueError('HTTP cohort installed executable pin invalid')
            image_identities.add((row.get('image_source','archive'), executable_pin))
            if len(image_identities)>1:
                raise ValueError('HTTP cohort executable pin differs across workers')
            if 'archive_sha256' in report and row.get('archive_sha256') != report['archive_sha256']:
                raise ValueError('HTTP cohort archive pin differs')
            check_token_library(row,report.get('native_token_count',False))
            if row.get('token_library_sha256') != report.get('token_library_sha256'):
                raise ValueError('HTTP cohort token library pin differs')
            check_completion_observer(row,report.get('completion_observer','poll'))
            check_usage_summary(row,report.get('native_usage_summary',False))
            check_terminal_reference(row,report.get('native_terminal_reference',False))
            check_image_preparation(row,report.get('native_image_preparation',False))
            if (row.get('diagnostic_stages_enabled') or row.get('diagnostic_stages')
                    or row.get('diagnostic_storage_metrics')):
                raise ValueError('stage diagnostics cannot grant timing admission')
            if 'worker_reuse' in report:check_worker_reuse(row,report['worker_reuse'])
            if row['work_sha256']!=digest(comparable_work(row)):
                raise ValueError('HTTP cohort work signature differs')
            if report.get('storage_authority') is not None:check_authority(row,report['storage_authority'])
            signatures.add(row['work_sha256'])
        for index in range(0,len(rows)-1,2):
            pair=sorted(rows[index:index+2],key=lambda row:0 if row['arm']=='python' else 1)
            if compare(pair)['measurement_permitted'] is not True:
                raise ValueError('HTTP cohort arms perform different paid work')
    if len(signatures)>1:raise ValueError('HTTP cohort fixture changed across workers')
    return summarize(dict(warm=report['warm'],cold=report['cold'],memory=[],backend=report.get('backend','sqlite'),scenario='text'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-executable',type=Path)
    parser.add_argument('--installed-token-library',type=Path)
    parser.add_argument('--backend',choices=APPLICATION_BACKENDS,default='sqlite')
    parser.add_argument('--storage-engine',type=Path)
    parser.add_argument('--reuse-native-workers',action='store_true')
    parser.add_argument('--prepare-native-image',action='store_true')
    parser.add_argument('--completion-observer',choices=COMPLETION_OBSERVERS,default='poll')
    parser.add_argument('--native-usage-summary',action='store_true')
    parser.add_argument('--native-terminal-reference',action='store_true')
    parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--warmups',type=int,default=20);parser.add_argument('--cold-pairs',type=int,default=100)
    parser.add_argument('--python-command',type=json.loads,default=[sys.executable])
    args=parser.parse_args()
    if args.pairs<=0 or args.samples<=0 or args.warmups<0 or args.cold_pairs<0:raise ValueError('invalid cohort counts')
    witness=authority_witness(args.backend,args.storage_engine,ROOT)
    names=['lib/storage/runtime.py','lib/storage/supervisor.py','tofu_db/embedded.py','serverctl_pkg/native_engine.py','lib/tasks_pkg/invocation.py','lib/tasks_pkg/native_gateway.py','lib/agent_core/native_token_count.py','lib/token_counter/native.py','lib/token_counter/tiktoken_counter.py','lib/agent_core/native_image.py','lib/agent_core/native_artifact.py','benchmarks/native_application_authority.py','benchmarks/native_application_root_measure.py','benchmarks/native_application_root.py',
        'benchmarks/native_agent_root.py','benchmarks/native_agent_root_measure.py',
        'lib/agent_core/native_worker_pool.py','lib/agent_core/native_process.py','lib/tasks_pkg/native_host.py',
        'lib/tasks_pkg/native_application.py','lib/tasks_pkg/native_invocation.py','lib/tasks_pkg/native_execution.py',
        'lib/tasks_pkg/native_prepared.py','lib/tasks_pkg/native_pricing.py','lib/tasks_pkg/native_cost_projection.py',
        'lib/tasks_pkg/spawn.py','lib/tasks_pkg/manager/_persist.py','lib/tasks_pkg/orchestrator/_post_commit.py','lib/conversation_sync/task_start.py',
        'benchmarks/native_completion.py','lib/tasks_pkg/native_terminal.py',
        'contracts/native_agent_stdio_v1.schema.json','packages/tofu-runtime/src/cli_terminal.rs',
        'packages/tofu-runtime/src/usage_aggregate.rs','packages/tofu-runtime/src/cli_agent.rs']
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report=dict(scope=__doc__,sources=sources,archive_sha256=hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        image_source='installed_executable' if args.installed_executable else 'archive',
        native_token_count=args.installed_token_library is not None,
        token_library_sha256=hashlib.sha256(args.installed_token_library.read_bytes()).hexdigest() if args.installed_token_library else None,
        entry='priced_counted_quart_application',native_terminal_reference=args.native_terminal_reference,native_image_preparation=args.prepare_native_image,native_usage_summary=args.native_usage_summary,completion_observer=args.completion_observer,worker_reuse=args.reuse_native_workers,backend=args.backend,storage_authority=witness,scenario='text',warm=[],cold=[],memory=[],
        limits=['In-process TESTING Quart ingress, fixed loopback model/prices, explicit disposable authority, text and explicit budget',
            'Common application/store/provider startup and verification readback excluded from operation clocks',
            'No paid wallet debit, socket/TLS ingress, other backends/tools or concurrent scaling',
            'Separate stopped-VM memory cohort required; timing cannot admit memory/defaults'])
    with tempfile.TemporaryDirectory(prefix='tofu-http-cohorts-') as temporary:
        for mode,pairs in (('warm',args.pairs),('cold',args.cold_pairs)):
            for index in range(pairs):
                for arm in ('python','native') if index%2==0 else ('native','python'):
                    for name,sha in sources.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha,name
                    check_authority_files(args.storage_engine,witness)
                    output=Path(temporary)/'worker.json'
                    command=args.python_command+[str(ROOT/'benchmarks/native_application_root.py'),'--worker',
                        '--archive',str(args.archive.resolve()),'--output',str(output),'--arm',arm,'--mode',mode,
                        '--samples',str(args.samples),'--warmups',str(args.warmups),'--backend',args.backend]
                    command+=['--completion-observer',args.completion_observer]
                    if args.native_usage_summary:command+=['--native-usage-summary']
                    if args.native_terminal_reference:command+=['--native-terminal-reference']
                    if args.reuse_native_workers:command+=['--reuse-native-workers']
                    if args.prepare_native_image:command+=['--prepare-native-image']
                    if args.installed_executable:command+=['--installed-executable',str(args.installed_executable.resolve())]
                    if args.installed_token_library:command+=['--installed-token-library',str(args.installed_token_library.resolve())]
                    if args.storage_engine:command+=['--storage-engine',str(args.storage_engine.resolve())]
                    result=subprocess.run(command,capture_output=True,text=True)
                    if result.returncode:raise RuntimeError('HTTP cohort worker failed: '+result.stdout[-1000:]+result.stderr[-5000:])
                    report[mode].append(json.loads(output.read_text()))
                    report['summary']=timing_summary(report)
                    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
                print(mode,index+1,'joined',flush=True)
    print(json.dumps(report['summary'],sort_keys=True),flush=True)


if __name__=='__main__':main()

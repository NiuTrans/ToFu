"""Controlled Evolution repair and private native storage-crash benchmark.

Entry: run_proof / CLI. Dependencies: runtime authorities and test provider seams.
No live application storage, real model calls or executor replay is involved.
TemporaryDirectory owns all storage; finally releases every task and engine.
The measurements prove the named controlled scope, never research quality.
"""
from pathlib import Path as Path
import argparse as argparse
import sys as sys
# Repository checkout and test dependencies are required for controlled provider seams.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json as json
import tempfile as tempfile
import time as time
import hashlib as hashlib
import os as os
from unittest.mock import patch as patch
from lib.storage import StorageRuntime as StorageRuntime,StorageSupervisor as StorageSupervisor
from lib.storage.service import install_runtime_for_test as install_runtime_for_test
from lib.evolution import runs as runs,stage_outcomes as stage_outcomes
from lib.tasks_pkg.manager import discard_task as discard_task
from lib.agent_core.execution_session import ExecutionPhase as ExecutionPhase
from lib.tools.result_value import capture_result as capture_result
from tests.evolution.test_evolution_scientific_observations import protocol as protocol
from tests.support.research_observations import observation_payload as observation_payload
from tests.evolution.test_evolution_runs import finish as finish,review as review,OWNER as OWNER
from tests.evolution.test_evolution_source_tools import delivered as delivered



def run_proof(attempts=48,crash_after=(16,40),source_repetitions=8000):
    if (type(attempts) is not int or attempts<1 or type(source_repetitions) is not int or source_repetitions<1
            or not isinstance(crash_after,(tuple,list)) or any(type(index) is not int or not 1<=index<attempts for index in crash_after)
            or list(crash_after)!=sorted(set(crash_after))):
        raise ValueError('invalid controlled benchmark dimensions or crash positions')
    tasks=[];measurements=[];crashes=[];originals=[]
    with patch.dict(os.environ, {'TOFUDB_OWNER_ID': str(OWNER)}), tempfile.TemporaryDirectory(prefix='tofu-long-horizon-private-') as directory:
        supervisor=StorageSupervisor(project_root=Path(directory),backend='tofudb',startup_timeout=60)
        runtime=StorageRuntime(supervisor,auto_restart=False)
        install_runtime_for_test(runtime)
        try:
            with patch('lib.evolution.runs.resolve_run_model',side_effect=lambda requested,**kwargs:requested),patch('lib.tasks_pkg.spawn.spawn_task',side_effect=tasks.append),patch('lib.evolution.runs.default_task_loader',side_effect=lambda tid,user_id:next((task for task in tasks if task['id']==tid and task['_userId']==user_id),None)):
                runtime.start();frozen=protocol()
                initial=runs.submit_run({'objective':'Controlled long-horizon execution and evidence stability','scientific_protocol':frozen},user_id=OWNER)
                for index in range(attempts):
                    started=time.perf_counter();executor=tasks[-1]
                    payload=observation_payload(frozen,'metrics','supported');payload['attempt_index']=index
                    payload['padding']='完整原文 🧪\n'*source_repetitions
                    raw='$ measure\n'+json.dumps(payload,ensure_ascii=False)+'\n[exit code: 0]'
                    executor['_evolutionScientificCommandCapture'](executor,'run_command',f'measure-{index}',{},capture_result(raw))
                    executor['messages']=[message for message in executor['messages'] if message.get('role')!='tool']
                    current=finish((runs,None,tasks),executor,f'Revision {index} measured; await independent assessment.')
                    assert current['status']=='reviewing'
                    ref=current['tool_evidence_source'];reviewer=tasks[-1];offset=0;pieces=[]
                    while True:
                        page=delivered(reviewer,{'action':'read','run_id':current['run_id'],'task_id':ref['task_id'],'source_kind':'tool_evidence','source_sha256':ref['source_sha256'],'offset':offset})
                        pieces.append(page['content'])
                        if page['next_offset'] is None:break
                        offset=page['next_offset']
                    text=''.join(pieces);assert any(json.loads(line).get('content')==raw for line in text.splitlines())
                    originals.append((executor['id'],hashlib.sha256(text.encode()).hexdigest()))
                    current=finish((runs,None,tasks),reviewer,review('pass' if index==attempts-1 else 'fail'))
                    assert current['status']==('completed' if index==attempts-1 else 'executing')
                    assert current['attempt']==index+1+(index!=attempts-1)
                    assert current['objective']==initial['objective'] and current['scientific_protocol']==frozen
                    from lib.evolution import records
                    stored=records.get_record(records.NAMESPACE_RUNS,runs._key(current['run_id'],OWNER))
                    measurements.append({'stored_run_header_bytes':len(json.dumps(stored,ensure_ascii=False).encode()),'stored_receipts':len(stored['receipts']),'archived_receipts':(stored.get('receipt_archive') or {}).get('count',0),'attempt':index+1,'elapsed_ms':(time.perf_counter()-started)*1000,'run_header_bytes':len(json.dumps(current,ensure_ascii=False).encode()),'receipts':len(current['receipts'])})
                    if index+1 in crash_after:
                        before=runs.get_run(current['run_id'],user_id=OWNER)
                        process=supervisor._process;assert process is not None and process.poll() is None
                        process.kill();exitcode=process.wait(timeout=10);assert exitcode!=0
                        runtime.stop();runtime.start()
                        assert runs.get_run(current['run_id'],user_id=OWNER)==before
                        crashes.append({'after_attempt':index+1,'exit_code':exitcode,'run_readback':'exact'})
                    if (index+1)%8==0:print(json.dumps({'progress_attempts':index+1,**measurements[-1]}),flush=True)
                for task_id,digest in originals:
                    offset=0;sha=hashlib.sha256()
                    while True:
                        page=stage_outcomes.page(current['run_id'],task_id,user_id=OWNER,source_kind='tool_evidence',offset=offset)
                        sha.update(page['content'].encode())
                        if page['next_offset'] is None:break
                        offset=page['next_offset']
                    assert sha.hexdigest()==digest
                return {'schema_version':1,'scope':'controlled_native_evolution_repair_and_storage_crash','status':'passed','attempts':attempts,'receipts':len(current['receipts']),'verified_originals':len(originals),'storage_crashes':crashes,'requested_crashes':list(crash_after),'measurements':measurements,'actual_model_calls':0,'executor_replay_tested':False,'research_quality_tested':False}
        finally:
            for task in tasks:
                task['_executionSession'].settle(ExecutionPhase.CANCELLED,cause='proof_cleanup')
                discard_task(task['id'],'')
            runtime.stop();install_runtime_for_test(None)

def validate_report(report):
    """Reject incomplete benchmark coverage before reporting controlled success."""
    if not isinstance(report,dict) or report.get('schema_version')!=1 or report.get('status')!='passed':return False
    attempts=report.get('attempts')
    if type(attempts) is not int or attempts<1:return False
    if (report.get('scope')!='controlled_native_evolution_repair_and_storage_crash'
            or report.get('actual_model_calls')!=0 or report.get('executor_replay_tested') is not False
            or report.get('research_quality_tested') is not False
            or report.get('receipts')!=attempts*2 or report.get('verified_originals')!=attempts):return False
    measurements=report.get('measurements');requested=report.get('requested_crashes');crashes=report.get('storage_crashes')
    if not isinstance(measurements,list) or len(measurements)!=attempts:return False
    if not isinstance(requested,list) or any(type(index) is not int or not 1<=index<attempts for index in requested):return False
    if requested!=sorted(set(requested)):return False
    if not isinstance(crashes,list) or any(not isinstance(row,dict) for row in crashes):return False
    if [row.get('after_attempt') for row in crashes]!=requested:return False
    if any(type(row.get('exit_code')) is not int or row['exit_code']>=0 or row.get('run_readback')!='exact' for row in crashes):return False
    import math
    return all(isinstance(row,dict) and row.get('attempt')==index and row.get('receipts')==index*2
        and type(row.get('stored_run_header_bytes')) is int and row['stored_run_header_bytes']>0
        and type(row.get('stored_receipts')) is int and 0<=row['stored_receipts']<=64
        and type(row.get('archived_receipts')) is int and row['archived_receipts']>=0 and row['archived_receipts']%32==0
        and row['stored_receipts']+row['archived_receipts']==row['receipts']
        and type(row.get('run_header_bytes')) is int and row['run_header_bytes']>0
        and type(row.get('elapsed_ms')) in (int,float) and math.isfinite(row['elapsed_ms']) and row['elapsed_ms']>0
        for index,row in enumerate(measurements,1))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempts',type=int,default=48)
    parser.add_argument('--crash-after',type=int,nargs='*',default=[16,40])
    parser.add_argument('--source-repetitions',type=int,default=8000)
    args=parser.parse_args()
    if args.attempts<1 or args.source_repetitions<1 or len(set(args.crash_after))!=len(args.crash_after) or any(not 1<=index<args.attempts for index in args.crash_after):
        parser.error('attempts/source-repetitions must be positive and crash positions unique before the last attempt')
    report=run_proof(args.attempts,args.crash_after,args.source_repetitions)
    if not validate_report(report):raise RuntimeError('benchmark coverage is incomplete')
    print(json.dumps(report,ensure_ascii=False),flush=True)

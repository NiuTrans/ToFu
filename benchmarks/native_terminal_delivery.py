"""Real SDK/transient terminal delivery with bounded long history and ACK retry.

No storage authority, billed model, mocked persistence or default admission.
Each operation owns a native process; clocks include launch and joined exit.
Provider-write through joined exit is a separate terminal-tail clock.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def validate_observation(row):
    """Reject partial work/cleanup before comparing clocks."""
    if (row.get('scope')!='native SDK transient terminal delivery'
            or row.get('joined_cleanup') is not True
            or row.get('provider_calls')!=row.get('operations')
            or row.get('terminal_receipts')!=row.get('operations')
            or row.get('receiver_calls')!=row.get('operations')*(2 if row.get('retry') else 1)
            or row.get('exact_history_verified') is not True
            or len(row.get('raw',[]))!=row.get('samples')):
        raise ValueError('incomplete native terminal delivery evidence')
    selected=row.get('completion_reference',False)
    count=row.get('reference_frames',0)
    if (type(selected) is not bool or type(count) is not int
            or count!=(row['operations'] if selected else 0)):
        raise ValueError('invalid terminal reference witness')
    retry_selected=row.get('retry_reference',False)
    retry_count=row.get('retry_reference_frames',0)
    wanted=row['operations'] if retry_selected and row.get('retry') is True else 0
    if type(retry_selected) is not bool or type(retry_count) is not int or retry_count!=wanted:
        raise ValueError('invalid terminal retry reference witness')
    for sample in row['raw']:
        if any(type(sample.get(key)) is not int or sample[key]<=0
               for key in ('wall_ns','tail_wall_ns','cpu_ns')):
            raise ValueError('invalid native terminal delivery clock')


def compare_reference_pair(baseline,candidate):
    return _compare_reference_pair(baseline,candidate,"completion_reference")


def compare_retry_reference_pair(baseline,candidate):
    if baseline.get("retry") is not True or candidate.get("retry") is not True:
        raise ValueError("retry reference comparison requires actual ACK retries")
    return _compare_reference_pair(baseline,candidate,"retry_reference")


def _compare_reference_pair(baseline,candidate,selection):
    """Compare the same binary/work with explicit off/on physical witnesses."""
    import statistics
    from benchmarks.native_agent_root_measure import quantiles
    for row in (baseline,candidate):validate_observation(row)
    if (baseline.get(selection,False) is not False
            or candidate.get(selection) is not True
            or not baseline.get('process_identity')
            or not candidate.get('process_identity')
            or baseline.get('process_identity')==candidate.get('process_identity')
            or any(baseline.get(key) is None or baseline.get(key)!=candidate.get(key)
                for key in ('history_bytes','retry','samples','warmups','work_sha256','binary_sha256'))):
        raise ValueError('terminal reference pair selection, identity or work differs')
    other='retry_reference' if selection=='completion_reference' else 'completion_reference'
    if baseline.get(other,False)!=candidate.get(other,False):
        raise ValueError('other terminal reference policy differs')
    ratios={}
    for key in ('wall_ns','tail_wall_ns'):
        before=quantiles([sample[key] for sample in baseline['raw']])
        after=quantiles([sample[key] for sample in candidate['raw']])
        ratios[key]={name:after[name]/before[name] for name in ('p50','p95','p99')}
    ratios['cpu_mean']=statistics.mean(sample['cpu_ns'] for sample in candidate['raw'])/statistics.mean(sample['cpu_ns'] for sample in baseline['raw'])
    return dict(scope='same native binary transient '+selection+' off/on',ratios=ratios,
        performance_admitted=False,default_selection_admitted=False)


async def trial(args,directory):
    from lib.agent_core.native_process import Budget,NativeProcessClient,_TerminalControl
    from lib.agent_core.terminal_delivery import bind_terminal_delivery
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy,project_native_terminal
    binary=args.binary.resolve(strict=True)
    actual=hashlib.sha256(binary.read_bytes()).hexdigest()
    if actual!=args.sha256:raise ValueError('native binary changed')
    history='x'*args.history_bytes
    calls=0;receivers=0;terminal_receipts=0;provider_write_ns=0
    expected_usage={'prompt_tokens':3,'completion_tokens':2}
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal calls,provider_write_ns
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert request['messages']==[{'role':'user','content':history}]
            calls+=1
            frame={'choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}],
                   'usage':expected_usage}
            body=('data: '+json.dumps(frame)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(body)));self.end_headers()
            provider_write_ns=time.perf_counter_ns()
            self.wfile.write(body)
        def log_message(self,*_args):pass
    server=HTTPServer(('127.0.0.1',0),Provider)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    owner=PrincipalContext.user(subject_id='terminal-measure',owner_user_id=7)
    cap=6*args.history_bytes+(1<<20)
    config=dict(version='tofu.native-agent-stdio/v1',type='configure',owner_user_id=7,
        task_id='task',epoch='epoch',endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},
        template={'model':'fixture'},messages=[{'role':'user','content':history}],tools={},
        event_bytes=cap,output_bytes=65536,tool_calls=8,request_bytes=cap,rounds=3,
        retained_bytes=cap,feedback_bytes=4096,tool_frame_bytes=8192,deadline_ms=10000,
        policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,
            request_headers_ms=5000,pool_wait_ms=1000,stream_idle_ms=1000,
            drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16),
        terminal_persistence=dict(operation_id='commit',ack_ms=5000,attempts=2,frame_bytes=cap))
    selected=getattr(args,'completion_reference',False)
    if selected:config['terminal_persistence']['completion_reference']=True
    retry_selected=getattr(args,'retry_reference',False)
    if retry_selected:config['terminal_persistence']['retry_reference']=True
    retry_reference_frames=0
    original_handle=_TerminalControl.handle
    async def observe_handle(control,frame,process):
        nonlocal retry_reference_frames
        result=await original_handle(control,frame,process)
        if frame.get('type')=='terminal_retry':retry_reference_frames+=1
        return result
    reference_frames=0
    original_reference=_TerminalControl.resolve_reference
    def observe_reference(control,frame):
        nonlocal reference_frames
        result=original_reference(control,frame)
        reference_frames+=1
        return result
    raw=[]
    def cpu_clock():
        child=resource.getrusage(resource.RUSAGE_CHILDREN)
        return time.process_time_ns()+int((child.ru_utime+child.ru_stime)*1e9)
    from unittest.mock import patch
    try:
        with patch.object(_TerminalControl,"handle",observe_handle), patch.object(_TerminalControl,"resolve_reference",observe_reference), ThreadPoolExecutor(max_workers=1) as executor:
            for index in range(args.warmups+args.samples):
                task=dict(id='task',_userId=7,status='running',_transientRuntime=True)
                receiver_count=0
                def receive(value):
                    nonlocal receivers,receiver_count
                    receivers+=1;receiver_count+=1
                    assert value['content']=='answer' and value['usage']==expected_usage
                    if args.retry and receiver_count==1:raise OSError('lost receiver ACK')
                bind_terminal_delivery(task,receive)
                policy=NativeTaskTerminalPolicy(principal=owner,task=task,operation_id='commit',
                    executor=executor,project=project_native_terminal,candidate_bytes=cap,transient=True)
                client=NativeProcessClient(binary=binary,sha256=actual,protocol_version=config['version'],
                    principal=owner,budget=Budget(1,binary.stat().st_size,cap,cap,5))
                start_cpu,start_wall=cpu_clock(),time.perf_counter_ns()
                try:
                    event=await client.run(config,callbacks={},working_directory=directory,terminal_policy=policy)
                    assert client.active_runs==0
                finally:await client.close()
                end_wall,end_cpu=time.perf_counter_ns(),cpu_clock()
                assert policy.accepted and event['status']=='done'
                terminal_receipts+=1
                assert event['result']['messages']==[{'role':'user','content':history},{'role':'assistant','content':'answer'}]
                assert task['messages']==event['result']['messages'] and task['usage']==expected_usage
                sample=dict(wall_ns=end_wall-start_wall,tail_wall_ns=end_wall-provider_write_ns,cpu_ns=end_cpu-start_cpu)
                if index>=args.warmups:raw.append(sample)
    finally:
        server.shutdown();server.server_close();thread.join()
    row=dict(scope='native SDK transient terminal delivery',binary_sha256=actual,
        history_bytes=args.history_bytes,retry=args.retry,retry_reference=retry_selected,retry_reference_frames=retry_reference_frames,completion_reference=selected,reference_frames=reference_frames,samples=args.samples,warmups=args.warmups,
        operations=args.samples+args.warmups,provider_calls=calls,receiver_calls=receivers,
        terminal_receipts=terminal_receipts,exact_history_verified=True,joined_cleanup=True,
        process_identity=f'{os.getpid()}:{time.monotonic_ns()}',raw=raw,
        work_sha256=hashlib.sha256(json.dumps(dict(history_bytes=args.history_bytes,retry=args.retry,
            content='answer',usage=expected_usage),sort_keys=True).encode()).hexdigest(),
        performance_admitted=False,default_selection_admitted=False,
        limits=['Transient delivery only; no Quart/durable Turn/storage/wallet qualification.',
                'Tail starts before provider writes final bytes; includes transport/settlement/projection/ACK/exit.',
                'CPU includes parent process threads and waited native children; no resident-memory/scaling gate.'])
    validate_observation(row)
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--sha256',required=True)
    parser.add_argument('--history-bytes',type=int,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--warmups',type=int,default=20)
    parser.add_argument('--retry',action='store_true')
    parser.add_argument('--completion-reference',action='store_true')
    parser.add_argument('--retry-reference',action='store_true');args=parser.parse_args()
    if not 0<args.history_bytes<=16<<20 or not 0<args.samples<=10000 or not 0<=args.warmups<=10000:
        raise ValueError('invalid bounded cohort')
    with tempfile.TemporaryDirectory(prefix='tofu-terminal-sdk-measure-') as temporary:
        directory=Path(temporary)
        for key in list(os.environ):
            if key.startswith(('TOFU_STORAGE_','TOFU_SUPERVISOR_','TOFUDB_','TOFU_DB_','_TOFU_')):
                os.environ.pop(key,None)
        os.environ['TOFU_RUNTIME_DIR']=str(directory);os.environ['TOFU_DATA_DIR']=str(directory)
        row=asyncio.run(trial(args,directory))
    args.output.write_text(json.dumps(row,indent=2)+'\n')


if __name__=='__main__':main()

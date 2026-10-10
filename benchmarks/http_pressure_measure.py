"""Real TCP/TLS slow-observer disconnect and abort/close quiescence samples.

Slow-consumer setup holds an unread large stream for 50ms and witnesses bounded
production. Timing starts on transport release. Cancellation times explicit
abort plus transport close to observer quiescence. Setup/hold are outside timing;
diagnostic polling contributes wall/CPU. Synthetic compute is not full-agent
inference. Existing same-connection abort/H2-reset lifecycle checks remain gates.
"""
import argparse as argparse
import asyncio as asyncio
import hashlib as hashlib
import inspect as inspect
import json as json
import os as os
from pathlib import Path as Path
import sys as sys
import tempfile as tempfile
import time as time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks import http_runtime as http_runtime
from benchmarks.http_cpu_clock import OwnedProcessCpuClock as OwnedProcessCpuClock
from benchmarks.http_frame_coalescing_lifecycle import serve_child as lifecycle_server
from benchmarks.http_frame_coalescing_measure import checked_trial_source as checked_trial_source,checked_shutdown as checked_shutdown


DIAGNOSIS_ENABLED=False
FAILURE_LOG=None


def diagnose_h2():
    import hypercorn.protocol as protocol
    from hypercorn.events import Closed
    original=protocol.H2Protocol
    class ObservedH2(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            receive=self.connection.receive_data
            def observed(data):
                try:return receive(data)
                except Exception as error:
                    print('H2_RX_ERROR',id(self),self.keep_alive_requests,type(error).__name__,str(error),flush=True)
                    raise
            self.connection.receive_data=observed
        async def handle(self,event):
            if isinstance(event,Closed):print('H2_CLOSED',id(self),self.keep_alive_requests,flush=True)
            return await super().handle(event)
    protocol.H2Protocol=ObservedH2


async def requests(pid,origin,protocol,case,samples,concurrency,verify):
    import httpx
    clock=OwnedProcessCpuClock(pid);raw=[];cpu_raw=[];witnesses=[]
    options=dict(base_url=origin,http2=protocol=='h2',verify=verify,trust_env=False,
                 headers={'Authorization':f'Bearer {http_runtime.TOKEN}'},timeout=10)
    async def one(index):
        async with httpx.AsyncClient(**options) as client, httpx.AsyncClient(**options) as observer:
            created=await client.post('/api/v1/agent/run',json={'messages':[{'role':'user','content':'synthetic'}]},headers={'Prefer':'respond-async'})
            assert created.status_code==202
            task_id=created.json()['task_id']
            async with client.stream('GET',f'/api/v1/tasks/{task_id}/stream') as response:
                assert response.status_code==200 and response.http_version==('HTTP/2' if protocol=='h2' else 'HTTP/1.1')
                lines=response.aiter_lines()
                async for line in lines:
                    if line.startswith('data: '):assert json.loads(line[6:])['seq']==0;break
                assert (await observer.get('/health/ready')).status_code==200
                if case=='slow_consumer':
                    await asyncio.sleep(.05)
                    held=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                    assert held['active']==1 and 0<held['produced']<512,held
                start_cpu=clock.read_ns();start=time.perf_counter_ns()
                if case=='cancellation':
                    aborted=await observer.post(f'/api/v1/tasks/{task_id}/abort')
                    assert aborted.status_code==200 and aborted.json()['abort_requested']
                await client.aclose()
                deadline=time.monotonic()+2
                while True:
                    state=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                    if state['active']==0:break
                    assert time.monotonic()<deadline,state
                    await asyncio.sleep(0)
                wall=(time.perf_counter_ns()-start)/1e6;cpu=(clock.read_ns()-start_cpu)/1e6
                expected='running' if case=='slow_consumer' else 'aborted'
                assert state['closed']==1 and state['status']==expected,state
                assert wall>0 and cpu>0
                if index>=0:
                    raw.append(wall);cpu_raw.append(cpu);witnesses.append(dict(active=0,closed=1,status=expected))
                if case=='slow_consumer':
                    aborted=await observer.post(f'/api/v1/tasks/{task_id}/abort')
                    assert aborted.status_code==200 and aborted.json()['abort_requested']
    for _ in range(20):await one(-1)
    for index in range(samples):await one(index)
    return raw,20,cpu_raw,witnesses


def trial(variant,protocol,case,samples,directory,memory=False):
    source=checked_trial_source(http_runtime.trial)
    old='raw, warmup_requests = asyncio.run(requests(origin, protocol, case, samples, concurrency, verify))'
    assert source.count(old)==1
    source=source.replace(old,'raw, warmup_requests, cpu_raw, witnesses = asyncio.run(pressure_requests(process.pid, origin, protocol, case, samples, concurrency, verify))')
    old="'raw_wall_ms': raw if not memory else [],";assert source.count(old)==1
    source=source.replace(old,"'raw_wall_ms': raw if not memory else [], 'raw_cpu_ms':cpu_raw, 'terminal_witnesses':witnesses,")
    old="    log = directory /";assert source.count(old)==1
    source=source.replace(old,"    argv.extend(['--workload',case])\n    if diagnosis_enabled: argv.append('--diagnose-h2')\n"+old)
    namespace=dict(http_runtime.trial.__globals__,__file__=__file__,pressure_requests=requests,_checked_shutdown=checked_shutdown,diagnosis_enabled=DIAGNOSIS_ENABLED)
    exec(compile(source,'<owned-http-pressure-trial>','exec'),namespace)
    try:
        result=namespace['trial'](variant,protocol,case,samples,1,directory,memory)
    except BaseException as primary:
        logs=sorted(directory.glob(f'{variant}-{protocol}-{case}-*.log'))
        if logs:
            evidence=FAILURE_LOG if FAILURE_LOG is not None else directory/'server-failure.log'
            evidence.write_bytes(logs[-1].read_bytes())
            primary.add_note('Owned server log retained: '+str(evidence))
        raise
    result.update(shutdown_exit_code=0,shutdown_joined=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',action='store_true');parser.add_argument('--loop',choices=['reference','coalesced'],default='reference')
    parser.add_argument('--socket-fd',type=int);parser.add_argument('--certfile',default='');parser.add_argument('--keyfile',default='')
    parser.add_argument('--diagnose-h2',action='store_true')
    parser.add_argument('--workload',choices=['slow_consumer','cancellation'])
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    global DIAGNOSIS_ENABLED, FAILURE_LOG
    DIAGNOSIS_ENABLED=args.diagnose_h2
    FAILURE_LOG=args.output.with_suffix('.server-failure.log') if args.output else None
    if args.server:
        if args.diagnose_h2:diagnose_h2()
        args.transport='owned';args.original_owner_source='';asyncio.run(lifecycle_server(args));return
    assert args.output and args.samples>0 and args.pairs>0
    sys.executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    names=('benchmarks/http_pressure_measure.py','benchmarks/http_cpu_clock.py','benchmarks/http_runtime.py','benchmarks/http_stream_lifecycle.py','benchmarks/http_frame_coalescing.py','benchmarks/http_frame_coalescing_lifecycle.py','benchmarks/http_frame_coalescing_measure.py','tofu_agent/server.py','lib/hypercorn_runtime.py')
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    report=dict(format='tofu.http-pressure-measurement/v1',scope=__doc__,whole_system_verdict='incomplete',production_promoted=False,instrumented=args.diagnose_h2,sources=sources,samples=args.samples,required_pairs=args.pairs,cases=[])
    seen=set()
    with tempfile.TemporaryDirectory(prefix='tofu-http-pressure-') as folder:
        for protocol in ('h1','h2'):
            for case in ('slow_consumer','cancellation'):
                row=dict(protocol=protocol,case=case,pairs=[]);report['cases'].append(row)
                for pair in range(args.pairs):
                    values={}
                    for variant in (('reference','coalesced') if pair%2==0 else ('coalesced','reference')):
                        assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                        value=trial(variant,protocol,case,args.samples,Path(folder));memory=trial(variant,protocol,case,args.samples,Path(folder),True)
                        for result in (value,memory):assert result['server_pid'] not in seen;seen.add(result['server_pid'])
                        assert value['terminal_witnesses']==memory['terminal_witnesses']
                        value.update(memory_server_pid=memory['server_pid'],rss_after_requests_bytes=memory['rss_after_requests_bytes']);values[variant]=value
                    assert values['reference']['terminal_witnesses']==values['coalesced']['terminal_witnesses']
                    row['pairs'].append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(protocol,case,pair,'samples',args.samples,flush=True)


if __name__=='__main__':main()

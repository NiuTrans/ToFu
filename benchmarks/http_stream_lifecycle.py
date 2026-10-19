#!/usr/bin/env python3
"""Owned real TCP/TLS transport checks for Headless SSE cleanup/backpressure.

Production auth/routes/SSE framing run unchanged. Only compute and diagnostics
are synthetic. This is not full-agent, persistence or framework admission.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import hashlib
import platform
import statistics
import os
import threading
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks.http_runtime import ComputeFixture, ExecutionFixture, TOKEN


class StreamingExecution(ExecutionFixture):
    def __init__(self,task_id,request,runtime):
        super().__init__(task_id,request)
        self.status='running';self.runtime=runtime
        self.active=0;self.closed=0;self.produced=0

    async def events_async(self,*,cursor=0):
        self.active+=1
        try:
            for index in range(cursor,512):
                if self.status=='aborted':
                    yield {'seq':index,'type':'aborted'}
                    return
                self.produced+=1
                yield {'seq':index,'type':'delta','content':'x'*65536}
                await asyncio.sleep(0)
        finally:
            self.active-=1;self.closed+=1


class StreamingRuntime(ComputeFixture):
    def start(self,request):
        old=super().start(request)
        execution=StreamingExecution(old.task_id,request,self)
        self.executions[old.task_id]=execution
        return execution


async def serve_child(args):
    if args.transport in {'owned','owned-original'}:
        from lib.hypercorn_runtime import serve
        if args.transport=='owned-original':
            import ast
            import lib.hypercorn_runtime as runtime
            assert args.original_owner_source, 'original-owner comparison requires frozen source'
            tree=ast.parse(Path(args.original_owner_source).read_text())
            owner=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='_OwnedTCPServer')
            namespace=dict(runtime.__dict__)
            exec(compile(ast.Module(body=[owner],type_ignores=[]),'<original-owned-transport>','exec'),namespace)
            runtime._OwnedTCPServer=namespace['_OwnedTCPServer']
    else:
        from hypercorn.asyncio import serve
    from hypercorn.config import Config
    from quart import jsonify
    from tofu_agent.server import HeadlessServerConfig,create_app
    runtime=StreamingRuntime()
    app=create_app(runtime=runtime,model_routing_setup=object(),
        config=HeadlessServerConfig(token=TOKEN,auth_mode='token',setup_enabled=False))
    @app.get('/benchmark/stream/<task_id>')
    async def diagnostics(task_id):
        execution=runtime.get(task_id)
        return jsonify({'active':execution.active,'closed':execution.closed,
                        'produced':execution.produced,'status':execution.status})
    @app.get('/benchmark/system')
    async def system_diagnostics():
        import gc
        import psutil
        from collections import Counter
        from quart import request
        collected=gc.collect() if request.args.get('collect')=='1' else None
        names=Counter(getattr(task.get_coro(),'__qualname__',type(task.get_coro()).__name__) for task in asyncio.all_tasks() if not task.done())
        chains=Counter()
        for task in asyncio.all_tasks():
            if task.done():continue
            current=task.get_coro();chain=[];seen=set()
            while current is not None and id(current) not in seen and len(chain)<24:
                seen.add(id(current));chain.append(getattr(current,'__qualname__',type(current).__name__))
                current=getattr(current,'cr_await',getattr(current,'gi_yieldfrom',getattr(current,'ag_await',None)))
            chains[' -> '.join(chain)]+=1
        generators=Counter(getattr(generator,'__qualname__',type(generator).__name__) for generator in getattr(asyncio.get_running_loop(),'_asyncgens',()))

        return jsonify({'rss_bytes':psutil.Process().memory_info().rss,'active_producers':sum(execution.active for execution in runtime.executions.values()),
            'tasks':dict(names),'await_chains':dict(chains),'async_generators':dict(generators),'gc_collected':collected,'gc_counts':gc.get_count()})
    config=Config();config.bind=[f'fd://{args.socket_fd}'];config.accesslog=config.errorlog=None
    config.graceful_timeout=2
    if args.certfile:config.certfile,config.keyfile=args.certfile,args.keyfile
    stopped=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stopped.set)
    await serve(app,config,shutdown_trigger=stopped.wait)


async def check(origin,protocol,verify):
    import httpx
    async with httpx.AsyncClient(base_url=origin,verify=verify,http2=protocol=='h2',
            trust_env=False,headers={'Authorization':f'Bearer {TOKEN}'},timeout=5) as client:
        # Stream and diagnostics use separate connections; H2 still exercises stream cancellation.
        async with httpx.AsyncClient(base_url=origin,verify=verify,http2=protocol=='h2',
                trust_env=False,headers={'Authorization':f'Bearer {TOKEN}'},timeout=5) as observer:
            created=await client.post('/api/v1/agent/run',json={
                'messages':[{'role':'user','content':'synthetic'}]},headers={'Prefer':'respond-async'})
            assert created.status_code==202,created.text
            task_id=created.json()['task_id'];url=f'/api/v1/tasks/{task_id}/stream'
            async with client.stream('GET',url) as response:
                assert response.status_code==200
                assert response.http_version==('HTTP/2' if protocol=='h2' else 'HTTP/1.1')
                lines=response.aiter_lines()
                async for line in lines:
                    if line.startswith('data: '):
                        assert json.loads(line[6:])['seq']==0
                        break
                await asyncio.sleep(.05)
                held=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                assert held['active']==1 and 0<held['produced']<512,held
            # HTTPX response close alone does not emit RST_STREAM in this version.
            # Close the actual transport; explicit stream-reset is checked separately.
            await client.aclose()
            disconnected=time.perf_counter_ns()
            deadline=time.monotonic()+2
            while True:
                state=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                if state['active']==0:break
                if time.monotonic()>=deadline:
                    diagnostic=(await observer.get('/benchmark/system')).json()
                    raise AssertionError(('stream did not release producer',state,diagnostic))
                await asyncio.sleep(.01)
            cleanup_ms=(time.perf_counter_ns()-disconnected)/1e6
            assert state['closed']==1 and state['status']=='running',state
            # Native cursor reconnect; cancelling transport must not abort owned computation.
            async with observer.stream('GET',url+'?cursor=7') as response:
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        assert json.loads(line[6:])['seq']==7
                        break
                try:
                    aborted=await observer.post(f'/api/v1/tasks/{task_id}/abort')
                except httpx.TimeoutException as error:
                    # Observe after the original timeout through a fresh
                    # connection; do not change/close the tested stream first.
                    diagnostic={'stage':'same_connection_abort','task_id':task_id,'protocol':protocol}
                    try:
                        async with httpx.AsyncClient(base_url=origin,verify=verify,http2=protocol=='h2',
                                trust_env=False,headers={'Authorization':f'Bearer {TOKEN}'},timeout=1) as fresh:
                            diagnostic['execution']=(await fresh.get(f'/benchmark/stream/{task_id}')).json()
                    except Exception as secondary:
                        diagnostic['diagnostic_error']=type(secondary).__name__
                    error.add_note(json.dumps(diagnostic,sort_keys=True))
                    raise
                assert aborted.status_code==200 and aborted.json()['abort_requested']
            deadline=time.monotonic()+2
            while True:
                final=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                if final['active']==0:break
                assert time.monotonic()<deadline,final
                await asyncio.sleep(.01)
            assert final['closed']==2 and final['status']=='aborted',final
            return {'cleanup_ms':cleanup_ms,'held':held,'disconnected':state,'aborted':final}


async def check_h2_reset(origin,verify):
    """Send a witnessed RST_STREAM while keeping the same H2 socket usable."""
    import ssl
    import httpx
    from urllib.parse import urlsplit
    from h2.connection import H2Connection
    from h2.config import H2Configuration
    from h2.events import DataReceived,ResponseReceived,StreamEnded
    from h2.settings import SettingCodes
    address=urlsplit(origin)
    context=ssl.create_default_context(cafile=verify);context.set_alpn_protocols(['h2'])
    reader,writer=await asyncio.open_connection(address.hostname,address.port,ssl=context,server_hostname=address.hostname)
    connection=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8'))
    connection.initiate_connection();connection.update_settings({SettingCodes.INITIAL_WINDOW_SIZE:131072})
    connection.increment_flow_control_window(1048576)
    writer.write(connection.data_to_send());await writer.drain()
    async with httpx.AsyncClient(base_url=origin,verify=verify,http2=True,trust_env=False,
            headers={'Authorization':f'Bearer {TOKEN}'},timeout=5) as observer:
        try:
            created=await observer.post('/api/v1/agent/run',json={'messages':[{'role':'user','content':'synthetic'}]},headers={'Prefer':'respond-async'})
            assert created.status_code==202
            task_id=created.json()['task_id']
            def request_headers(path):
                return [(':method','GET'),(':scheme','https'),(':authority',address.netloc),(':path',path),('authorization',f'Bearer {TOKEN}')]
            connection.send_headers(1,request_headers(f'/api/v1/tasks/{task_id}/stream'),end_stream=True)
            writer.write(connection.data_to_send());await writer.drain()
            received=b'';status=None
            while b'\n\n' not in received:
                data=await asyncio.wait_for(reader.read(65536),5);assert data
                for event in connection.receive_data(data):
                    if isinstance(event,ResponseReceived):status=dict(event.headers)[':status']
                    if isinstance(event,DataReceived):
                        received+=event.data
                        connection.acknowledge_received_data(event.flow_controlled_length,event.stream_id)
                writer.write(connection.data_to_send());await writer.drain()
            assert status=='200' and b'id: 0\n' in received
            await asyncio.sleep(.05)
            held=(await observer.get(f'/benchmark/stream/{task_id}')).json()
            assert held['active']==1 and 0<held['produced']<512,held
            connection.reset_stream(1,error_code=8)
            reset_bytes=connection.data_to_send();assert reset_bytes
            writer.write(reset_bytes);await writer.drain()
            started=time.perf_counter_ns();deadline=time.monotonic()+2
            while True:
                state=(await observer.get(f'/benchmark/stream/{task_id}')).json()
                if state['active']==0:break
                assert time.monotonic()<deadline,('RST_STREAM did not release producer',state)
                await asyncio.sleep(.01)
            assert state['closed']==1 and state['status']=='running',state
            cleanup_ms=(time.perf_counter_ns()-started)/1e6
            connection.send_headers(3,request_headers('/api/v1/capabilities'),end_stream=True)
            writer.write(connection.data_to_send());await writer.drain()
            complete=False;body=b'';status=None
            while not complete:
                data=await asyncio.wait_for(reader.read(65536),5);assert data
                for event in connection.receive_data(data):
                    if isinstance(event,ResponseReceived) and event.stream_id==3:status=dict(event.headers)[':status']
                    if isinstance(event,DataReceived) and event.stream_id==3:
                        body+=event.data;connection.acknowledge_received_data(event.flow_controlled_length,3)
                    if isinstance(event,StreamEnded) and event.stream_id==3:complete=True
                writer.write(connection.data_to_send());await writer.drain()
            assert status=='200' and json.loads(body)['runtime']=='tofu-agent'
            return {'cleanup_ms':cleanup_ms,'held':held,'after_reset':state,'same_connection_next_stream':True}
        finally:
            writer.close();await writer.wait_closed()


def trial(loop,protocol,directory,samples=1,memory=False,diagnose=False,transport="upstream",original_owner_source=""):
    import httpx
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    certfile=keyfile=''
    if protocol=='h2':
        from lib.server.server_tls import ensure_tls_certificates
        certfile,keyfile=ensure_tls_certificates(data_root=str(directory),bind_host='127.0.0.1',environ={})
    origin=f'{"https" if protocol=="h2" else "http"}://127.0.0.1:{port}'
    verify=certfile or True
    log=directory/f'{loop}-{protocol}-{time.time_ns()}.log'
    with log.open('wb') as output:
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--server','--loop',loop,
            '--socket-fd',str(listener.fileno()),'--certfile',certfile,'--keyfile',keyfile,'--transport',transport,'--original-owner-source',str(original_owner_source)],
            pass_fds=(listener.fileno(),),stdout=output,stderr=output)
    listener.close()
    try:
        deadline=time.monotonic()+60
        with httpx.Client(verify=verify,trust_env=False,timeout=1) as probe:
            while True:
                if process.poll() is not None:raise RuntimeError(log.read_text()[-2000:])
                try:
                    if probe.get(origin+'/health/ready').status_code==200:break
                except httpx.TransportError:
                    if time.monotonic()>=deadline:raise TimeoutError('Owned server readiness expired')
                    time.sleep(.05)
        import psutil
        server=psutil.Process(process.pid);peak=[server.memory_info().rss];stop=threading.Event();sampler=None
        if memory:
            def sample_memory():
                while not stop.is_set():
                    peak[0]=max(peak[0],server.memory_info().rss);stop.wait(.005)
            sampler=threading.Thread(target=sample_memory,daemon=True);sampler.start()
        before=server.cpu_times()
        async def collect():
            rows=[]
            for index in range(samples+20):
                row=await check(origin,protocol,verify)
                if protocol=='h2':row['explicit_stream_reset']=await check_h2_reset(origin,verify)
                if index>=20:rows.append(row)
            return rows
        try:rows=asyncio.run(collect())
        finally:
            stop.set()
            if sampler:sampler.join(timeout=2);assert not sampler.is_alive()
        after=server.cpu_times();raw=[row['cleanup_ms'] for row in rows];ordered=sorted(raw)
        result={'server_pid':process.pid,'samples':samples,'warmups':20,
            'observed_cleanup_p50_ms':statistics.median(raw),'observed_cleanup_p99_ms':ordered[int((samples-1)*.99)],
            'server_cpu_ms_per_cycle_including_warmup':(after.user+after.system-before.user-before.system)*1000/(samples+20),
            'sampled_peak_server_rss_bytes':peak[0] if memory else None,
            'raw':rows if not memory else [],'synthetic_compute':True}
        if diagnose:
            with httpx.Client(base_url=origin,verify=verify,http2=protocol=='h2',trust_env=False,
                    headers={'Authorization':f'Bearer {TOKEN}'},timeout=5) as diagnostics:
                result['transport_before_gc']=diagnostics.get('/benchmark/system').json()
                time.sleep(1)
                result['transport_after_idle']=diagnostics.get('/benchmark/system').json()
                result['transport_after_gc']=diagnostics.get('/benchmark/system?collect=1').json()
        return result
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
            raise RuntimeError('Owned server failed graceful shutdown')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',action='store_true');parser.add_argument('--loop',choices=['asyncio','uvloop'],default='asyncio')
    parser.add_argument('--socket-fd',type=int);parser.add_argument('--certfile',default='');parser.add_argument('--keyfile',default='')
    parser.add_argument('--output',type=Path,required=False)
    parser.add_argument('--original-owner-source',default='')
    parser.add_argument('--transport',choices=['upstream','owned','owned-original'],default='upstream')
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--check',action='store_true');parser.add_argument('--diagnose',action='store_true');args=parser.parse_args()
    if args.server:
        if args.loop=='uvloop':
            import uvloop
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:runner.run(serve_child(args))
        else:asyncio.run(serve_child(args))
        return
    assert args.output is not None,'--output required'
    if not args.check and not args.diagnose and (args.samples<100 or args.pairs<3):parser.error('comparison requires 100 samples and three pairs')
    if args.samples<1 or args.pairs<1:parser.error('samples and pairs must be positive')
    report={'transport':args.transport,'owner_source_sha256':hashlib.sha256((ROOT/'lib/hypercorn_runtime.py').read_bytes()).hexdigest(),
        'original_owner_source_sha256':hashlib.sha256(Path(args.original_owner_source).read_bytes()).hexdigest() if args.original_owner_source else None,'scope':'production Headless transport with synthetic compute; lifecycle comparison, not performance admission',
        'whole_system_verdict':'incomplete','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'runtime':{'python':sys.version,'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0))},
        'metrics_limitations':['cleanup latency ends at observer HTTP acknowledgement; not actual producer release time',
            'CPU includes synthetic producer, TLS, diagnostic requests and 20 warmups; not serving cost alone',
            'separate 5ms sampled server RSS; spikes shorter than sampling interval may be missed',
            'connection close and actual RST_STREAM are distinct; HTTPX response close alone sends no reset in installed httpcore'],
        'trials':[]}
    with tempfile.TemporaryDirectory(prefix='tofu-http-stream-') as temporary:
        for protocol in (('h2',) if args.diagnose else ('h1','h2')):
            for pair in range(1 if args.check or args.diagnose else args.pairs):
                measurements={}
                for loop in ((args.loop,) if args.diagnose else (('asyncio','uvloop') if pair%2==0 else ('uvloop','asyncio'))):
                    values=trial(loop,protocol,Path(temporary),1 if args.check else args.samples,diagnose=args.diagnose,transport=args.transport,original_owner_source=args.original_owner_source)
                    if not args.check and not args.diagnose:
                        memory=trial(loop,protocol,Path(temporary),args.samples,memory=True,transport=args.transport,original_owner_source=args.original_owner_source)
                        values['memory_server_pid']=memory['server_pid']
                        values['sampled_peak_server_rss_bytes']=memory['sampled_peak_server_rss_bytes']
                    measurements[loop]=values
                    print(protocol,pair,loop,{key:value for key,value in values.items() if key!='raw'},flush=True)
                report['trials'].append({'protocol':protocol,'pair':pair,'measurements':measurements})
                args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')

if __name__=='__main__':main()

#!/usr/bin/env python3
"""Granian embedded ASGI vs owned Hypercorn; same Headless application and compute.

Inherited sockets, app/loop/thread policy and teardown are experiment-owned.
Embedded Granian is diagnostic, not CLI/full-app deployment admission.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import statistics
import threading
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from benchmarks.http_runtime import ComputeFixture,TOKEN,requests


def application(lifecycle=False):
    from tofu_agent.server import HeadlessServerConfig,create_app
    from benchmarks.http_stream_lifecycle import StreamingRuntime
    runtime=StreamingRuntime() if lifecycle else ComputeFixture()
    app=create_app(runtime=runtime,model_routing_setup=object(),
        config=HeadlessServerConfig(token=TOKEN,auth_mode='token',setup_enabled=False))
    if lifecycle:
        from quart import jsonify
        @app.get('/benchmark/stream/<task_id>')
        async def diagnostics(task_id):
            execution=runtime.get(task_id)
            return jsonify({'active':execution.active,'closed':execution.closed,'produced':execution.produced,'status':execution.status})
    return app


async def serve(args):
    app=application(args.lifecycle)
    if args.backend=='owned':
        from hypercorn.config import Config
        from lib.hypercorn_runtime import serve as run
        config=Config();config.bind=[f'fd://{args.socket_fd}'];config.errorlog=config.accesslog=None;config.graceful_timeout=2
        if args.certfile:config.certfile,config.keyfile=args.certfile,args.keyfile
        stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
        await run(app,config,shutdown_trigger=stop.wait)
    else:
        from granian.server.embed import Server
        from granian.constants import Interfaces
        from granian._granian import SocketHolder
        class InheritedSocketServer(Server):
            def _init_shared_socket(self):
                self._ssp=None;self._shd=SocketHolder(os.dup(args.socket_fd),False,128)
                self._sfd=self._shd.get_fd()
        server=InheritedSocketServer(app,interface=Interfaces.ASGI,runtime_threads=1,
            log_enabled=False,ssl_cert=Path(args.certfile) if args.certfile else None,
            ssl_key=Path(args.keyfile) if args.keyfile else None)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,server.stop)
        await server.serve()


def trial(backend,protocol,case,directory,samples=100,memory=False,*,executable=None,operation=None):
    import httpx
    import psutil
    if operation is not None and case!='lifecycle':
        raise ValueError('custom lifecycle observation cannot collect performance samples')
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
    certfile=keyfile=''
    if protocol=='h2':
        from lib.server.server_tls import ensure_tls_certificates
        certfile,keyfile=ensure_tls_certificates(data_root=str(directory),bind_host='127.0.0.1',environ={})
    origin=f'{"https" if protocol=="h2" else "http"}://127.0.0.1:{listener.getsockname()[1]}'
    log=directory/f'{backend}-{protocol}-{case}-{time.time_ns()}.log'
    with log.open('wb') as stream:
        process=subprocess.Popen([executable or sys.executable,str(Path(__file__).resolve()),'--server','--backend',backend,
            '--socket-fd',str(listener.fileno()),'--certfile',certfile,'--keyfile',keyfile,*(['--lifecycle'] if case=='lifecycle' else [])],pass_fds=(listener.fileno(),),stdout=stream,stderr=stream)
    listener.close()
    result=None
    try:
        deadline=time.monotonic()+60
        with httpx.Client(verify=certfile or True,trust_env=False,timeout=1) as probe:
            while True:
                if process.poll() is not None:raise RuntimeError(log.read_text()[-3000:])
                try:
                    if probe.get(origin+'/health/ready').status_code==200:break
                except httpx.TransportError:
                    if time.monotonic()>deadline:raise TimeoutError('owned server readiness deadline')
                    time.sleep(.05)
        if operation is not None:
            result=operation(process,[],origin,certfile or True)
            result.update(pid=process.pid,protocol=protocol)
            return result
        if case=='lifecycle':
            from benchmarks.http_stream_lifecycle import check,check_h2_reset
            result=asyncio.run(check(origin,protocol,certfile or True))
            if protocol=='h2':result['explicit_reset']=asyncio.run(check_h2_reset(origin,certfile))
            return result
        server=psutil.Process(process.pid);peak=[server.memory_info().rss];count=[0];stop=threading.Event();sampler=None
        if memory:
            def sample():
                while not stop.is_set():
                    peak[0]=max(peak[0],server.memory_info().rss);count[0]+=1;stop.wait(.005)
            sampler=threading.Thread(target=sample,daemon=True);sampler.start()
        before=server.cpu_times()
        try:raw,warmups=asyncio.run(requests(origin,protocol,case,samples,16,certfile or True))
        finally:
            stop.set()
            if sampler:sampler.join(timeout=2);assert not sampler.is_alive()
        after=server.cpu_times();ordered=sorted(raw)
        return {'raw_ms':raw,'p50_ms':statistics.median(raw),'p95_ms':ordered[int((samples-1)*.95)],'p99_ms':ordered[int((samples-1)*.99)],
            'warmups':warmups,'server_cpu_ms_per_request':(after.user+after.system-before.user-before.system)*1000/(samples+warmups),
            'sampled_peak_server_rss_bytes':peak[0] if memory else None,'rss_samples':count[0],'pid':process.pid}
    except BaseException:
        print(log.read_text()[-3000:],file=sys.stderr);raise
    finally:
        primary=sys.exc_info()[1]
        process.terminate()
        try:
            code=process.wait(timeout=10)
            if operation is not None:
                if code!=0:raise RuntimeError(f'owned lifecycle server shutdown returned {code}')
                if result is not None:result.update(shutdown_returncode=code,server_joined=True)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
            cleanup=RuntimeError('owned server failed graceful shutdown')
            if primary is not None:primary.add_note(str(cleanup))
            else:raise cleanup
        except Exception as cleanup:
            if primary is not None:primary.add_note('owned server cleanup failed: '+repr(cleanup))
            else:raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--server',action='store_true')
    parser.add_argument('--backend',choices=['owned','granian'],default='owned');parser.add_argument('--socket-fd',type=int)
    parser.add_argument('--lifecycle',action='store_true');parser.add_argument('--measure',action='store_true');parser.add_argument('--certfile',default='');parser.add_argument('--keyfile',default='');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.server:asyncio.run(serve(args));return
    report={'scope':__doc__,'whole_system_verdict':'incomplete','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'limitations':['Granian2.8.4 embedded mode and inherited-socket adapter, not CLI deployment','same Python asyncio app loop, one native runtime thread','RSS sampled5ms in independent process, spikes may be missed','CPU includes warmup/cohort requests; cold startup not statistically admitted'],'checks':[]}
    with tempfile.TemporaryDirectory(prefix='tofu-native-http-') as temporary:
        for protocol in ['h1','h2']:
            cases=['short_requests','authentication','sse_fanout'] if args.measure else ['lifecycle']
            for case in cases:
                for pair in range(3 if args.measure else 1):
                    values={}
                    for backend in (['owned','granian'] if pair%2==0 else ['granian','owned']):
                        result=trial(backend,protocol,case,Path(temporary),samples=1000 if args.measure else 100)
                        if args.measure:
                            memory=trial(backend,protocol,case,Path(temporary),samples=1000,memory=True)
                            result['sampled_peak_server_rss_bytes']=memory['sampled_peak_server_rss_bytes'];result['memory_pid']=memory['pid'];result['rss_samples']=memory['rss_samples']
                        values[backend]=result;print(backend,protocol,case,pair,'pass',flush=True)
                    report['checks'].append({'protocol':protocol,'case':case,'pair':pair,'measurements':values})
                    args.output.write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()

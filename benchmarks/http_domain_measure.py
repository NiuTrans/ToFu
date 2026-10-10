"""Five declared HTTP workloads through owned Headless TCP/TLS transport.

Short/auth/SSE unit is a concurrency-sized request batch; slow/cancel units are
release/abort-and-close to observer quiescence. Setup/hold are excluded. CPU is
owned server process time. Separate memory servers sample peak RSS and observe
post-GC idle growth from post-warmup GC baseline. Synthetic compute is explicit.
"""
import argparse as argparse
import asyncio as asyncio
import hashlib as hashlib
import importlib.metadata
import inspect as inspect
import json as json
import os as os
from pathlib import Path as Path
import platform as platform
import statistics as statistics
import subprocess as subprocess
import sys as sys
import tempfile as tempfile
import threading as threading
import time as time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks import http_runtime as http_runtime,http_pressure_measure as http_pressure_measure,http_frame_coalescing_measure as http_frame_coalescing_measure,http_frame_coalescing_lifecycle as http_frame_coalescing_lifecycle
from benchmarks.http_cpu_clock import OwnedProcessCpuClock as OwnedProcessCpuClock
from benchmarks.system_performance import compare as compare
from runtime_guards import probe_system_resources as probe_system_resources,resource_budget_manifest as resource_budget_manifest

CASES=('short_requests','authentication','sse_fanout','slow_consumer','cancellation')


class ServerMemory:
    def __init__(self,pid):
        import psutil
        self.process=psutil.Process(pid);self.stop_event=threading.Event();self.peak=0;self.error=None
        self.thread=threading.Thread(target=self.sample);self.thread.start()
    def sample(self):
        try:
            while not self.stop_event.is_set():
                self.peak=max(self.peak,self.process.memory_info().rss)
                self.stop_event.wait(.005)
        except Exception as error:self.error=error
    def hwm(self):
        lines=Path(f'/proc/{self.process.pid}/status').read_text().splitlines()
        return next(int(line.split()[1])*1024 for line in lines if line.startswith('VmHWM:'))
    def close(self,primary=None):
        self.stop_event.set();self.thread.join()
        if self.error is not None:
            if primary is None:raise self.error
            primary.add_note('owned server memory sampling failed: '+str(self.error))


async def memory_snapshot(origin,protocol,verify):
    import httpx
    async with httpx.AsyncClient(base_url=origin,http2=protocol=='h2',verify=verify,trust_env=False,
            headers={'Authorization':f'Bearer {http_runtime.TOKEN}'},timeout=10) as client:
        response=await client.get('/benchmark/memory?collect=1');assert response.status_code==200
        value=response.json();assert value['active_producers']==0
        return value['rss_bytes']


async def requests(pid,origin,protocol,case,samples,concurrency,verify,memory):
    import httpx
    clock=OwnedProcessCpuClock(pid);sampler=ServerMemory(pid) if memory else None
    baseline=None
    try:
        if case in ('slow_consumer','cancellation'):
            source=inspect.getsource(http_pressure_measure.requests)
            needle='    for index in range(samples):await one(index)';assert source.count(needle)==1
            source=source.replace(needle,'    if memory: baseline.append(await memory_snapshot(origin,protocol,verify))\n'+needle)
            values=[];namespace=dict(http_pressure_measure.requests.__globals__,memory=memory,baseline=values,memory_snapshot=memory_snapshot)
            exec(compile(source,'<owned-http-domain-pressure>','exec'),namespace)
            walls,_,cpus,witnesses=await namespace['requests'](pid,origin,protocol,case,samples,1,verify)
            if memory:baseline=values[0]
        else:
            options=dict(base_url=origin,http2=protocol=='h2',verify=verify,trust_env=False,
                headers={'Authorization':f'Bearer {http_runtime.TOKEN}'},timeout=30,
                limits=httpx.Limits(max_connections=concurrency,max_keepalive_connections=concurrency))
            client=httpx.AsyncClient(**options)
            async def one():
                if case=='sse_fanout':
                    async with client.stream('POST','/api/v1/agent/run',json={'messages':[{'role':'user','content':'synthetic'}],'stream':True}) as response:
                        assert response.status_code==200
                        assert response.http_version==('HTTP/2' if protocol=='h2' else 'HTTP/1.1')
                        values=[json.loads(line[6:]) async for line in response.aiter_lines() if line.startswith('data: ') and line!='data: [DONE]']
                        assert [value['data']['seq'] for value in values]==list(range(9))
                        assert all(value.get('delta',{}).get('content')=='synthetic 中文' for value in values[:8])
                        assert values[-1]['event']=='done'
                else:
                    response=await client.get('/api/v1/capabilities',headers={'Authorization':'Bearer incorrect'} if case=='authentication' else {})
                    assert response.status_code==(401 if case=='authentication' else 200)
                    assert response.http_version==('HTTP/2' if protocol=='h2' else 'HTTP/1.1')
                    if case!='authentication':assert response.json()['runtime']=='tofu-agent'
            async def batch():await asyncio.gather(*(one() for _ in range(concurrency)))
            walls=[];cpus=[];witnesses=[]
            try:
                for _ in range(20):await batch()
                if memory:baseline=await memory_snapshot(origin,protocol,verify)
                for index in range(samples):
                    if index and index%16==0:
                        await client.aclose();client=httpx.AsyncClient(**options)
                        await asyncio.gather(*(one() for _ in range(20)))
                    start_cpu=clock.read_ns();start=time.perf_counter_ns();await batch()
                    walls.append((time.perf_counter_ns()-start)/1e6);cpus.append((clock.read_ns()-start_cpu)/1e6)
                    witnesses.append(dict(requests=concurrency,status=401 if case=='authentication' else 200,events_per_request=9 if case=='sse_fanout' else 0))
            finally:await client.aclose()
        assert len(walls)==len(cpus)==samples and all(value>0 for value in cpus)
        result=dict(wall_samples_ms=walls,cpu_samples_ms=cpus,warmup_operations=20,terminal_witnesses=witnesses)
        if memory:
            after=await memory_snapshot(origin,protocol,verify)
            result.update(peak_rss_bytes=max(sampler.peak,sampler.hwm()),retained_bytes=max(0,after-baseline),retention_baseline_rss_bytes=baseline,post_gc_rss_bytes=after)
        return result
    finally:
        if sampler is not None:sampler.close(sys.exc_info()[1])


async def serve_child(args):
    from tofu_agent.server import create_app
    from benchmarks.http_frame_coalescing import create_app as coalesced
    from quart import jsonify
    selected=coalesced if args.loop=='coalesced' else create_app
    def application(*values,**options):
        app=selected(*values,**options);runtime=options['runtime']
        @app.get('/benchmark/memory')
        async def memory():
            import gc
            import psutil
            gc.collect()
            return jsonify(rss_bytes=psutil.Process().memory_info().rss,
                active_producers=sum(getattr(value,'active',0) for value in runtime.executions.values()))
        return app
    if args.workload in ('slow_consumer','cancellation'):
        owner=http_frame_coalescing_lifecycle.http_stream_lifecycle.serve_child
        source=inspect.getsource(owner)
        seam='from tofu_agent.server import HeadlessServerConfig,create_app';assert source.count(seam)==1
        source=source.replace(seam,'from tofu_agent.server import HeadlessServerConfig')
        namespace=dict(owner.__globals__,create_app=application)
        exec(compile(source,'<owned-http-domain-pressure-server>','exec'),namespace)
        args.transport='owned';args.original_owner_source='';await namespace['serve_child'](args)
    else:
        source=inspect.getsource(http_runtime.serve_child)
        source=source.replace('from hypercorn.asyncio import serve','from lib.hypercorn_runtime import serve')
        source=source.replace('from tofu_agent.server import HeadlessServerConfig, create_app','from tofu_agent.server import HeadlessServerConfig')
        namespace=dict(http_runtime.serve_child.__globals__,create_app=application)
        exec(compile(source,'<owned-http-domain-server>','exec'),namespace);await namespace['serve_child'](args)


def trial(variant,protocol,case,samples,concurrency,folder,memory):
    source=http_frame_coalescing_measure.checked_trial_source(http_runtime.trial)
    needle='raw, warmup_requests = asyncio.run(requests(origin, protocol, case, samples, concurrency, verify))';assert source.count(needle)==1
    source=source.replace(needle,'measurement = asyncio.run(domain_requests(process.pid, origin, protocol, case, samples, concurrency, verify, memory))\n        raw=measurement[\'wall_samples_ms\'];warmup_requests=20')
    source=source.replace("'raw_wall_ms': raw if not memory else [],","'measurement':measurement,")
    needle='    log = directory /';assert source.count(needle)==1
    source=source.replace(needle,"    argv.extend(['--workload',case])\n"+needle)
    namespace=dict(http_runtime.trial.__globals__,__file__=__file__,domain_requests=requests,_checked_shutdown=http_frame_coalescing_measure.checked_shutdown)
    exec(compile(source,'<owned-five-case-http-trial>','exec'),namespace)
    result=namespace['trial'](variant,protocol,case,samples,concurrency,folder,memory)
    measurement=result['measurement'];wall=measurement['wall_samples_ms'];ordered=sorted(wall)
    measurement.update(process_identity=result['server_pid'],protocol=protocol,p50_ms=statistics.median(wall),p95_ms=ordered[int((samples-1)*.95)],p99_ms=ordered[int((samples-1)*.99)],throughput_per_second=samples*1000/sum(wall),cpu_ms_per_operation=statistics.mean(measurement['cpu_samples_ms']),profiler_enabled=False,tracemalloc_enabled=False,memory_separate_process=memory,shutdown_exit_code=0,shutdown_joined=True)
    return measurement


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',action='store_true');parser.add_argument('--loop',choices=('reference','coalesced'),default='reference')
    parser.add_argument('--socket-fd',type=int);parser.add_argument('--certfile',default='');parser.add_argument('--keyfile',default='');parser.add_argument('--workload',choices=CASES)
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--concurrency',type=int,default=16);parser.add_argument('--output-prefix',type=Path)
    args=parser.parse_args()
    if args.server:asyncio.run(serve_child(args));return
    assert args.output_prefix and args.samples>0 and args.pairs>0 and args.concurrency>0
    sys.executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    receipts={}
    for name,extra in [('http_frame_coalescing_check.py',[]),('http_frame_coalescing_lifecycle.py',['--output',str(args.output_prefix)+'-lifecycle.json'])]:
        result=subprocess.run([sys.executable,str(ROOT/'benchmarks'/name),*extra],cwd=ROOT,check=True,capture_output=True,text=True,timeout=180)
        receipts[name]=result.stdout.strip()
    names=['benchmarks/'+n for n in ('http_domain_measure.py','http_cpu_clock.py','http_runtime.py','http_pressure_measure.py','http_stream_lifecycle.py','http_frame_coalescing.py','http_frame_coalescing_check.py','http_frame_coalescing_measure.py','http_frame_coalescing_lifecycle.py')]+['lib/hypercorn_runtime.py','tofu_agent/server.py']
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    runtime=dict(python=sys.version,platform=platform.platform(),packages={n:importlib.metadata.version(n) for n in ('hypercorn','quart','httpx','h2')})
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    reports={v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,check_receipts=receipts,workloads=[],limitations=['synthetic compute; no inference/full application/storage','5ms RSS sample plus parent VmHWM; one GC retention interval','pressure setup and observer polling declared separately']) for v in ('reference','candidate')};seen=set()
    with tempfile.TemporaryDirectory(prefix='tofu-http-domain-') as folder:
        for case in CASES:
            fixture_hash=hashlib.sha256(json.dumps(dict(case=case,protocols=['h1','h2'],concurrency=args.concurrency,samples=args.samples,stream='8 small or512x64KiB pressure deltas'),sort_keys=True).encode()).hexdigest();rows={}
            for variant in reports:
                row=dict(domain='http',case=case,scope='domain_integration',correctness_passed=True,failure_and_cleanup_passed=True,identity=dict(source_sha256=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant),sort_keys=True).encode()).hexdigest(),runtime=runtime,hardware=hardware,fixture_sha256=fixture_hash),measurements=[])
                rows[variant]=row;reports[variant]['workloads'].append(row)
            for protocol in ('h1','h2'):
                for pair in range(args.pairs):
                    values={}
                    for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                        assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                        arm='reference' if variant=='reference' else 'coalesced'
                        value=trial(arm,protocol,case,args.samples,args.concurrency,Path(folder),False);memory=trial(arm,protocol,case,args.samples,args.concurrency,Path(folder),True)
                        for result in (value,memory):assert result['process_identity'] not in seen;seen.add(result['process_identity'])
                        assert value['terminal_witnesses']==memory['terminal_witnesses']
                        value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],memory_process_identity=memory['process_identity'],memory_separate_process=True,retention_baseline_rss_bytes=memory['retention_baseline_rss_bytes'],post_gc_rss_bytes=memory['post_gc_rss_bytes']);values[variant]=value
                    assert values['reference']['terminal_witnesses']==values['candidate']['terminal_witnesses']
                    for variant in reports:
                        rows[variant]['measurements'].append(values[variant]);Path(str(args.output_prefix)+'-'+variant+'.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
                    print(case,protocol,pair,'samples',args.samples,flush=True)
    verdict=compare(reports['reference'],reports['candidate']);Path(str(args.output_prefix)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n');print('whole-system verdict',verdict['verdict'],'coverage',verdict['coverage'],flush=True)


if __name__=='__main__':main()

"""Five real-browser reader workloads, raw CPU, peak RSS and disposed residency.

Wall time is actual render plus forced layout. CPU brackets the same browser
RPC using owned browser/renderer process clocks, including RPC dispatch. Memory
runs use independent browsers, sample renderer RSS, read renderer VmHWM and
collect JS garbage before warm baseline and after renderer disposal. No physical
paint, full app/API, math or broad deployment-security claim is inferred.
"""
import hashlib as hashlib
import json as json
import os as os
from pathlib import Path as Path
import platform as platform
import statistics as statistics
import threading as threading
import time as time

from tests._runtime_sections import ROOT as ROOT
from benchmarks.markdown_worker.reader_measure import bundles as bundles
from benchmarks.http_cpu_clock import OwnedProcessCpuClock as OwnedProcessCpuClock
from benchmarks.system_performance import compare as compare

CASES=('streaming_prose','streaming_code','tables_and_references','selection_and_scroll','terminal_render')


def run(browser,origin,compiled,variant,case,samples,memory):
    import psutil
    options=dict(headless=True,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
    if os.environ.get('TOFU_E2E_EXECUTABLE_PATH'):options['executable_path']=os.environ['TOFU_E2E_EXECUTABLE_PATH']
    owned=browser.browser_type.launch(**options);stop=threading.Event();observer=None;errors=[];peak=[0]
    try:
        page=owned.new_page(viewport={'width':1180,'height':900});page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(origin,wait_until='load')
        for name in ('node_modules/marked/lib/marked.umd.js','node_modules/dompurify/dist/purify.min.js'):page.add_script_tag(path=str(ROOT/name))
        for name in ('policy','markdown','highlighter',variant):page.add_script_tag(path=compiled[name])
        page.add_script_tag(path=str(ROOT/'benchmarks/markdown_worker/domain_reader.js'));page.evaluate('document.fonts.ready')
        page.evaluate('name => {globalThis.domainReader=createDomainReader(name);}',case)
        cdp=owned.new_browser_cdp_session();renderer_cdp=page.context.new_cdp_session(page)
        identities=cdp.send('SystemInfo.getProcessInfo')['processInfo']
        browser_pid=next(int(r['id']) for r in identities if r['type']=='browser')
        renderer_pids=[int(r['id']) for r in identities if r['type']=='renderer'];assert renderer_pids
        clocks=[OwnedProcessCpuClock(pid) for pid in [browser_pid,*renderer_pids]]
        def cpu():return sum(clock.read_ns() for clock in clocks)
        def rss():return sum(psutil.Process(pid).memory_info().rss for pid in renderer_pids)
        def collect():renderer_cdp.send('HeapProfiler.collectGarbage')
        page.evaluate('domainReader.operation(-1)');page.evaluate('domainReader.oracle()')
        for index in range(20):page.evaluate('i => domainReader.operation(i)',index);page.evaluate('domainReader.oracle()')
        if memory:
            collect();baseline=rss()
            def sample():
                try:
                    while not stop.is_set():peak[0]=max(peak[0],rss());stop.wait(.005)
                except Exception as error:errors.append(str(error))
            observer=threading.Thread(target=sample);observer.start()
        walls=[];cpus=[]
        for index in range(20,20+samples):
            start=cpu();wall=page.evaluate('i => domainReader.operation(i)',index);consumed=(cpu()-start)/1e6
            assert wall>0 and consumed>0
            walls.append(wall);cpus.append(consumed)
            assert page.evaluate('domainReader.oracle()')==index+2
        final=page.evaluate('domainReader.source()');after=cdp.send('SystemInfo.getProcessInfo')['processInfo']
        assert {r['id'] for r in after}=={r['id'] for r in identities}
        if memory:
            hwm=sum(next(int(line.split()[1])*1024 for line in Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith('VmHWM:')) for pid in renderer_pids)
            stop.set();observer.join()
        page.evaluate('domainReader.close()')
        if memory:collect();retained=max(0,rss()-baseline)
        assert not errors,errors
        ordered=sorted(walls)
        return dict(process_identity=browser_pid,renderer_pids=renderer_pids,wall_samples_ms=walls,cpu_samples_ms=cpus,
            p50_ms=statistics.median(walls),p95_ms=ordered[int((samples-1)*.95)],p99_ms=ordered[int((samples-1)*.99)],throughput_per_second=samples*1000/sum(walls),cpu_ms_per_operation=statistics.mean(cpus),
            peak_rss_bytes=max(peak[0],hwm) if memory else None,retained_bytes=retained if memory else None,
            final_source_sha256=hashlib.sha256(final.encode()).hexdigest(),canonical_frames=samples+21,warmup_operations=20,
            profiler_enabled=False,tracemalloc_enabled=False,memory_separate_process=memory)
    finally:
        stop.set()
        if observer is not None:observer.join()
        owned.close()


def measure(browser,origin,folder,destination,samples=100,pairs=3):
    compiled,reference=bundles(folder)
    artifacts={n:hashlib.sha256(Path(p).read_bytes()).hexdigest() for n,p in compiled.items()}
    names=['benchmarks/markdown_worker/'+n for n in ('domain_reader.py','domain_reader.js','reader_measure.py')]+['benchmarks/http_cpu_clock.py','static/styles.css','frontend/src/conversation/ui/renderers/stable-dom.ts','frontend/src/conversation/ui/renderers/text-blocks.ts','node_modules/marked/lib/marked.umd.js','node_modules/dompurify/dist/purify.min.js']
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    runtime=dict(browser=browser.version,python=platform.python_version(),platform=platform.platform())
    from runtime_guards import probe_system_resources,resource_budget_manifest
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    reports={v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,compiled_sha256=artifacts,reference_sources=reference,workloads=[],limitations=['RPC dispatch included in CPU; renderer-only wall','5ms RSS and summed renderer high-water marks; may overestimate simultaneous peak','GC only in separate memory browser; one disposal interval','fixture HTTP origin; no full-app API/physical paint/math']) for v in ('reference','candidate')};seen=set()
    for case in CASES:
        fixture=hashlib.sha256(json.dumps(dict(case=case,paragraphs=160,samples=samples,viewport=[1180,900]),sort_keys=True).encode()).hexdigest();rows={}
        for variant in reports:
            row=dict(domain='markdown',case=case,scope='domain_integration',correctness_passed=True,failure_and_cleanup_passed=True,identity=dict(source_sha256=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant,artifacts=artifacts),sort_keys=True).encode()).hexdigest(),runtime=runtime,hardware=hardware,fixture_sha256=fixture),measurements=[])
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(pairs):
            values={}
            for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                assert artifacts=={n:hashlib.sha256(Path(p).read_bytes()).hexdigest() for n,p in compiled.items()}
                value=run(browser,origin,compiled,variant,case,samples,False);memory=run(browser,origin,compiled,variant,case,samples,True)
                for r in (value,memory):assert r['process_identity'] not in seen;seen.add(r['process_identity'])
                assert value['final_source_sha256']==memory['final_source_sha256'] and value['canonical_frames']==memory['canonical_frames']
                value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],memory_process_identity=memory['process_identity'],memory_separate_process=True);values[variant]=value
            assert values['reference']['final_source_sha256']==values['candidate']['final_source_sha256']
            for variant in reports:
                rows[variant]['measurements'].append(values[variant]);Path(str(destination)+'-'+variant+'.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
    verdict=compare(reports['reference'],reports['candidate']);Path(str(destination)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
    return reports

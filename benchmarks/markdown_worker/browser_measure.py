"""Independent browser-process inline-cache comparison through production DOM.

CPU has a separate repeated interval; it is a mean, not fabricated per-operation
CPU samples. Full application/input-paint and strict CPU admission remain gaps.
"""
import hashlib
import json
import os
import platform
from pathlib import Path
import statistics
import sys
import threading

from tests._runtime_sections import ROOT, native_module_path


def run(browser, origin, variant, case, *, samples=100, memory=False):
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']}
    executable = os.environ.get('TOFU_E2E_EXECUTABLE_PATH')
    if executable:
        options['executable_path'] = executable
    owned = browser.browser_type.launch(**options)
    stop = threading.Event(); sampler = None; peak = [0]; observations = [0]; sampler_errors = []
    try:
        page = owned.new_page(viewport={'width': 1180, 'height': 900})
        page.goto(origin, wait_until='load')
        for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js',
                       'benchmarks/markdown_worker/inline_cache.js'):
            page.add_script_tag(path=str(ROOT/source))
        for name, source in (('measure-policy', 'frontend/src/markdown-policy.ts'),
                             ('measure-renderer', 'frontend/src/markdown/index.ts'),
                             ('measure-blocks', 'frontend/src/conversation/ui/renderers/text-blocks.ts')):
            page.add_script_tag(path=native_module_path(f'.native/inline-{name}.js', ROOT/source))
        session = owned.new_browser_cdp_session()
        def processes():
            return session.send('SystemInfo.getProcessInfo')['processInfo']
        page.evaluate(r'''({variant,caseName})=>{
          const runtime=new marked.Marked();installMarkdownPolicy(runtime);
          let epoch=0;const started=performance.now();
          const cache=variant==='inline'?createInlineCacheExperiment({ownerId:900001,maximumBytes:1024*1024,
            getEpoch:()=>epoch,createLexer:()=>new runtime.Lexer(runtime.defaults),renderTokens:t=>runtime.parser(t)}):null;
          const parser=cache?{parse:text=>cache.parse(900001,text)}:runtime;
          const environment={document,parser,sanitizer:DOMPurify,highlighter:null,mathRenderer:null,basePath:'',projectActive:false};
          const renderer=createMarkdownRenderer({maximumCacheEntries:30,getEnvironment:()=>environment,ensureMath(){}});
          const node=document.createElement('section');document.body.append(node);
          const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
          const newline=String.fromCharCode(10), separator=newline+newline;
          const fixtures={short:'Small paragraph ',
            prose:Array.from({length:64},(_,i)=>'Paragraph '+i+': '+'reader evidence 中文 '.repeat(32)).join(separator)+separator+'Tail ',
            code:Array.from({length:32},(_,i)=>['```js',('const value_'+i+'='+i+';'+newline).repeat(8),'```'].join(newline)).join(separator)+separator+'Tail ',
            references:['[reference]','','| a | b |','| --- | --- |','| first | second |','',''].join(newline).repeat(32)+'Tail ',
            terminal:('Terminal paragraph with **markup**.'+separator).repeat(64)};
          const terminal=caseName==='terminal', captured=[];let cpuCount=0;
          function text(index,version) {
            return fixtures[caseName]+String(index+version)+(caseName==='references'&&index>=60?separator+'[reference]: /source':'');
          }
          function render(index,version) {
            const source=text(index,version);
            renderTextBlock(node,{kind:'text',blockId:'fixture',markdown:source,terminal},ports);
            node.getBoundingClientRect();return source;
          }
          globalThis.__inlineMeasure={startupMs:performance.now()-started,
            warm(version){epoch++;renderer.clearCache();for(let i=0;i<20;i++)render(i,version);},
            latency(count,capture){const raw=[];for(let i=20;i<20+count;i++){
              const began=performance.now();const source=render(i,0);raw.push(performance.now()-began);
              if(capture)captured.push({source,html:node.querySelector('.md-content').innerHTML});
            }return {raw,finalSource:text(19+count,0)};},
            cpu(count){cpuCount=count;for(let i=20;i<20+count;i++)render(i,1000);return count;},
            oracle(){const canonical=new marked.Marked();installMarkdownPolicy(canonical);
              const finalCpu={source:text(19+cpuCount,1000),html:node.querySelector('.md-content').innerHTML};
              environment.parser=canonical;renderer.clearCache();
              for(const frame of [...captured,finalCpu]){const expected=document.createElement('div');
                expected.innerHTML=renderer.render(frame.source,{streaming:!terminal});
                if(expected.innerHTML!==frame.html)throw Error('inline DOM differs from canonical');
              }return captured.length;},
            dispose(){renderer.dispose();cache?.dispose(900001);node.remove();},
          };
        }''', {'variant': variant, 'caseName': case})
        page.evaluate('__inlineMeasure.warm(0)')
        initial = processes()
        renderer_ids = [int(row['id']) for row in initial if row['type'] == 'renderer']
        browser_id = next(int(row['id']) for row in initial if row['type'] == 'browser')
        if memory:
            import psutil
            def sample():
                while not stop.is_set():
                    try:
                        peak[0] = max(peak[0], sum(psutil.Process(pid).memory_info().rss for pid in renderer_ids))
                        observations[0] += 1
                    except psutil.NoSuchProcess as error:
                        sampler_errors.append(str(error));return
                    stop.wait(.005)
            sampler = threading.Thread(target=sample, daemon=True); sampler.start()
        result = page.evaluate('count=>__inlineMeasure.latency(count, '+('false' if memory else 'true')+')', samples)
        stop.set()
        if sampler:
            sampler.join(timeout=2); assert not sampler.is_alive()
            assert observations[0] > 0
            assert not sampler_errors, 'sampled renderer exited during memory cohort'
        assert renderer_ids == [int(row['id']) for row in processes() if row['type'] == 'renderer']
        # Reset/rewarm with fresh source versions. No timers, oracle captures,
        # CDP queries or RSS probes execute inside the repeated CPU interval.
        page.evaluate('__inlineMeasure.warm(1000)')
        before = processes()
        assert page.evaluate('count=>__inlineMeasure.cpu(count)', samples) == samples
        after = processes()
        assert {row['id'] for row in before} == {row['id'] for row in after}
        cpu = (sum(row['cpuTime'] for row in after)-sum(row['cpuTime'] for row in before))*1000/samples
        assert page.evaluate('__inlineMeasure.oracle()') == (0 if memory else samples)
        raw = result['raw']; ordered = sorted(raw)
        return {'browser_pid': browser_id, 'renderer_pids': renderer_ids,
                'p50_ms': statistics.median(raw), 'p95_ms': ordered[int((len(raw)-1)*.95)],
                'p99_ms': ordered[int((len(raw)-1)*.99)], 'wall_samples_ms': raw,
                'throughput_per_second': len(raw)*1000/sum(raw),
                'cpu_ms_per_operation_separate_interval': cpu,
                'cpu_final_dom_verified': True, 'latency_oracle_frames': 0 if memory else samples,
                'sampled_peak_renderer_rss_bytes': peak[0] if memory else None,
                'rss_samples': observations[0], 'startup_ms': page.evaluate('__inlineMeasure.startupMs'),
                'final_source_sha256': hashlib.sha256(result['finalSource'].encode()).hexdigest()}
    finally:
        stop.set()
        if sampler:
            sampler.join(timeout=2)
        try:
            if 'page' in locals() and not page.is_closed():
                page.evaluate('globalThis.__inlineMeasure?.dispose()')
        finally:
            owned.close()


def measure(browser, origin, destination, *, samples=100, pairs=3, cases=None):
    sources = ('benchmarks/markdown_worker/browser_measure.py', 'benchmarks/markdown_worker/inline_cache.js',
               'frontend/src/markdown-policy.ts', 'frontend/src/markdown/index.ts',
               'frontend/src/conversation/ui/renderers/text-blocks.ts',
               'node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js',
               'static/styles.css')
    sources += tuple(str(path.relative_to(ROOT)) for path in sorted((ROOT/'frontend/src/markdown').glob('*.ts')))
    digests = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete', 'browser': browser.version,
              'python': sys.version, 'platform': platform.platform(), 'affinity': sorted(os.sched_getaffinity(0)),
              'sources': digests, 'samples': samples, 'cases': [],
              'limitations': ['CPU is one separate cohort mean, not per-operation raw CPU admission',
                              'memory separate browser; renderer RSS every5ms, short peaks may be missed',
                              'memory cohort retains no captured oracle frame strings',
                              'startup has only paired diagnostics; no cold/input-paint/full-app/CSP admission']}
    for case in cases or ('short', 'prose', 'code', 'references', 'terminal'):
        cohorts = []; report['cases'].append({'case': case, 'pairs': cohorts})
        for pair in range(pairs):
            values = {}
            for variant in (('main', 'inline') if pair%2 == 0 else ('inline', 'main')):
                for name, digest in digests.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                result = run(browser, origin, variant, case, samples=samples)
                memory = run(browser, origin, variant, case, samples=samples, memory=True)
                for name, digest in digests.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                assert result['browser_pid'] != memory['browser_pid']
                assert result['final_source_sha256'] == memory['final_source_sha256']
                result['memory_browser_pid'] = memory['browser_pid']
                result['sampled_peak_renderer_rss_bytes'] = memory['sampled_peak_renderer_rss_bytes']
                result['rss_samples'] = memory['rss_samples']; values[variant] = result
            assert values['main']['final_source_sha256'] == values['inline']['final_source_sha256']
            cohorts.append(values);Path(destination).write_text(json.dumps(report, indent=2)+'\n')
    return report

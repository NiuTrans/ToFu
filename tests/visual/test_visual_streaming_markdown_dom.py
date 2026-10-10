"""Real production text rendering preserves readers during stream growth."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def test_streaming_keeps_prefix_selection_and_matches_full_render(frontend_visual_page):
    page = frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js',
                   'node_modules/dompurify/dist/purify.min.js'):
        page.add_script_tag(path=str(ROOT / source))
    for name, source in (
        ('markdown', 'frontend/src/markdown/index.ts'),
        ('policy', 'frontend/src/markdown-policy.ts'),
        ('text', 'frontend/src/conversation/ui/renderers/text-blocks.ts'),
    ):
        page.add_script_tag(path=native_module_path(f'.native/streaming-reader-{name}.js', ROOT / source))
    result = page.evaluate(r"""() => {
      installMarkdownPolicy(marked);
      const renderer=createMarkdownRenderer({maximumCacheEntries:300,
        getEnvironment:()=>({document,parser:marked,sanitizer:DOMPurify,
          highlighter:null,mathRenderer:null,basePath:'',projectActive:false}),ensureMath(){}});
      const node=document.createElement('section');node.className='conversation-text-block';
      document.body.append(node);
      const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
      const before='Stable reader paragraph.\n\nCurrent streaming paragraph.';
      const block={kind:'text',blockId:'stream',markdown:before,terminal:false};
      renderTextBlock(node,block,ports);
      const body=node.querySelector('.md-content'),first=body.firstElementChild;
      const text=body.lastElementChild.firstChild;
      const selection=getSelection(),range=document.createRange();
      range.setStart(text,0);range.setEnd(text,7);selection.removeAllRanges();selection.addRange(range);
      renderTextBlock(node,{...block,markdown:before+' More evidence.'},ports);
      const retained=first===body.firstElementChild,selected=selection.toString();
      const textRetained=text===body.lastElementChild.firstChild;
      const fixtures=[
        'Stable reader paragraph.\n\nCurrent **rich** paragraph.',
        'Stable reader paragraph.\n\n```ts\nconst value=1;\n',
        'Stable reader paragraph.\n\n```ts\nconst value=1;\nconst next=2;\n```\n\nEnd.',
        '[reference]\n\nTail.', '[reference]\n\nTail.\n\n[reference]: /source',
        '| Column | Value |\n| --- | --- |\n| a | b |\n\nTail.',
        'Edited prefix.\n\nTail.',
      ];
      let equal=true;
      for(const markdown of fixtures){
        renderTextBlock(node,{...block,markdown},ports);
        const oracle=document.createElement('div');
        oracle.innerHTML=renderer.render(markdown,{streaming:true});
        equal &&= body.innerHTML===oracle.innerHTML;
      }
      renderTextBlock(node,{...block,markdown:fixtures.at(-1),terminal:true},ports);
      const final=document.createElement('div');final.innerHTML=renderer.render(fixtures.at(-1));
      const terminalEqual=body.innerHTML===final.innerHTML;
      const thinking=document.createElement('section');document.body.append(thinking);
      const reasoning={kind:'thinking',blockId:'reasoning',markdown:before,terminal:false};
      renderThinkingBlock(thinking,reasoning,ports);
      const reasoningBody=thinking.querySelector('.md-content'),reasoningPrefix=reasoningBody.firstElementChild;
      renderThinkingBlock(thinking,{...reasoning,markdown:before+' More evidence.'},ports);
      const thinkingRetained=reasoningPrefix===reasoningBody.firstElementChild;
      disposeThinkingBody(thinking);thinking.remove();
      renderer.dispose();node.remove();selection.removeAllRanges();
      return {retained,selected,textRetained,equal,terminalEqual,thinkingRetained};
    }""")
    assert result == {'retained': True, 'selected': 'Current', 'textRetained': True,
                      'equal': True, 'terminalEqual': True, 'thinkingRetained': True}


def test_streaming_renderer_paired_performance(browser, frontend_visual_origin, tmp_path):
    """Opt-in measurement uses fresh browser processes, never a pass claim."""
    destination = os.environ.get('TOFU_STREAMING_PERF_OUTPUT')
    if not destination:
        pytest.skip('set TOFU_STREAMING_PERF_OUTPUT for raw performance measurements')
    if browser.browser_type.name != 'chromium':
        pytest.skip('CDP renderer CPU/heap measurements require Chromium')
    import psutil
    reference_source = subprocess.check_output(
        ['git', 'show', '77d822e282aa6ed1415fa4db9816d6a3b30afc50:frontend/src/conversation/ui/renderers/text-blocks.ts'],
        cwd=ROOT, text=True)
    owner = ROOT / 'frontend/src/conversation/ui/renderers/text-blocks.ts'
    rewritten = re.sub(r"from (['\"])(\.[^'\"]+)\1",
                       lambda match: 'from ' + json.dumps(str((owner.parent / match[2]).resolve())),
                       reference_source)
    baseline = tmp_path / 'reference-text-blocks.ts'
    baseline.write_text(rewritten)
    bundles = {
        'markdown': native_module_path('.native/perf-markdown.js', ROOT / 'frontend/src/markdown/index.ts'),
        'policy': native_module_path('.native/perf-markdown-policy.js', ROOT / 'frontend/src/markdown-policy.ts'),
        'candidate': native_module_path('.native/perf-stream-current.js', owner),
        'reference': native_module_path('.native/perf-stream-reference.js', baseline),
    }
    result = {'format': 'tofu.streaming-renderer-experiment/v1',
              'scope': 'renderTextBlock plus forced layout; no backend/SSE claim',
              'whole_system_verdict': 'incomplete', 'browser': browser.version,
              'sources': {'reference': hashlib.sha256(reference_source.encode()).hexdigest(),
                          'candidate': hashlib.sha256(owner.read_bytes()).hexdigest(),
                          'reconciliation': hashlib.sha256((owner.parent / 'stable-dom.ts').read_bytes()).hexdigest()},
              'workloads': []}

    def run(variant, case, memory=False):
        owned = browser.browser_type.launch(headless=True, args=[
            '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
        stopped = threading.Event()
        observer = None
        rss = []
        errors = []
        try:
            page = owned.new_page(viewport={'width': 1180, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(frontend_visual_origin, wait_until='load')
            for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js'):
                page.add_script_tag(path=str(ROOT / source))
            for key in ('markdown', 'policy', variant):
                page.add_script_tag(path=bundles[key])
            page.evaluate('document.fonts.ready')
            browser_cdp = owned.new_browser_cdp_session()
            process_ids = [int(row['id']) for row in browser_cdp.send('SystemInfo.getProcessInfo')['processInfo']
                           if row['type'] == 'renderer']
            if not process_ids:
                raise AssertionError('missing owned renderer process')
            if memory:
                def sample():
                    while not stopped.is_set():
                        try:
                            rss.append(sum(psutil.Process(pid).memory_info().rss for pid in process_ids))
                        except psutil.NoSuchProcess:
                            # Owned browser teardown can retire a renderer.
                            break
                        stopped.wait(.005)
                observer = threading.Thread(target=sample, name='stream-perf-memory-observer')
                observer.start()
            cdp = page.context.new_cdp_session(page)
            cdp.send('Performance.enable')
            page.evaluate(r"""({caseName}) => {
              installMarkdownPolicy(marked);
              const renderer=createMarkdownRenderer({maximumCacheEntries:300,
                getEnvironment:()=>({document,parser:marked,sanitizer:DOMPurify,
                  highlighter:null,mathRenderer:null,basePath:'',projectActive:false}),ensureMath(){},
                reportFailure(error){throw error;}});
              const node=document.createElement('section');node.className='conversation-text-block';
              document.body.append(node);
              const prefix=caseName==='code'
                ? Array.from({length:32},(_,i)=>`Paragraph ${i}.\n\n\`\`\`js\nconst value${i}=1;\n\`\`\`\n\n`).join('')
                : caseName==='tables'
                  ? Array.from({length:32},(_,i)=>`| Field | Value |\n| --- | --- |\n| row ${i} | data |\n\n`).join('')
                  : Array.from({length:160},(_,i)=>`Paragraph ${i}: **retained evidence** with 中文文本 and a [source](/source).\n\n`).join('');
              const tail=caseName==='code'?'\`\`\`js\n':'Current paragraph. ';
              const frames=Array.from({length:120},(_,i)=>prefix+tail+(caseName==='code'?'const next=2;\n':'More evidence. ').repeat(i+1));
              const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
              const block={kind:caseName==='thinking'?'thinking':'text',blockId:'stream',terminal:false};
              const renderBlock=caseName==='thinking'?renderThinkingBlock:renderTextBlock;
              window.measureStream=()=>{
                const raw=[];
                for(let i=0;i<frames.length;i++){
                  const started=performance.now();renderBlock(node,{...block,markdown:frames[i]},ports);
                  node.getBoundingClientRect();
                  if(i>=20)raw.push(performance.now()-started);
                }
                return raw;
              };
              window.streamHtml=()=>node.querySelector('.md-content').innerHTML;
            }""", {'caseName': case})
            metrics = lambda: {row['name']: row['value'] for row in cdp.send('Performance.getMetrics')['metrics']}
            before = metrics()
            raw = page.evaluate('measureStream()')
            after = metrics()
            html = page.evaluate('streamHtml()')
            cdp.send('HeapProfiler.collectGarbage')
            heap = metrics()['JSHeapUsedSize']
            assert not errors, errors
            ordered = sorted(raw)
            return {'process_identities': process_ids, 'wall_samples_ms': raw,
                    'p50_ms': (ordered[49] + ordered[50]) / 2,
                    'p95_ms': ordered[94], 'p99_ms': ordered[98],
                    'cpu_ms_per_operation': (after['TaskDuration'] - before['TaskDuration']) * 1000 / 120,
                    'cpu_includes_warmup': True, 'retained_js_heap_bytes': heap,
                    'peak_renderer_rss_bytes': max(rss) if memory and rss else None,
                    'html_sha256': hashlib.sha256(html.encode()).hexdigest()}
        finally:
            stopped.set()
            if observer:
                observer.join(timeout=5)
                assert not observer.is_alive(), 'memory observer did not stop'
            owned.close()

    for case in ('prose', 'code', 'tables', 'thinking'):
        row = {'case': case, 'pairs': []}
        for pair in range(3):
            order = ('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')
            measurements = {variant: run(variant, case) for variant in order}
            assert measurements['reference']['html_sha256'] == measurements['candidate']['html_sha256']
            for variant in order:
                memory = run(variant, case, memory=True)
                measurements[variant]['peak_renderer_rss_bytes'] = memory['peak_renderer_rss_bytes']
                measurements[variant]['retained_js_heap_bytes'] = memory['retained_js_heap_bytes']
                measurements[variant]['memory_process_identities'] = memory['process_identities']
            row['pairs'].append(measurements)
        result['workloads'].append(row)
    Path(destination).write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')

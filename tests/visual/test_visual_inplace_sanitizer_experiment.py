"""Real browser exact canonical sanitizer/renderer comparisons, including hostile HTML."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('variant', ['fragment', 'document'])
def test_inplace_sanitizer_exact_browser_oracle(frontend_visual_page, variant):
    page = frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js',
                   'benchmarks/markdown_worker/inplace_sanitizer.js'):
        page.add_script_tag(path=str(ROOT/source))
    for name, source in (('policy', 'frontend/src/markdown-policy.ts'), ('renderer', 'frontend/src/markdown/index.ts')):
        page.add_script_tag(path=native_module_path('.native/inplace-'+name+'.js', ROOT/source))
    result = page.evaluate(r'''variant => {
      const parser=new marked.Marked();installMarkdownPolicy(parser);
      const factory=variant==='fragment'?createInplaceSanitizerExperiment:createInplaceDocumentSanitizerExperiment;
      const candidate=factory({document,sanitizer:DOMPurify});
      let active=DOMPurify;
      const renderer=createMarkdownRenderer({maximumCacheEntries:30,
        getEnvironment:()=>({document,parser,sanitizer:active,highlighter:null,
          mathRenderer:null,basePath:'/fixture',projectActive:false}),ensureMath(){}});
      const fixtures=['plain 中文 😀 **bold**','[link](/api/source) ![image](/uploads/image)',
        '<script>window.__inplaceXss=1</script><img src=x onerror="window.__inplaceXss=1">',
        '<svg onload="window.__inplaceXss=1"><foreignObject><p>mixed</p></foreignObject></svg>',
        '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=1 onerror=window.__inplaceXss=1>">',
        '<form id=attributes><input name=parentNode><input name=removeChild></form>',
        '<html><head><title>Title</title><style>bad</style></head><body><p>Body</p></body></html>',
        '<table><div>foster text</div><tr><td>cell</td></tr></table>',
        '<pre><code class="language-js">const value=1;</code></pre>',
        '<p title="</pre>">literal</p><pre><pre><code>nested</code></pre></pre>',
        '[unsafe](javascript:alert(1)) <a href="data:text/html,evil">data</a>',
        '[reference]\n\nTail\n\n[reference]: /source',
        '| name | value |\n| --- | --- |\n| a | b |',
        '- [ ] open\n- [x] done','$$a+b$$ and $c+d$','~~strict~~ and ~literal~',
        '  <p>leading whitespace</p>','\n\n<p>leading lines</p>',
        '<noscript><img src=x onerror="window.__inplaceXss=1"></noscript>',
        '<template><p>template</p><script>window.__inplaceXss=1</script></template>'];
      const failures=[];let comparisons=0;
      try {
        for (const full of fixtures) {
          const fragments=[full];for(let end=1;end<full.length;end+=3)fragments.push(full.slice(0,end));
          for (const source of fragments) for(const streaming of [true,false]) for(const escapeRawHtml of [false,true]) {
            active=DOMPurify;const reference=renderer.render(source,{streaming,escapeRawHtml});
            active=candidate;const actual=renderer.render(source,{streaming,escapeRawHtml});
            comparisons++;
            const a=document.createElement('div'),b=document.createElement('div');
            a.innerHTML=reference;b.innerHTML=actual;
            if(a.innerHTML!==b.innerHTML && failures.length<12)failures.push({source,streaming,escapeRawHtml,reference,actual});
            if(candidate.snapshot().retainedNodes)throw Error('scratch retained parsed nodes');
            if(b.querySelector('script,[onerror],[onload]'))throw Error('executable markup survived candidate');
          }
        }
        candidate.dispose();let denied=false;try{candidate.sanitize('late');}catch(error){denied=true;}
        return {comparisons,failures,closed:denied&&candidate.snapshot().closed,retainedNodes:candidate.snapshot().retainedNodes};
      } finally {renderer.dispose();candidate.dispose();}
    }''', variant)
    destination=os.environ.get('TOFU_INPLACE_SANITIZER_CHECK_OUTPUT')
    if destination:
        sources=('benchmarks/markdown_worker/inplace_sanitizer.js','tests/visual/test_visual_inplace_sanitizer_experiment.py',
                 'frontend/src/markdown-policy.ts','frontend/src/markdown/index.ts','node_modules/dompurify/dist/purify.min.js')
        output=Path(destination);output=output.with_name(output.stem+'-'+variant+output.suffix)
        output.write_text(json.dumps({'scope':__doc__,'variant':variant,'whole_system_verdict':'incomplete',
            'performance_admission':False,'browser':page.context.browser.version,'checks':result,
            'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}},indent=2)+'\n')
    assert result['comparisons']>300 and result['closed'] and result['retainedNodes']==0
    if variant=='fragment':
        # This variant is explicitly rejected, not made compatible by ignoring
        # its failures. Pin the real head/body semantic counterexample.
        assert any('<head>' in f['source'] and '<style>' in f['actual'] for f in result['failures'])
    else:
        assert result['failures']==[],result['failures']


def test_inplace_document_measurement_smoke(browser, frontend_visual_origin, tmp_path):
    from benchmarks.markdown_worker.inplace_measure import measure
    report=measure(browser, frontend_visual_origin, tmp_path/'smoke.json', samples=2, pairs=1)
    assert len(report['cases'])==5
    for row in report['cases']:
        for value in row['pairs'][0].values():
            assert value['latency_oracle_frames']==2 and len(value['wall_samples_ms'])==2
            assert value['rss_samples']>0 and value['browser_pid']!=value['memory_browser_pid']


def test_inplace_document_full_measurement(browser, frontend_visual_origin):
    destination=os.environ.get('TOFU_INPLACE_SANITIZER_PERF_OUTPUT')
    if not destination:
        pytest.skip('set TOFU_INPLACE_SANITIZER_PERF_OUTPUT for complete browser pairs')
    from benchmarks.markdown_worker.inplace_measure import measure
    measure(browser, frontend_visual_origin, destination)

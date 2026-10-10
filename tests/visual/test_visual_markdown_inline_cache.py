"""Real renderer/sanitizer/layout oracle for the offline inline-lexer candidate.

Own local HTTP origin and browser page; no product parser default is changed.
Performance and full application reader/CSP journeys remain separate gates.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa:F401

pytestmark = pytest.mark.visual


def test_inline_cache_actual_renderer_sanitizer_layout_and_lifecycle(frontend_visual_page):
    page = frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js',
                   'node_modules/dompurify/dist/purify.min.js',
                   'benchmarks/markdown_worker/inline_cache.js'):
        page.add_script_tag(path=str(ROOT/source))
    for name, source in (('inline-policy', 'frontend/src/markdown-policy.ts'),
                         ('inline-renderer', 'frontend/src/markdown/index.ts')):
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT/source))
    result = page.evaluate(r'''() => {
      const reference=new marked.Marked(), candidate=new marked.Marked();
      if (!installMarkdownPolicy(reference) || !installMarkdownPolicy(candidate)) throw Error('policy not installed');
      let epoch=0, active=reference, comparisons=0;
      const cache=createInlineCacheExperiment({ownerId:900001,maximumBytes:1024*1024,getEpoch:()=>epoch,
        createLexer:()=>new candidate.Lexer(candidate.defaults),renderTokens:tokens=>candidate.parser(tokens)});
      const proxy={parse(source){return cache.parse(900001,source);}};
      const renderer=createMarkdownRenderer({maximumCacheEntries:30,
        getEnvironment:()=>({document,parser:active,sanitizer:DOMPurify,highlighter:null,
          mathRenderer:null,basePath:'/fixture',projectActive:false}),ensureMath(){}});
      const container=document.createElement('div');document.body.append(container);
      const newline=String.fromCharCode(10);
      const fixtures=['plain 中文 paragraph','~~strict~~ and ~literal~',
        ['[reference]','','Tail.'],['[reference]','','Tail.','','[reference]: /first "title"'],
        ['[reference]','','Tail.','','[reference]: /changed'],
        ['- [ ] open','- [x] done','','  continuation',''],
        ['> quote **bold**','> nested [reference]','','[reference]: /source'],
        ['```js','const x="<script>";',''],['```js','const x=1;','```','','Next.'],
        ['| name | value |','| --- | --- |','| a | 82'+String.fromCharCode(92)+'|181 |'],
        '<script>window.__inlineXss=1</script><img src=x onerror="window.__inlineXss=1">',
        '[unsafe](javascript:alert(1)) <svg onload="window.__inlineXss=1"></svg>',
        '$a+b$ and $$c+d$$','**unfinished','[unfinished](']
        .map(value=>Array.isArray(value)?value.join(newline):value);
      function compare(source,escaped,streaming=true) {
        active=reference;const expected=renderer.render(source,{streaming,escapeRawHtml:escaped});
        container.innerHTML=expected;const height=container.getBoundingClientRect().height;
        active=proxy;const actual=renderer.render(source,{streaming,escapeRawHtml:escaped});
        container.innerHTML=actual;const actualHeight=container.getBoundingClientRect().height;
        if (actual!==expected || actualHeight!==height) throw Error('renderer/layout mismatch');
        if (container.querySelector('script,[onerror],[onload]')) throw Error('unsafe executable DOM');
        if (Array.from(container.querySelectorAll('a')).some(a=>(a.getAttribute('href')||'').trimStart().toLowerCase().startsWith('javascript:')))
          throw Error('unsafe navigation');
        comparisons++;
      }
      try {
        for (const text of fixtures) for (const escaped of [false,true]) {
          compare(text,escaped);compare(text,escaped);compare(text,escaped,false);
          for (let end=1;end<text.length;end+=3) compare(text.slice(0,end),escaped);
        }
        const repeated=('same **bold** paragraph'+newline+newline).repeat(50);
        compare(repeated,false);compare(repeated,false);
        const reused=cache.snapshot(900001).hits>0;
        reference.setOptions({breaks:false});candidate.setOptions({breaks:false});epoch++;
        compare(['line one','line two'].join(newline),false);
        const snapshot=cache.snapshot(900001);cache.dispose(900001);
        let closed=false;try{cache.parse(900001,'late parse');}catch(error){closed=true;}
        return {comparisons,reused,bounded:snapshot.retainedBytes<=1024*1024,
          reclaimed:cache.snapshot(900001).retainedBytes===0,closed,xss:window.__inlineXss===undefined};
      } finally { renderer.dispose();cache.dispose(900001);container.remove(); }
    }''')
    assert result['comparisons'] > 200
    assert all(result[key] for key in ('reused', 'bounded', 'reclaimed', 'closed', 'xss'))
    destination = os.environ.get('TOFU_MARKDOWN_INLINE_CHECK_OUTPUT')
    if destination:
        sources = ('benchmarks/markdown_worker/inline_cache.js',
                   'tests/visual/test_visual_markdown_inline_cache.py',
                   'frontend/src/markdown-policy.ts', 'frontend/src/markdown/index.ts',
                   'node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js')
        Path(destination).write_text(json.dumps({
            'scope': 'actual production renderer/sanitizer and forced layout on owned local origin',
            'whole_system_verdict': 'incomplete', 'performance_admission': False,
            'browser': page.context.browser.version, 'checks': result,
            'sources': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
        }, indent=2)+'\n')


def test_inline_cache_measurement_smoke(browser, frontend_visual_origin, tmp_path):
    from benchmarks.markdown_worker.browser_measure import measure
    report = measure(browser, frontend_visual_origin, tmp_path/'inline-smoke.json',
                     samples=2, pairs=1, cases=('prose',))
    values = report['cases'][0]['pairs'][0]
    identities = []
    for value in values.values():
        assert len(value['wall_samples_ms']) == 2
        assert value['rss_samples'] > 0
        assert value['sampled_peak_renderer_rss_bytes'] > 0
        identities.extend((value['browser_pid'], value['memory_browser_pid']))
    assert len(set(identities)) == 4
    assert report['whole_system_verdict'] == 'incomplete'


def test_inline_cache_paired_browser_measurement(browser, frontend_visual_origin):
    destination = os.environ.get('TOFU_MARKDOWN_INLINE_MEASURE_OUTPUT')
    if not destination:
        pytest.skip('opt-in independent-process inline cache performance measurement')
    from benchmarks.markdown_worker.browser_measure import measure
    measure(browser, frontend_visual_origin, destination)

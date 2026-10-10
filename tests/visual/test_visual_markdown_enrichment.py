"""Real selection/focus behavior across delayed optional Markdown libraries.

Synthetic ports hold library completion; parser/sanitizer and renderer are real.
No backend state or durable conversation is written.
"""
import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('dispose', [False, True])
def test_optional_enrichment_respects_reader_selection_and_disposal(frontend_visual_page, dispose):
    page = frontend_visual_page
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='enrichment-fixture';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<html><body><main class="md-content"></main></body></html>')
    for source in ['node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js']:
        frame.add_script_tag(path=str(ROOT / source))
    frame.add_script_tag(path=native_module_path('.native/markdown-enrichment.js', ROOT / 'frontend/src/markdown/index.ts'))
    frame.evaluate(r"""() => {
      window.libraryReady=false;window.activeListeners=new Set();
      const add=document.addEventListener.bind(document),remove=document.removeEventListener.bind(document);
      document.addEventListener=(type,handler,...args)=>{
        if(type==='selectionchange')activeListeners.add(handler);add(type,handler,...args);
      };
      document.removeEventListener=(type,handler,...args)=>{
        if(type==='selectionchange')activeListeners.delete(handler);remove(type,handler,...args);
      };
      const escape=text=>text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const grammar=new Promise(resolve=>window.releaseGrammar=resolve);
      const math=new Promise(resolve=>window.releaseMath=resolve);
      window.environment={document,parser:marked,sanitizer:DOMPurify,basePath:'',projectActive:false,
        mathRenderer:null,highlighter:{getLanguage:()=>libraryReady,
          highlight:text=>({value:'<span class="highlighted">'+escape(text)+'</span>'}),
          highlightAuto:text=>({value:escape(text)})}};
      window.renderer=createMarkdownRenderer({getEnvironment:()=>({...environment}),maximumCacheEntries:8,
        ensureHighlightLanguage:()=>grammar,ensureMath:()=>math});
      document.querySelector('main').innerHTML=renderer.render('```rust\nlet value = 1;\n```\n\nEquation $x+1$.');
      window.copy=document.querySelector('.copy-btn');copy.focus();
      const code=document.querySelector('pre code'),mathCode=document.querySelector('.math-pending');
      const range=document.createRange();range.setStart(code.firstChild,0);range.setEnd(mathCode.firstChild,3);
      const selection=document.getSelection();selection.removeAllRanges();selection.addRange(range);
      window.selected=selection.toString();
    }""")
    if dispose:
        frame.evaluate('renderer.dispose()')
    frame.evaluate("""async () => {
      libraryReady=true;
      environment.mathRenderer={renderToString:tex=>'<span class="typeset">'+tex+'</span>'};
      releaseGrammar(true);releaseMath();
      await new Promise(resolve=>setTimeout(resolve,0));
    }""")
    assert frame.evaluate('document.getSelection().toString()===selected')
    assert frame.evaluate('document.activeElement===copy')
    assert frame.locator('.highlighted').count() == 0
    assert frame.locator('.typeset').count() == 0
    assert frame.evaluate('activeListeners.size') == (0 if dispose else 1)
    frame.evaluate('document.getSelection().removeAllRanges()')
    if not dispose:
        frame.wait_for_function('document.querySelector(".highlighted") && document.querySelector(".typeset")')
    else:
        assert frame.locator('.highlighted').count() == 0
        assert frame.locator('.math-pending').count() == 1
    assert frame.evaluate('document.activeElement===copy')
    assert frame.evaluate('activeListeners.size') == 0
    frame.evaluate('renderer.dispose()')

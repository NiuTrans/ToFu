"""Real browser selection, manual viewport and typing during production rendering.

Owned local HTTP fixture uses shipped CSS and unchanged renderer/sanitizer.
This is interaction semantics, not input-to-paint timing or full app/backend/CSP.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark=pytest.mark.visual


def test_selection_viewport_and_typing_survive_stream_growth(frontend_visual_page):
    page=frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js','node_modules/dompurify/dist/purify.min.js'):
        page.add_script_tag(path=str(ROOT/source))
    for name,source in (('markdown','frontend/src/markdown/index.ts'),
                        ('policy','frontend/src/markdown-policy.ts'),
                        ('text','frontend/src/conversation/ui/renderers/text-blocks.ts')):
        page.add_script_tag(path=native_module_path('.native/reader-interaction-'+name+'.js',ROOT/source))
    page.evaluate(r'''() => {
      installMarkdownPolicy(marked);
      const renderer=createMarkdownRenderer({maximumCacheEntries:300,
        getEnvironment:()=>({document,parser:marked,sanitizer:DOMPurify,
          highlighter:null,mathRenderer:null,basePath:'',projectActive:false}),ensureMath(){}});
      const input=document.createElement('input');input.id='reader-input';
      input.setAttribute('aria-label','Reader typing fixture');document.body.append(input);
      const scroller=document.createElement('div');scroller.style.cssText='height:240px;width:720px;overflow:auto;position:relative';
      const node=document.createElement('section');node.className='conversation-text-block';
      scroller.append(node);document.body.append(scroller);
      const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
      const prefix=Array.from({length:40},(_,index)=>`Paragraph ${index}: reader 中文 😀 **stable emphasis** and evidence.`).join('\n\n')+'\n\n';
      const block={kind:'text',blockId:'interaction-stream',markdown:prefix+'Tail.',terminal:false};
      renderTextBlock(node,block,ports);
      const body=node.querySelector('.md-content'),target=body.children[15];
      target.scrollIntoView({block:'center'});
      const range=document.createRange();range.selectNodeContents(target);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      const expectedSelection=selection.toString(),initialTop=scroller.scrollTop;
      const initialAnchor=target.getBoundingClientRect().top;
      let updates=0,interval=null,markdown=block.markdown;
      function advance(index,terminal=false){
        const tails=['Tail with **rich text**.','```js\nconst value=1;\n',
          '```js\nconst value=1;\n```\n\n[reference]',
          '| Name | Value |\n| --- | --- |\n| stable | 1 |\n\n[reference]: /source'];
        markdown=prefix+tails[index%tails.length]+'\n\nMore evidence '+index;
        renderTextBlock(node,{...block,markdown,terminal},ports);updates++;
      }
      window.__readerInteraction={advance,expectedSelection,initialTop,initialAnchor,
        check(){const oracle=document.createElement('div');oracle.innerHTML=renderer.render(markdown,{streaming:true});
          return {selected:getSelection().toString(),nodeRetained:target===body.children[15],
            scrollDelta:Math.abs(scroller.scrollTop-initialTop),
            anchorDelta:Math.abs(target.getBoundingClientRect().top-initialAnchor),
            domEqual:body.innerHTML===oracle.innerHTML,updates};},
        startTyping(){interval=setInterval(()=>advance(updates),20);},
        stopTyping(){clearInterval(interval);interval=null;},
        terminal(){selection.removeAllRanges();const terminalRange=document.createRange();
          terminalRange.selectNodeContents(target);selection.addRange(terminalRange);
          const top=scroller.scrollTop,anchor=target.getBoundingClientRect().top;
          advance(updates,true);const oracle=document.createElement('div');oracle.innerHTML=renderer.render(markdown);
          return {domEqual:body.innerHTML===oracle.innerHTML,selected:selection.toString(),
            nodeRetained:target===body.children[15],scrollDelta:Math.abs(scroller.scrollTop-top),
            anchorDelta:Math.abs(target.getBoundingClientRect().top-anchor)};},
        close(){clearInterval(interval);renderer.dispose();selection.removeAllRanges();scroller.remove();input.remove();delete window.__readerInteraction;}};
    }''')
    try:
        result=page.evaluate('''async () => {
          for(let index=0;index<40;index++){
            __readerInteraction.advance(index);
            await new Promise(resolve=>requestAnimationFrame(resolve));
          }
          return {...__readerInteraction.check(),expectedSelection:__readerInteraction.expectedSelection};
        }''')
        assert result['selected']==result['expectedSelection'] and result['selected'],result
        assert result['nodeRetained'] and result['domEqual'] and result['updates']==40
        assert result['scrollDelta']<=1 and result['anchorDelta']<=1
        page.locator('#reader-input').click()
        page.evaluate('__readerInteraction.startTyping()')
        page.keyboard.type('typing during stream',delay=8)
        page.evaluate('__readerInteraction.stopTyping()')
        assert page.locator('#reader-input').input_value()=='typing during stream'
        assert page.evaluate('document.activeElement.id')=='reader-input'
        assert page.evaluate('__readerInteraction.check().updates')>40
        terminal=page.evaluate('__readerInteraction.terminal()')
        assert terminal['domEqual'] and terminal['nodeRetained'],terminal
        assert terminal['selected']==result['expectedSelection'],terminal
        assert terminal['scrollDelta']<=1 and terminal['anchorDelta']<=1,terminal
        destination=os.environ.get('TOFU_STREAMING_READER_INTERACTION_OUTPUT')
        if destination:
            sources=('tests/visual/test_visual_streaming_reader_interactions.py',
                'frontend/src/conversation/ui/renderers/text-blocks.ts',
                'frontend/src/conversation/ui/renderers/stable-dom.ts','frontend/src/markdown/index.ts',
                'frontend/src/markdown-policy.ts','node_modules/marked/lib/marked.umd.js',
                'node_modules/dompurify/dist/purify.min.js','static/styles.css')
            Path(destination).write_text(json.dumps({'scope':__doc__,'whole_system_verdict':'incomplete',
                'browser':page.context.browser.version,'checks':result,'typing_and_terminal_passed':True,'terminal_checks':terminal,
                'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}},indent=2)+'\n')
    finally:page.evaluate('__readerInteraction.close()')

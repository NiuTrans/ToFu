"""Human reading hierarchy on shipped CSS and the actual typed renderer graph.

Synthetic turns stay inside an owned iframe; no durable conversation is written.
Markdown uses the retained production port, including code/table controls.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [('light', 1362), ('light', 1180), ('dark', 1180), ('tofu', 1180),
                                       ('light', 390), ('dark', 390), ('tofu', 390)])
def test_conversation_reading_hierarchy_and_disclosure(frontend_visual_page, tmp_path, theme, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1156 if width == 1362 else 1000})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='reading-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0;z-index:99999';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html data-theme="{theme}" lang="en"><body><div class="chat-inner">'
                      '<main id="surface"></main></div></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;'
                               'padding:var(--space-6) var(--space-4)}'
                               '.chat-inner{margin-inline:auto;min-height:0;padding:0}')
    for source in ['node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js']:
        frame.add_script_tag(path=str(ROOT / source))
    frame.evaluate(r"""() => {
      window.BASE_PATH='';window.projectState={active:false};
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }""")
    frame.add_script_tag(content=runtime_section('core/markdown.js', scope_prelude=False))
    for name, source in [('feature', 'frontend/src/conversation/index.ts'),
                         ('state', 'frontend/src/core/turn-state.ts')]:
        frame.add_script_tag(path=native_module_path(f'.native/reading-{name}.js', ROOT / source))
    frame.evaluate(r"""() => {
      const scope={taskId:'execution',attemptId:'attempt'};
      const user={turnId:'question',conversationId:'reading',actor:'human',role:'user',laneId:'main',
        ordinal:1,status:'completed',projectionRevision:1,projection:{segments:[
          {type:'text',blockId:'question-text',text:'How can we make conversation rendering easier to read?'}]}};
      const assistant={turnId:'answer',conversationId:'reading',actor:'assistant',role:'assistant',laneId:'main',
        ordinal:2,status:'completed',projectionRevision:1,projection:{segments:[
          {type:'thinking',blockId:'thought',text:'I will compare the current hierarchy and preserve the evidence needed to inspect each action.',llmRound:0,...scope},
          {type:'tool_use',blockId:'tool',id:'tool',name:'read_files',llmRound:0,...scope,
            input:{path:'frontend/src/conversation/README.md'},_round:{toolName:'read_files',toolCallId:'tool',
              roundNum:1,llmRound:0,...scope,status:'done',toolArgs:{path:'frontend/src/conversation/README.md'},
              results:[{text:'Conversation ownership and renderer contracts verified.',exitCode:0}]}},
          {type:'text',blockId:'deliverable',deliverable:true,terminal:true,text:
            '## A clearer conversation\n\nPut the answer first in the visual hierarchy. Keep the process available without letting it compete with the result.\n\n'
            +'### What changes\n\n- **Readable text:** a comfortable measure and consistent paragraph rhythm.\n'
            +'- **Inspectable work:** compact reasoning and tool disclosures.\n'
            +'- **Clear outcomes:** failures remain visible and actionable.\n\n'
            +'```ts\nconst reading = { answer: "primary", process: "inspectable" };\n```\n\n'
            +'| Content | Treatment |\n| --- | --- |\n| Final answer | Open reading column |\n| Tool evidence | Native disclosure |\n\n'
            +'The same hierarchy holds on narrow screens, with keyboard access to every control.\n\n'
            +'Read the [conversation contract](https://example.test/contract) for the details.'}]}};
      window.state={...createTurnState('reading'),turnsById:{question:user,answer:assistant},laneOrder:{main:['question','answer']}};
      const ports={renderSafeMarkdownHtml:renderMarkdown};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers(ports));
      window.render=()=>surface.render(selectConversationViewModel(state));render();
    }""")
    thinking = frame.locator('.thinking-header')
    thinking.focus()
    thinking.press('Enter')
    assert frame.locator('.thinking-text').is_visible()
    thinking.press('Enter')
    assert not frame.locator('.thinking-text').is_visible()
    thinking.evaluate('(node)=>node.blur()')
    answer = frame.locator('[data-block-id="deliverable"] .md-content')
    assert answer.is_visible()
    assert frame.locator('.conversation-turn.message').count() == 2
    assert answer.locator('.code-header .copy-btn').is_visible()
    assert answer.locator('.table-header .copy-btn').is_visible()
    geometry = frame.evaluate("""() => {
      const answer=document.querySelector('[data-block-id=deliverable] .md-content');
      const summary=document.querySelector('.thinking-header');
      const style=getComputedStyle(answer),process=getComputedStyle(summary);
      return {width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,
        answerSize:parseFloat(style.fontSize),lineHeight:parseFloat(style.lineHeight),
        processSize:parseFloat(process.fontSize),answerWidth:answer.getBoundingClientRect().width,
        answerBorder:style.borderStyle,answerBackground:style.backgroundColor,
        actions:[...document.querySelectorAll('.conversation-turn-actions button')].map(n=>n.getBoundingClientRect().height)};
    }""")
    assert geometry['scroll'] <= geometry['width'] + 1
    assert geometry['answerWidth'] > min(width - 100, 600)
    assert geometry['lineHeight'] >= geometry['answerSize'] * 1.6
    assert geometry['answerSize'] > geometry['processSize']
    typography = answer.locator('h2, h3, pre code').evaluate_all("""nodes => nodes.map(el => ({
      tag:el.tagName, size:parseFloat(getComputedStyle(el).fontSize),
      family:getComputedStyle(el).fontFamily
    }))""")
    assert any(item['tag'] == 'H2' and item['size'] == 18 for item in typography)
    assert any(item['tag'] == 'H3' and item['size'] == 16 for item in typography)
    assert all(item['size'] == 13 for item in typography if item['tag'] == 'CODE')

    assert geometry['answerBorder'] == 'none'
    assert geometry['answerBackground'] == 'rgba(0, 0, 0, 0)'
    if width == 390:
        assert all(height >= 42 for height in geometry['actions'])
    screenshot = tmp_path / f'conversation-reading-{theme}-{width}.png'
    page.locator('#reading-fixture').screenshot(path=str(screenshot))
    print(f'Conversation reading screenshot: {screenshot}')
    (tmp_path / f'conversation-reading-{theme}-{width}.json').write_text(json.dumps(geometry, indent=2))
    if width == 1180:
        # The existing shared measure remains the authority for shell, composer,
        # prose and rails; an explicit wider measure must not be silently capped.
        frame.evaluate('document.documentElement.style.setProperty("--msg-measure", "960px")')
        assert answer.evaluate('(n)=>n.getBoundingClientRect().width') > geometry['answerWidth']
        frame.evaluate('document.documentElement.style.removeProperty("--msg-measure")')
    # A new authoritative revision must preserve mounted disclosure choices,
    # while wide code/table evidence scrolls inside its own surface.
    frame.evaluate("""() => {
      const answer=state.turnsById.answer;
      const text=answer.projection.segments.at(-1).text+'\\n\\n'+ '长链接与连续文本'.repeat(150)
        +'\\n\\n```text\\n'+ 'long-code-'.repeat(100)+'\\n```'
        +'\\n\\n| '+Array.from({length:30},(_,i)=>'Column '+i).join(' | ')+' |\\n'
        +'| '+Array(30).fill('---').join(' | ')+' |\\n'
        +'| '+Array(30).fill('evidence').join(' | ')+' |';
      const segments=answer.projection.segments.map((segment,index,list)=>
        index===list.length-1?{...segment,text}:segment);
      state={...state,turnsById:{...state.turnsById,answer:{...answer,
        projectionRevision:answer.projectionRevision+1,projection:{...answer.projection,segments}}}};
      render();
    }""")
    assert frame.locator('[data-block-id="deliverable"]').is_visible()
    assert frame.locator('.md-table-wrapper').count() == 2
    assert frame.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    assert frame.locator('.md-table-wrapper').last.evaluate('(n)=>n.scrollWidth > n.clientWidth')
    assert answer.locator('pre code').last.evaluate('(n)=>n.scrollWidth > n.clientWidth')
    frame.evaluate('surface.dispose()')


@pytest.mark.parametrize('width', [1180, 390])
def test_reading_measure_in_the_live_application_shell(page, tmp_path, width):
    """Actual app boot/composer uses the same measure as mounted reading turns."""
    page.set_viewport_size({'width': width, 'height': 1000})
    page.wait_for_function("window.TofuModules?.version === 3")
    input = page.locator('#userInput')
    input.wait_for(state='visible')
    input.fill('Compare the answer with its supporting evidence.')
    geometry = page.evaluate("""() => {
      const box=document.querySelector('.input-inner');
      return {width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,
        measure:parseFloat(getComputedStyle(document.querySelector('.chat-inner')).getPropertyValue('--msg-measure')),
        composer:box.getBoundingClientRect().width,toolbar:parseFloat(box.style.getPropertyValue('--toolbar-w'))};
    }""")
    assert geometry['scroll'] <= geometry['width'] + 1
    assert geometry['measure'] == 720
    if width == 1180:
        assert geometry['toolbar'] >= geometry['measure']
        assert geometry['composer'] >= geometry['measure']
    page.screenshot(path=str(tmp_path / f'live-reading-shell-{width}.png'))
    input.fill('')

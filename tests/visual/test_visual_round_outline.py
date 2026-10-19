"""Mounted round reading surface with shipped styles against an isolated live app.

Synthetic conversation state never enters durable storage. This exercises the
actual selector + Surface modules, not an LLM journey or transport fault test.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('language,width', [('en', 1180), ('zh', 390)])
def test_round_reading_outline_keyboard_replay_and_geometry(frontend_visual_page, tmp_path,
                                                           theme, language, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='round-outline-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0;z-index:99999';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html data-theme="{theme}" lang="{language}"><body>'
                      '<main id="surface"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)

    # Native-module test bundling strips CSS imports; load the same authored
    # Surface sheet that Vite ships, rather than inventing fixture geometry.
    frame.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    # Fixture shell only; all round/card/control geometry uses application CSS.
    frame.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);'
                               'height:auto;overflow:auto}#surface{display:block;max-width:100%}')
    for name, source in [('feature', 'frontend/src/conversation/index.ts'),
                         ('state', 'frontend/src/core/turn-state.ts')]:
        frame.add_script_tag(path=native_module_path(f'.native/round-visual-{name}.js', ROOT / source))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      const scope={taskId:'execution-a',attemptId:'attempt-a'};
      const thinking=(id,round)=>({type:'thinking',blockId:id,text:'Inspecting the evidence for this round.',llmRound:round,...scope});
      const tool=(id,n,extra={})=>({type:'tool_use',blockId:id,id,name:'read_files',llmRound:n,...scope,
        input:{path:'synthetic/example.ts'},_round:{toolName:'read_files',toolCallId:id,
          roundNum:n+1,llmRound:n,...scope,status:'done',toolArgs:{path:'synthetic/example.ts'},
          results:[{text:'Read-only synthetic result; no command executed.',exitCode:0}],...extra}});
      const segments=[thinking('thought-0',0),tool('tool-0',0),
        thinking('thought-1',1),tool('tool-1',1,{results:[{backgrounded:true,exitCode:'background'}]}),
        {type:'system_note',blockId:'note',reason:'active_checklist',text:'Validation is still required.'},
        thinking('thought-2',2),tool('tool-2',2,{status:'error',results:[{text:'Validation failed safely.',exitCode:1}]}),
        {type:'text',blockId:'answer',text:'Final answer remains readable outside process disclosures.',deliverable:true,terminal:true}];
      const turn={turnId:'turn',conversationId:'round-visual',role:'assistant',laneId:'main',
        ordinal:1,status:'completed',projectionRevision:1,projection:{segments}};
      window.state={...createTurnState('round-visual'),turnsById:{turn},laneOrder:{main:['turn']}};
      const translate=(key,n)=>(locale[key]||key).replace('{n}',String(n));
      window.surface=createConversationSurface(document.getElementById('surface'),{roundOutlineLabel:translate});
      window.render=()=>surface.render(selectConversationViewModel(state));
      render();
      window.originalHeaders=[...document.querySelectorAll('.conversation-tool-card > summary')];
      window.originalBlocks=[...document.querySelectorAll('.conversation-block')];
      window.replay=()=>{state=JSON.parse(JSON.stringify(state));render()};
    }""", locale)
    headers = frame.locator('.conversation-tool-card > summary')
    cards = frame.locator('.conversation-tool-card')
    assert headers.count() == 3

    assert frame.locator('.conversation-round-toolbar:visible').count() == 0
    assert headers.nth(0).locator('.conversation-tool-card__title').inner_text() == 'read_files'
    assert frame.locator('[data-block-id="thought-0"]').is_visible()
    assert [headers.nth(i).evaluate('(node)=>node.closest("[data-block-id]").dataset.actionState') for i in range(3)] == [
        'returned', 'background', 'attention']
    assert [cards.nth(i).evaluate('(node)=>node.open') for i in range(3)] == [False, True, True]
    assert frame.locator('[data-block-id="answer"]').is_visible()
    assert frame.locator('[data-block-id="note"]').is_visible()
    first = headers.nth(0)
    first.focus()
    first.press('Enter')
    assert cards.nth(0).evaluate('(node)=>node.open')
    first.press('Space')
    assert not cards.nth(0).evaluate('(node)=>node.open')
    first.press('Enter')
    assert frame.locator('.conversation-block[hidden]').count() == 0
    first.press('Space')
    assert cards.nth(1).evaluate('(node)=>node.open')
    assert cards.nth(2).evaluate('(node)=>node.open')
    first.focus()
    frame.evaluate('()=>{for(let i=0;i<25;i++) replay()}')
    assert frame.evaluate('originalHeaders[0]===document.activeElement')
    assert frame.evaluate('originalHeaders.every((n,i)=>n===document.querySelectorAll(".conversation-tool-card > summary")[i])')
    assert frame.evaluate('originalBlocks.every((n,i)=>n===document.querySelectorAll(".conversation-block")[i])')
    assert not cards.nth(0).evaluate('(node)=>node.open')
    headers.nth(2).focus()
    assert frame.evaluate('originalHeaders[2]===document.activeElement')
    geometry = frame.evaluate("""() => ({
      width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,
      headers:[...document.querySelectorAll('.conversation-tool-card > summary')].map(n=>({
        left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right,
        height:n.getBoundingClientRect().height})),
      controls:[...document.querySelectorAll('.conversation-tool-card')].every(n=>
        n.querySelector('summary') && n.querySelector('.conversation-tool-card__body')),
      rail:getComputedStyle(document.querySelector('.conversation-turn-blocks'),'::before').content,
      dots:[...document.querySelectorAll('.conversation-block')].map(n=>getComputedStyle(n,'::before').content)
    })""")
    assert geometry['scroll'] <= geometry['width'] + 1
    assert geometry['controls']
    assert geometry['rail'] in ('none', 'normal', '""')
    assert all(value in ('none', 'normal', '""') for value in geometry['dots'])
    assert all(0 <= h['left'] < h['right'] <= geometry['width'] + 1 and h['height'] >= 28
               for h in geometry['headers'])
    assert max(h['left'] for h in geometry['headers']) - min(h['left'] for h in geometry['headers']) <= 1
    screenshot = tmp_path / f'round-outline-{theme}-{language}-{width}.png'
    page.locator('#round-outline-fixture').screenshot(path=str(screenshot))
    print(f'Round outline screenshot: {screenshot}')
    (tmp_path / f'round-outline-{theme}-{language}-{width}.json').write_text(
        json.dumps(geometry, ensure_ascii=False, indent=2) + '\n')
    frame.evaluate('surface.dispose()')

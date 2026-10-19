"""Real tool/program renderers preserve unknown-effect warnings on restore and repaint."""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width,locale', [('light', 390, 'en'), ('dark', 1180, 'zh'), ('tofu', 390, 'zh')])
def test_unknown_effect_is_visible_without_opening_results_and_survives_repaint(
        frontend_visual_page, theme, width, locale, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.add_script_tag(path=native_module_path('.native/tool-effect-recovery.js',
        ROOT / 'frontend/src/conversation/ui/renderers/tool-blocks.ts'))
    messages = json.loads((ROOT / f'frontend/src/i18n/locales/{locale}.json').read_text())
    page.evaluate('''({theme,messages})=>{
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main class="conversation-surface" id="fixture"><div id="tool"></div><div id="program"></div></main>';
      window.effect={operationId:'a'.repeat(32),state:'entered',recovery:'reconcile',scope:'attempt'};
      window.block={id:'tool-1',kind:'tool',name:'perform',toolCallId:'call',input:{},
        round:{roundNum:1,toolName:'perform',status:'done',executionEffect:effect},result:{status:'done',content:'response unavailable'}};
      window.turn={turnId:'turn',status:'interrupted',source:{conversationId:'conversation'}};
      const ports={localizedText:(key,fallback)=>messages[key]??fallback,
        renderToolBlockHtml:()=>'<details class="ptool-result-block"><summary class="ptool-line">Tool evidence</summary><pre>Previous response</pre></details>'};
      window.paint=()=>{
        renderToolBlock(document.querySelector('#tool'),block,turn,ports);
        renderProgramBlock(document.querySelector('#program'),{...block,kind:'program'},turn,ports);
      };
      paint();
    }''', {'theme': theme, 'messages': messages})
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}#fixture{max-width:var(--conversation-reading-measure);margin-inline:auto;}')
    notice = page.locator('#tool > [data-tool-effect-notice]')
    expect(notice).to_contain_text(messages['tool.effectUnknown'])
    expect(notice).to_contain_text(messages['tool.effectReconcile'])
    expect(page.locator('#program > [data-tool-effect-notice]')).to_be_visible()
    expect(page.locator('#tool details')).not_to_have_attribute('open', '')
    page.locator('#tool summary').press('Enter')
    page.locator('#tool summary').focus()
    page.evaluate('''()=>{window.oldNotice=document.querySelector('#tool > [data-tool-effect-notice]');window.oldSummary=document.activeElement;paint();}''')
    assert page.evaluate('document.activeElement===oldSummary')
    assert page.evaluate('document.querySelector("#tool > [data-tool-effect-notice]")===oldNotice')
    expect(page.locator('#tool details')).to_have_attribute('open', '')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path / f'tool-effect-{theme}-{width}.png'), full_page=True)
    page.evaluate("effect.state='returned';paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)
    page.evaluate("effect.state='unknown';effect.recovery='idempotent';turn.status='running';paint()")
    expect(notice).to_contain_text(messages['tool.effectIdempotent'])
    page.evaluate("effect.recovery='read_only';paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)


def test_live_surface_repaints_on_status_only_transition(frontend_visual_page, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    entry = tmp_path / 'effect-live.ts'
    entry.write_text('\n'.join('export * from '+json.dumps(str(ROOT / path))+';' for path in [
        'frontend/src/conversation/index.ts', 'frontend/src/core/turn-state.ts']))
    page.add_script_tag(path=native_module_path('.native/tool-effect-live.js', entry))
    page.evaluate('''()=>{
      document.body.innerHTML='<main id="surface" class="conversation-surface"></main>';
      const effect={operationId:'a'.repeat(32),state:'entered',recovery:'reconcile',scope:'attempt'};
      const round={toolName:'perform',toolCallId:'call',roundNum:1,status:'running',executionEffect:effect};
      const turn={turnId:'answer',conversationId:'effects',actor:'assistant',laneId:'main',ordinal:1,
        status:'running',projectionRevision:1,projection:{segments:[
          {type:'tool_use',blockId:'call',id:'call',name:'perform',input:{},_round:round}]}};
      window.state={...createTurnState('effects'),turnsById:{answer:turn},laneOrder:{main:['answer']}};
      const ports={renderSafeMarkdownHtml:()=>'',renderToolBlockHtml:()=>
        '<details class="ptool-result-block"><summary class="ptool-line">Tool evidence</summary><pre>Evidence</pre></details>'};
      window.surface=createConversationSurface(document.querySelector('#surface'),createClassicConversationRenderers(ports));
      window.paint=()=>surface.render(selectConversationViewModel(state));paint();
    }''')
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)
    page.locator('.ptool-result-block summary').focus()
    page.evaluate("window.previousSummary=document.activeElement;state.turnsById.answer={...state.turnsById.answer,status:'interrupted',projectionRevision:2};paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_be_visible()
    expect(page.locator('[data-tool-effect-notice]')).to_contain_text('Operation outcome unknown')
    assert page.evaluate('document.activeElement===previousSummary')
    page.evaluate('surface.dispose()')

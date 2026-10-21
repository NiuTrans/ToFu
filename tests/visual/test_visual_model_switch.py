"""Visible live fallback identity and replay in the fixed composer area in Chromium."""
import json
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page
pytestmark = pytest.mark.visual

@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1180])
def test_model_switch_composer_stays_visible(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.set_content('<!doctype html><html data-theme="'+theme+'"><head>'
                     '<link rel="stylesheet" href="/static/styles.css"></head><body>'
                     '<main style="height:100dvh;display:flex;flex-direction:column;min-width:0">'
                     '<div class="chat-container" id="chat"><div class="conversation-surface" style="height:4000px">'
                     '<header id="replyHeader"></header><p>Long running conversation</p></div></div>'
                     '<div class="input-area"><div class="input-inner"><div id="fixture" class="composer-model-notice" hidden></div>'
                     '<textarea aria-label="Message" style="max-width:100%"></textarea></div></div></main></body></html>')
    entry = tmp_path / 'model-notice-entry.ts'
    entry.write_text(''.join('export * from ' + json.dumps(str(ROOT / path)) + ';' for path in [
        'frontend/src/conversation/ui/composer-model-notice.ts',
        'frontend/src/conversation/ui/renderers/turn-chrome.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/model-switch-composer.js', entry))
    labels = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate(r"""labels=>{
      window.turn={actor:'assistant',status:'running',source:{createdAt:'2026-10-06T05:24:06+08:00',
        projection:{lastRoundUsage:{model:'gpt-6.1-sol',resolvedModel:'gpt-6.1-sol'}}},
        metadata:{fallback:{from:'gpt-6.1-sol',model:'kimi-k3',
          reason:'generic: SSE error: cyber_policy: This content was flagged for possible cybersecurity risk.'},
          fallbackInTimeline:true}};
      window.view={conversationId:'A',mainLane:{turns:[turn]}};
      window.paint=()=>renderComposerModelNotice(document.getElementById('fixture'),view,{
        renderSafeMarkdownHtml:()=>'',localizedText:(key,fallback,values={})=>
          (labels[key]||fallback).replace(/\{(\w+)\}/g,(_,name)=>values[name]??'')});
      renderTurnHeader(document.getElementById('replyHeader'),turn,{renderSafeMarkdownHtml:()=>''});
      paint();document.getElementById('chat').scrollTop=4000;
    }""", labels)
    notice=page.locator('.conversation-model-notice--switched')
    expect(notice).to_be_in_viewport()
    expect(page.locator('#replyHeader .conversation-model-notice')).to_have_count(0)
    expect(notice).to_contain_text('已自动切换模型：gpt-6.1-sol → kimi-k3')
    expect(notice).to_contain_text('切换原因：generic: SSE error: cyber_policy')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.evaluate("""()=>{turn=JSON.parse(JSON.stringify(turn));view.mainLane.turns=[turn];turn.status='completed';
      turn.source.projection.lastRoundUsage={model:'kimi-k3',resolvedModel:'kimi-k3',providerId:'example-corp'};paint()}""")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text('实际模型：kimi-k3 · example-corp')
    page.screenshot(path=str(tmp_path/f'model-switch-{theme}-{width}.png'))
    page.evaluate("delete turn.metadata.fallback;paint()")
    expect(notice).to_have_count(0)
    expect(page.locator('.conversation-model-notice')).to_have_text('实际模型：kimi-k3')

    page.evaluate("view.mainLane.turns.push({actor:'human',metadata:{},source:{projection:{}}});paint()")
    expect(page.locator('#fixture')).to_be_hidden()
    page.evaluate("renderComposerModelNotice(document.getElementById('fixture'),null)")
    expect(page.locator('#fixture')).to_have_attribute('data-conversation-id','')

"""Reply headers omit reasoning notices while real thinking and evidence survive.

Runs real common projection/renderer owners on an ephemeral local HTTP origin;
all Turns are synthetic and no model, credentials or durable data are touched.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def reasoning_browser_graph():
    return native_module_path('.native/reasoning-status-browser.js', ROOT / 'frontend/src/conversation/index.ts')


def _mount(page, graph, language, theme):
    translations = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate('''({language, theme, translations}) => {
      document.documentElement.dataset.theme = theme;
      document.documentElement.lang = language;
      document.body.dataset.theme = theme;
      document.body.innerHTML = '<main class="chat-inner"><div id="reasoning-surface"></div></main>';
      window.reasoningTranslations = translations;
    }''', {'language': language, 'theme': theme, 'translations': translations})
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;padding:var(--space-4);margin:0}'
                                '.chat-inner{padding:0;margin-inline:auto;min-height:0}')
    page.add_script_tag(path=graph)
    page.evaluate('''() => {
      const translate = (key, fallback, values) => {
        let value = reasoningTranslations[key] ?? fallback;
        for (const [name, part] of Object.entries(values ?? {})) value = value.replaceAll('{'+name+'}', String(part));
        return value;
      };
      const record = (id, ordinal, status, extra) => ({
        turnId:id,conversationId:'reasoning-fixture',actor:'assistant',role:'assistant',laneId:'main',
        ordinal,status,projectionRevision:10,createdAt:1792575000000,
        projection:{content:'Recorded answer '+id,thinking:'',segments:[{
          type:'text',blockId:'text:terminal',text:'Recorded answer '+id,terminal:true,deliverable:true}],
          ...extra}
      });
      const reported = {usage:{completion_tokens_details:{reasoning_tokens:2002}},usageComplete:true,
        lastRoundUsage:{round:1,reasoningResolution:{requested:'preset',requestedValue:'high',effective:'preset',
          actualMode:'exact',actualValue:'max'}}};
      const encryptedOnly = record('opaque',1,'completed',reported);
      const lowerBound = record('partial',2,'completed',{...reported,usageComplete:false});
      const readable = record('readable',3,'completed',{...reported,thinking:'A readable summary.',segments:[
        {type:'thinking',blockId:'readable-thinking',text:'A readable summary.',terminal:true},
        {type:'text',blockId:'text:terminal',text:'Recorded answer readable',terminal:true,deliverable:true}]});
      const absent = record('unknown',4,'completed',{});
      const unsafe = record('unsafe',5,'completed',{...reported,lastRoundUsage:{round:1,reasoningResolution:{
        actualMode:'exact',actualValue:'<img src=x onerror="window.reasoningPwned=1">'}}});
      const active = record('active',6,'running',reported);
      const failed = record('failed',7,'failed',reported);
      const off = record('off',8,'completed',{lastRoundUsage:{round:1,reasoningResolution:{actualMode:'off',actualValue:''}}});
      const pending = record('pending',9,'pending',reported);
      const interrupted = record('interrupted',10,'interrupted',reported);
      const truncated = record('truncated',11,'truncated',reported);
      const turns=[encryptedOnly,lowerBound,readable,absent,unsafe,active,failed,off,pending,interrupted,truncated];
      window.reasoningState={...createTurnState('reasoning-fixture'),
        turnsById:Object.fromEntries(turns.map(turn=>[turn.turnId,turn])),laneOrder:{main:turns.map(turn=>turn.turnId)}};
      const ports={localizedText:translate,renderSafeMarkdownHtml:text=>{
        const node=document.createElement('div');node.textContent=text;return node.innerHTML;}};
      window.reasoningSurface=createConversationSurface(document.getElementById('reasoning-surface'),
        createClassicConversationRenderers(ports));
      window.reasoningRender=()=>reasoningSurface.render(selectConversationViewModel(reasoningState));
      reasoningRender();
    }''')


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_missing_summary_stays_out_of_headers_and_preserves_readable_thinking(
        frontend_visual_page, reasoning_browser_graph, tmp_path, theme, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    _mount(page, reasoning_browser_graph, language, theme)
    turn = lambda id: page.locator(f'[data-turn-id="{id}"]').first
    assert page.locator('.conversation-reasoning-status, [data-reasoning-summary]').count() == 0
    assert turn('opaque').locator('.thinking-block').count() == 0
    assert turn('partial').locator('.thinking-block').count() == 0
    assert turn('opaque').locator('[data-block-id="text:terminal"]').inner_text().endswith('Recorded answer opaque')
    assert '2,002' not in turn('opaque').locator('[data-conversation-part="turn-header"]').inner_text()
    assert page.evaluate('window.reasoningPwned') is None
    # Readable reasoning retains the existing keyboard-accessible folds.
    thinking = turn('readable').locator('.thinking-header')
    thinking.focus()
    thinking.press('Enter')
    assert turn('readable').locator('.thinking-text').is_visible()
    assert turn('readable').locator('.thinking-text').inner_text() == 'A readable summary.'
    page.evaluate('''() => {
      window.reasoningAnswerNode=document.querySelector('[data-turn-id="opaque"] [data-block-id="text:terminal"]');
      window.reasoningHeaderNode=document.querySelector('[data-turn-id="opaque"] [data-conversation-part="turn-header"]').firstChild;
      const turn=reasoningState.turnsById.opaque;
      reasoningState={...reasoningState,turnsById:{...reasoningState.turnsById,opaque:{...turn,projectionRevision:11,
        projection:{...turn.projection,usage:{completion_tokens_details:{reasoning_tokens:7374}},lastRoundUsage:{
          round:2,reasoningResolution:{actualMode:'exact',actualValue:'high'}}}}}};
      reasoningRender();
    }''')
    assert page.locator('.conversation-reasoning-status, [data-reasoning-summary]').count() == 0
    assert page.evaluate('reasoningAnswerNode === document.querySelector(\'[data-turn-id="opaque"] [data-block-id="text:terminal"]\')')
    assert page.evaluate('reasoningHeaderNode === document.querySelector(\'[data-turn-id="opaque"] [data-conversation-part="turn-header"]\').firstChild')
    assert page.evaluate('reasoningState.turnsById.opaque.projection.usage.completion_tokens_details.reasoning_tokens') == 7374
    geometry = turn('opaque').evaluate('''node=>{
      const box=node.getBoundingClientRect();return {left:box.left,right:box.right,width:innerWidth,
        scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
    }''')
    assert geometry['left'] >= 0 and geometry['right'] <= geometry['width'] + 1
    assert geometry['scroll'] <= geometry['client'] + 1
    page.screenshot(path=str(tmp_path / f'no-reasoning-status-{language}-{theme}-{width}.png'))
    page.evaluate('reasoningSurface.dispose()')


def test_reload_keeps_header_quiet_without_losing_provider_evidence(
        frontend_visual_page, reasoning_browser_graph):
    page = frontend_visual_page
    _mount(page, reasoning_browser_graph, 'zh', 'light')
    before = page.locator('[data-turn-id="opaque"] [data-block-id="text:terminal"]').inner_text()
    page.reload(wait_until='load')
    _mount(page, reasoning_browser_graph, 'zh', 'light')
    assert page.locator('.conversation-reasoning-status, [data-reasoning-summary]').count() == 0
    assert page.locator('[data-turn-id="opaque"] [data-block-id="text:terminal"]').inner_text() == before
    assert page.locator('[data-turn-id="opaque"] .thinking-block').count() == 0
    assert page.evaluate('reasoningState.turnsById.opaque.projection.usage.completion_tokens_details.reasoning_tokens') == 2002
    assert page.evaluate('reasoningState.turnsById.opaque.projection.lastRoundUsage.reasoningResolution.actualValue') == 'max'
    page.evaluate('reasoningSurface.dispose()')

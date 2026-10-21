"""Reasoning receipts are visible without inventing expandable thinking text.

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
      const reported = {usage:{completion_tokens_details:{reasoning_tokens:2843}},usageComplete:true,
        lastRoundUsage:{round:1,reasoningResolution:{requested:'preset',requestedValue:'high',effective:'preset',
          actualMode:'exact',actualValue:'low'}}};
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
      const turns=[encryptedOnly,lowerBound,readable,absent,unsafe,active,failed,off];
      window.reasoningState={...createTurnState('reasoning-fixture'),
        turnsById:Object.fromEntries(turns.map(turn=>[turn.turnId,turn])),laneOrder:{main:turns.map(turn=>turn.turnId)}};
      const ports={localizedText:translate,renderSafeMarkdownHtml:text=>{
        const node=document.createElement('div');node.textContent=text;return node.innerHTML;}};
      window.reasoningSurface=createConversationSurface(document.getElementById('reasoning-surface'),
        createClassicConversationRenderers(ports));
      window.reasoningRender=()=>reasoningSurface.render(selectConversationViewModel(reasoningState));
      reasoningRender();
    }''')
    return translations


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_missing_summary_is_honest_visible_and_narrow_safe(
        frontend_visual_page, reasoning_browser_graph, tmp_path, theme, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    text = _mount(page, reasoning_browser_graph, language, theme)
    turn = lambda id: page.locator(f'[data-turn-id="{id}"]').first
    info = turn('opaque').locator('.conversation-reasoning-status')
    assert info.is_visible()
    assert info.get_attribute('data-reasoning-summary') == 'unavailable'
    assert text['chat.reasoning.summaryUnavailable'] in info.inner_text()
    assert text['chat.reasoning.effort.low'] in info.locator('.conversation-reasoning-effort').inner_text()
    assert '2,843' in info.locator('.conversation-reasoning-tokens').inner_text()
    assert info.locator('details,button,a,img').count() == 0
    assert turn('opaque').locator('.thinking-block').count() == 0
    assert turn('partial').locator('.conversation-reasoning-tokens').inner_text() == (
        text['chat.reasoning.reportedTokensLowerBound'].replace('{tokens}', '2,843'))
    assert turn('readable').locator('.conversation-reasoning-status').count() == 0
    assert turn('unknown').locator('.conversation-reasoning-status').count() == 0
    assert turn('unsafe').locator('.conversation-reasoning-effort').count() == 0
    assert page.evaluate('window.reasoningPwned') is None
    assert turn('active').locator('.conversation-reasoning-status').get_attribute('data-reasoning-summary') == 'not-received-yet'
    assert turn('failed').locator('.conversation-reasoning-status').get_attribute('data-reasoning-summary') == 'not-received'
    assert text['chat.reasoning.summaryUnavailable'] not in turn('failed').inner_text()
    assert turn('off').locator('.conversation-reasoning-summary').count() == 0
    # Reasoning text still uses the existing keyboard-accessible historical folds.
    process = turn('readable').locator('.conversation-history-process > summary')
    process.focus()
    process.press('Enter')
    thinking = turn('readable').locator('.thinking-header')
    thinking.focus()
    thinking.press('Enter')
    assert turn('readable').locator('.thinking-text').is_visible()
    assert turn('readable').locator('.thinking-text').inner_text() == 'A readable summary.'
    # A late authoritative receipt repaints metadata without replacing the answer.
    page.evaluate('''() => {
      window.reasoningAnswerNode=document.querySelector('[data-turn-id="opaque"] [data-block-id="text:terminal"]');
      const turn=reasoningState.turnsById.opaque;
      reasoningState={...reasoningState,turnsById:{...reasoningState.turnsById,opaque:{...turn,projectionRevision:11,
        projection:{...turn.projection,usage:{completion_tokens_details:{reasoning_tokens:7374}},lastRoundUsage:{
          round:2,reasoningResolution:{actualMode:'exact',actualValue:'high'}}}}}};
      reasoningRender();
    }''')
    assert '7,374' in info.locator('.conversation-reasoning-tokens').inner_text()
    assert text['chat.reasoning.effort.high'] in info.locator('.conversation-reasoning-effort').inner_text()
    assert page.evaluate('reasoningAnswerNode === document.querySelector(\'[data-turn-id="opaque"] [data-block-id="text:terminal"]\')')
    geometry = info.evaluate('''node=>{
      const box=node.getBoundingClientRect();return {left:box.left,right:box.right,width:innerWidth,
        scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth,
        textSize:parseFloat(getComputedStyle(node).fontSize)};
    }''')
    assert geometry['left'] >= 0 and geometry['right'] <= geometry['width'] + 1
    assert geometry['scroll'] <= geometry['client'] + 1
    assert geometry['textSize'] >= 11
    page.screenshot(path=str(tmp_path / f'reasoning-status-{language}-{theme}-{width}.png'))
    page.evaluate('reasoningSurface.dispose()')


def test_reasoning_receipts_reconstruct_on_reload_without_browser_state(
        frontend_visual_page, reasoning_browser_graph):
    page = frontend_visual_page
    _mount(page, reasoning_browser_graph, 'zh', 'light')
    before = page.locator('[data-turn-id="opaque"] .conversation-reasoning-status').inner_text()
    page.reload(wait_until='load')
    _mount(page, reasoning_browser_graph, 'zh', 'light')
    assert page.locator('[data-turn-id="opaque"] .conversation-reasoning-status').inner_text() == before
    assert page.locator('[data-turn-id="opaque"] .thinking-block').count() == 0
    page.evaluate('reasoningSurface.dispose()')

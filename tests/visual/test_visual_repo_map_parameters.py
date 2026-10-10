"""Repo-map requested scope through real Surface, presenters and shipped CSS.

An ephemeral local frontend server supplies assets; accepted rounds are synthetic.
No repository scan, persistent conversation or model request is executed.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual
_LONG_PATH = 'lib/' + 'requested_scope_without_breaks_' * 7 + '/symbols.py'


@pytest.mark.parametrize('width,language,theme', [
    (1100, 'en', 'light'), (1100, 'zh', 'tofu'),
    (390, 'en', 'dark'), (390, 'zh', 'tofu'),
])
def test_repo_maps_keep_distinct_parameters_visible_before_expansion(
    frontend_visual_page, tmp_path, width, language, theme,  # noqa: F811
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r'''({locale,language,theme}) => {
      document.documentElement.lang=language;document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main id="surface"></main>';
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window._featureFlags={debug_mode:false};window.projectState={extraRoots:[]};
      window.getActiveConv=()=>null;window.renderMarkdown=value=>escapeHtml(value);
      window.Icon=()=>'<svg viewBox="0 0 24 24" width="14" height="14"></svg>';
    }''', {'locale': locale, 'language': language, 'theme': theme})
    # Host geometry only; all actual tool, scope and disclosure CSS is shipped.
    page.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);height:auto;overflow:auto}'
                              '#surface{max-width:900px;margin:auto}.conversation-turn{--rail-w:0px}')
    page.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    page.add_script_tag(path=native_module_path(
        '.native/repo-map-parameters-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    page.evaluate(r'''longPath => {
      const scope={llmRound:0,attemptId:'repo-map-attempt',taskId:'repo-map-task'};
      const parameters=[{path:'lib',focus:'swarm',max_tokens:4500},
        {path:'frontend/src',focus:'conversation',max_tokens:9000},
        {path:longPath,focus:'<img src=x onerror="window.repoMapInjected=true">',max_tokens:2048}];
      const blocks=parameters.map((args,i)=>({kind:'tool',blockId:'map-'+i,ordinal:i+1,source:scope,
        round:{...scope,roundNum:i+1,toolCallId:'map-'+i,toolName:'repo_map',status:'done',query:'repo_map '+args.path,
          toolArgs:i===1?JSON.stringify(args):args,results:[{badge:'25/34 files'}],
          toolContent:'lib/swarm/example.py:\n  class Example\n  def inspect_scope()'}}));
      blocks.push({kind:'tool',blockId:'map-live',ordinal:4,source:scope,round:{...scope,
        roundNum:4,toolCallId:'map-live',toolName:'repo_map',status:'searching',query:'repo_map lib',
        toolArgs:{path:'lib',focus:'runtime',max_tokens:6000},results:[]}});
      const turn={turnId:'map-turn',presentationId:'map-turn',actor:'assistant',role:'assistant',laneId:'main',
        ordinal:1,status:'completed',terminal:true,blocks,queuedTurns:[],branches:[],actions:[],
        metadata:{translation:{pending:false,streaming:false}},finish:null,
        source:{projection:{timestamp:'2026-10-08T09:00:00Z'},createdAt:'2026-10-08T09:00:00Z'}};
      window.model={conversationId:'repo-map-fixture',conversationRevision:1,transport:'ready',
        mainLane:{laneId:'main',title:'Main',kind:'main',expanded:true,turns:[turn]},
        orphanLanes:[],queue:[],planDecision:null,transportNotice:null};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:source=>'<p>'+escapeHtml(source)+'</p>',localizedText:(key,fallback)=>t(key)||fallback,
        renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,block.round.status==='searching'),
      }));
      window.replay=()=>surface.render(JSON.parse(JSON.stringify(model)));
      replay();
    }''', _LONG_PATH)

    expected = [
        ['lib', 'swarm', '4500'], ['frontend/src', 'conversation', '9000'],
        [_LONG_PATH, '<img src=x onerror="window.repoMapInjected=true">', '2048'],
    ]
    for index, values in enumerate(expected):
        block = page.locator(f'[data-block-id="map-{index}"]')
        summary = block.locator('summary.ptool-result-header')
        assert block.locator('details').count() == 1
        assert block.locator('details').get_attribute('open') is None
        assert locale['tool.repoMap.label'] in summary.inner_text()
        assert locale['tool.repoMap.description'] in summary.inner_text()
        parameters = summary.locator('.ptool-repo-map-params')
        assert parameters.is_visible()
        assert parameters.locator('code').all_text_contents() == values
        assert '25/34 files' in summary.inner_text()
        assert not block.locator('.ptool-result-body').is_visible()
    assert 'path: lib · focus: runtime · max_tokens: 6000' in page.locator(
        '[data-block-id="map-live"] .ptool-repo-map-params').inner_text()
    assert page.locator('[data-block-id="map-live"] .ptool-active').count() == 1
    assert page.locator('[data-block-id="map-live"] .ptool-line-status--ok').count() == 0
    assert page.locator('#surface img, #surface script').count() == 0
    assert not page.evaluate('Boolean(window.repoMapInjected)')
    assert page.locator('body').evaluate('node=>node.scrollWidth<=innerWidth+1')
    assert page.locator('.ptool-result-header').evaluate_all('''nodes=>nodes.every(node=>{
      const bounds=node.getBoundingClientRect();return bounds.right<=innerWidth+1
        && node.scrollWidth<=node.clientWidth+1;
    })''')

    long_value = page.locator('[data-block-id="map-2"] .ptool-repo-map-param code').first
    layout = long_value.evaluate('''node=>{
      const range=document.createRange();range.selectNodeContents(node);
      const rects=[...range.getClientRects()].filter(rect=>rect.width>0);
      return {lines:rects.length,start:{x:rects[0].left+1,y:rects[0].top+rects[0].height/2},
        end:{x:rects.at(-1).right-1,y:rects.at(-1).top+rects.at(-1).height/2},
        userSelect:getComputedStyle(node).userSelect};
    }''')
    assert layout['userSelect'] == 'text'
    if width == 390:
        assert layout['lines'] > 1
    page.mouse.move(layout['start']['x'], layout['start']['y'])
    page.mouse.down()
    page.mouse.move(layout['end']['x'], layout['end']['y'], steps=12)
    page.mouse.up()
    assert page.evaluate('document.getSelection().toString()') == _LONG_PATH
    assert page.locator('[data-block-id="map-2"] details').get_attribute('open') is None
    page.evaluate('document.getSelection().removeAllRanges()')

    summary = page.locator('[data-block-id="map-0"] summary.ptool-result-header')
    summary.focus()
    page.keyboard.press('Enter')
    assert page.locator('[data-block-id="map-0"] .ptool-result-body').is_visible()
    page.wait_for_function("document.querySelector('[data-block-id=\"map-0\"] .ptool-result-body')?.textContent.includes('class Example')")
    assert 'class Example' in page.locator('[data-block-id="map-0"] .ptool-result-body').inner_text()
    page.evaluate('replay()')
    assert summary.evaluate('node=>node===document.activeElement')
    assert page.locator('[data-block-id="map-0"] details').get_attribute('open') is not None
    page.keyboard.press('Space')
    assert not page.locator('[data-block-id="map-0"] .ptool-result-body').is_visible()
    screenshot = tmp_path / f'repo-map-parameters-{width}-{language}-{theme}.png'
    page.screenshot(path=str(screenshot))
    print(f'Repo-map parameters screenshot: {screenshot}')

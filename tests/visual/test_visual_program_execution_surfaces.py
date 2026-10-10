"""Chromium checks the shared program card through all three rendering paths.

Projected round fixtures exercise presentation only; no API/storage is mocked.
Actual gateway-to-rendering integration is covered by the JavaScript visual test.
"""
from __future__ import annotations

import json
import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('surface', ['retained', 'classic', 'default'])
@pytest.mark.parametrize('width,language,theme', [
    (1280, 'zh', 'light'), (390, 'zh', 'light'),
    (1280, 'en', 'dark'), (390, 'en', 'dark'),
])
def test_shared_card_preserves_evidence_and_reader_choices(
        frontend_visual_page, surface, width, language, theme, tmp_path):  # noqa: F811 -- pytest fixture injection
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 880})
    page.evaluate('''theme=>{
      document.documentElement.dataset.theme=theme;
      document.documentElement.classList.toggle('dark',theme==='dark');
      document.body.innerHTML='<main id="program-fixture"></main>';
    }''', theme)
    page.add_style_tag(content='body{display:block;overflow:auto;height:auto}#program-fixture{max-width:960px;margin:24px auto;padding:0 16px}')
    messages = json.loads((ROOT/'frontend/src/i18n/locales'/f'{language}.json').read_text())
    page.evaluate('messages=>window.messages=messages', messages)
    page.add_script_tag(content=r'''
      window._featureFlags={debug_mode:false};
      window.Icon=()=>'<svg aria-hidden="true" viewBox="0 0 16 16"></svg>';
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window.t=(key,params)=>String(messages[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
    ''')
    for name, source in [
        ('groups', 'ui/tool-groups/index.ts'),
        ('card', 'ui/program-execution-card.ts'),
        ('default', 'ui/surface-default-renderers.ts'),
        ('classic', 'ui/renderers/tool-blocks.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/program-surface-{name}.js',
            ROOT/'frontend/src/conversation'/source))
    page.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    page.evaluate('''surface=>{
      window.round={_programSynthetic:true,roundNum:1,programStatus:'completed',programBackend:'local_javascript',
        programCode:'const rows = await tools.invoke("read", {}); return rows;',
        programResult:{kind:'observation',reason:'Choose the complete clause evidence',data:{rows:['<img src=x onerror=alert(1)>']}},
        childCallIds:['child'],childToolNames:['read'],
        programStats:{outcome:'needs_model',result_delivery:{outputBytes:0},result_bytes:250,call_stages:1,result_reads:3}};
      window.renderCard=()=>{
        const node=document.getElementById('program-fixture');
        const block={kind:'program',blockId:'program',round};
        if(surface==='default')renderDefaultBlock(node,block);
        else renderProgramBlock(node,block,{}, {
          renderSafeMarkdownHtml:()=>{throw Error('Program evidence must stay text')},
          localizedText:(key,fallback,values)=>t(key,values),
          ...(surface==='retained'?{renderToolBlockHtml:()=>_renderToolSlot(round,[round])}:{}),
        });
      };
      renderCard();
    }''', surface)
    card = page.locator('.ptc-card')
    assert card.is_visible()
    assert card.locator('.ptc-title').inner_text() == (
        'Program execution' if surface == 'default' else messages['ptc.title'])
    result = card.locator('.ptc-result')
    code = card.locator('.ptc-code')
    assert result.evaluate('node=>node.open') is True
    assert code.evaluate('node=>node.open') is False
    assert card.locator('.ptc-observation').inner_text() == 'Choose the complete clause evidence'
    assert card.locator('img').count() == 0
    assert json.loads(result.locator('pre').inner_text()) == {'rows': ['<img src=x onerror=alert(1)>']}
    assert 'local_javascript' not in card.inner_text()
    assert card.evaluate('node=>node.open') is True
    assert card.locator('.ptc-technical').evaluate('node=>node.open') is False
    assert not card.locator('.ptc-measurements').is_visible()
    assert ('3 retained results reused' if surface == 'default' else
            messages['ptc.measurement.reused'].replace('{n}', '3')) in card.inner_text()
    assert page.evaluate('''()=>{
      const card=document.querySelector('.ptc-card');
      return card.getBoundingClientRect().right<=innerWidth+1
        && card.scrollWidth<=card.clientWidth+1
        && card.querySelector('.ptc-result').compareDocumentPosition(card.querySelector('.ptc-code'))&Node.DOCUMENT_POSITION_FOLLOWING;
    }''')
    card.screenshot(path=str(tmp_path/f'{surface}-{width}-{language}.png'))
    result.locator('summary').click()
    code.locator('summary').focus()
    page.keyboard.press('Enter')
    assert code.evaluate('node=>node.open') is True
    page.evaluate('''()=>{
      window.originalCode=document.querySelector('.ptc-code');
      round.programResult={...round.programResult,data:{rows:['updated']}};
      renderCard();
    }''')
    assert result.evaluate('node=>node.open') is False
    assert code.evaluate('node=>node===originalCode&&node.open') is True
    assert code.locator('summary').evaluate('node=>node===document.activeElement') is True
    page.evaluate('''()=>{round.programStatus='error';round.programResult={code:'tool_failed',message:'Permission denied'};renderCard()}''')
    assert card.locator('.ptc-observation').count() == 0
    assert card.locator('.ptc-result-error').evaluate('node=>node.open') is True
    assert card.locator('.ptc-result').inner_text().endswith('"Permission denied"\n}')
    page.evaluate("()=>{round.programBackend='native_openai';renderCard()}")
    assert code.locator('code').get_attribute('class') == 'language-javascript'
    for value, expected in [(None, 'null'), ('', '""'), (0, '0'), (False, 'false')]:
        page.evaluate("""value=>{
          round.programStatus='completed';round.programResult=value;renderCard();
        }""", value)
        result = card.locator('.ptc-result')
        assert result.locator('summary').is_visible()
        assert result.locator('summary').inner_text() == (
            'Program result' if surface == 'default' else messages['ptc.result'])
        if not result.evaluate('node=>node.open'):
            result.locator('summary').click()
        assert result.locator('pre').is_visible()
        assert result.locator('pre').inner_text() == expected


def test_business_observation_is_rendered_as_a_return_value(frontend_visual_page):
    page = frontend_visual_page
    page.add_script_tag(path=native_module_path('.native/program-business-value.js',
        ROOT/'frontend/src/conversation/ui/program-execution-card.ts'))
    page.evaluate("""()=>{
      document.body.innerHTML='<main id="card"></main>';
      renderProgramExecutionCard(document.getElementById('card'),{
        programStatus:'completed',programStats:{outcome:'completed'},
        programResult:{kind:'observation',reason:'a business field',data:[1]}});
    }""")
    card = page.locator('.ptc-card')
    assert card.locator('.ptc-observation').count() == 0
    assert card.evaluate('node=>node.open') is False
    card.locator('.ptc-head').click()
    assert card.locator('.ptc-result summary').inner_text() == 'Program result'
    assert card.locator('.ptc-result').evaluate('node=>node.open') is False
    card.locator('.ptc-result summary').click()
    assert json.loads(card.locator('.ptc-result pre').inner_text()) == {
        'kind':'observation','reason':'a business field','data':[1]}

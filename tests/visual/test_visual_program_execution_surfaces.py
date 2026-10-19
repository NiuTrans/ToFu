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
    assert ('Saved results read 3 times' if surface == 'default' else
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


@pytest.mark.parametrize('width', [1280, 390])
def test_gateway_lookup_has_one_standard_header(frontend_visual_page, width, tmp_path):
    """Real Chromium/local HTTP: adapter and parent become one glanceable card."""
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 880})
    page.add_script_tag(path=native_module_path('.native/unified-program-groups.js',
        ROOT/'frontend/src/conversation/ui/tool-groups/index.ts'))
    page.add_script_tag(path=native_module_path('.native/unified-program-card.js',
        ROOT/'frontend/src/conversation/ui/program-execution-card.ts'))
    page.evaluate('''()=>{
      const code='return await tools.inputs({name:"mcp__llm__experiment_get"});';
      const gateway={toolName:'execute_program',toolCallId:'lookup',llmRound:15,
        attemptId:'attempt',status:'done',toolArgs:{code},query:'Programmatic tool call · tool gateway · R15'};
      const parent={_programSynthetic:true,_programCallId:'lookup',llmRound:15,attemptId:'attempt',
        roundNum:15,programStatus:'completed',programCode:code,programResult:[],
        programStats:{tool_calls:0,successful_tool_calls:0},childToolNames:[]};
      const text=(key,values)=>key.replace(/\\{(\\w+)\\}/g,(_,name)=>values?.[name]??'');
      const ports={text,icon:()=>'',markdown:value=>value,cleanTranslation:value=>value,
        renderLine:round=>'<p class="gateway-row">'+round.query+'</p>',renderSwarm:()=>null,
        renderProgram:round=>programExecutionCardHtml(round),debugEnabled:()=>false,
        taskIdForRound:()=>'',debugLog:()=>{}};
      document.body.innerHTML='<main id="unified"></main>';
      document.getElementById('unified').innerHTML=createToolGroupPresentation(ports)
        .createSession([gateway,parent]).renderGroups([gateway,parent]);
    }''')
    page.add_style_tag(content='body{display:block;overflow:auto;height:auto}#unified{max-width:960px;margin:24px auto;padding:0 16px}')
    card = page.locator('.ptc-card')
    assert card.count() == 1
    assert page.locator('.gateway-row').count() == 0
    assert page.locator('.ptool-turn-head,.ptool-turn-rno-solo').count() == 0
    header = card.locator('.ptc-head')
    assert header.locator('.ptc-title').inner_text() == 'Program execution'
    assert 'Script: look up prior results' in header.inner_text()
    assert 'No source tools called again' not in header.inner_text()
    assert 'No source tools called again' in card.locator('.ptc-measurements').text_content()
    assert 'Result: []' not in header.inner_text()
    assert card.locator('.ptc-preview').text_content() == 'Result: []'
    assert header.get_attribute('class') == 'ptc-head ptool-line'
    assert card.evaluate('node=>node.getBoundingClientRect().right<=innerWidth+1 && node.scrollWidth<=node.clientWidth+1')
    header.focus()
    page.keyboard.press('Enter')
    assert card.evaluate('node=>node.open')
    card.locator('.ptc-result summary').click()
    assert card.locator('.ptc-result pre').inner_text() == '[]'
    page.screenshot(path=str(tmp_path/f'unified-program-{width}.png'), full_page=True)


@pytest.mark.parametrize('width', [1280, 390])
def test_llm_canvas_links_are_safe_in_retained_and_fallback_titles(frontend_visual_page, width, tmp_path):

    # The default Surface sheet is a real lazy browser import, not critical CSS.
    from lib.tasks_pkg.tool_display._renderers import _tool_display_mcp

    frontend_visual_page.add_style_tag(path=str(ROOT/'frontend/src/conversation/ui/conversation-surface.css'))
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 880})
    for name, source in [('canvas-links', 'frontend/src/conversation/presentation/mcp-resource-links.ts'),
                         ('canvas-fallback', 'frontend/src/conversation/presentation/tool-fallback-presentation.ts'),
                         ('canvas-escape', 'frontend/src/html-safety.ts')]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT/source))
    query, extra = _tool_display_mcp('mcp__llm__experiment_get',
        {'experiment_id': '381153'}, '', '', user_id=917)
    page.evaluate('''row=>{
      document.body.innerHTML='<main id="canvas"></main>';
      const root=document.querySelector('#canvas');
      const retained=document.createElement('div'); retained.className='ptool-line';
      retained.innerHTML=renderMcpResourceLinks(escapeHtmlText(row.query),row);
      root.append(retained,buildToolFallbackCard(document,{title:row.query,
        resourceRound:row,status:'done',sections:[]}));
    }''', {'query': query, **extra})
    page.add_style_tag(content='body{display:block;overflow:auto;height:auto}#canvas{max-width:960px;margin:24px auto;padding:0 16px}')
    anchors=page.locator('#canvas a.ptool-mcp-link')
    assert anchors.count() == 2
    for link in anchors.all():
        assert link.get_attribute('href') == 'https://model.internal.example.com/user/modelManage/experimentalTaskDetail/381153'
        assert link.get_attribute('target') == '_blank'
        assert link.get_attribute('rel') == 'noopener noreferrer'
        link.focus()
        assert link.evaluate('node=>document.activeElement===node')
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
    page.screenshot(path=str(tmp_path/f'canvas-links-{width}.png'), full_page=True)
    assert page.evaluate('''()=>renderMcpResourceLinks('canvas',
      {_mcpLinks:{canvas:'javascript:alert(1)'}})==='canvas' ''')
    assert page.evaluate('''()=>renderMcpResourceLinks('<a href="/known">canvas</a>',
      {_mcpLinks:{canvas:'https://model.internal.example.com/'}})==='<a href="/known">canvas</a>' ''')


@pytest.mark.parametrize('width', [1280, 390])
def test_saved_result_reads_have_plain_chinese_headers(frontend_visual_page, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 880})
    messages = json.loads((ROOT/'frontend/src/i18n/locales/zh.json').read_text())
    page.add_script_tag(path=native_module_path('.native/program-saved-result.js',
        ROOT/'frontend/src/conversation/ui/program-execution-card.ts'))
    page.evaluate("""messages=>{
      document.body.innerHTML='<main id="card"></main>';
      const text=(key,fallback,values)=>String(messages[key]||fallback)
        .replace(/\{(\w+)\}/g,(_,k)=>values?.[k]??'');
      const node=document.getElementById('card');
      renderProgramExecutionCard(node,{programStatus:'completed',
        programCode:'return tools.keep(value);',
        programStats:{tool_calls:1,successful_tool_calls:1},
        programResult:{kind:'program_value',ref:'input_'+'a'.repeat(32),
          bytes:22144,sourceCoverage:'unknown'}},text);
      window.renderSavedRead=()=>renderProgramExecutionCard(node,{programStatus:'completed',
        programCode:'return tools.result({ref:reference});',
        programStats:{tool_calls:0,successful_tool_calls:0,result_reads:1},
        programResult:{keys:['0','1']}},text);
    }""", messages)
    card = page.locator('.ptc-card')
    header = card.locator('.ptc-head')
    assert '已保存程序结果（21.6 KiB）' in header.inner_text()
    assert 'input_' not in header.inner_text()
    header.click()
    card.locator('.ptc-result summary').click()
    assert 'input_' in card.locator('.ptc-result pre').inner_text()
    page.evaluate('renderSavedRead()')
    assert '读取已保存的结果 1 次' in header.inner_text()
    assert '未发起' not in header.inner_text()
    assert 'tools.result' not in header.inner_text()
    assert '本次没有再次执行源工具' in card.locator('.ptc-measurements').text_content()
    assert card.locator('.ptc-technical').evaluate('node=>node.open') is False
    assert card.evaluate('node=>node.getBoundingClientRect().right<=innerWidth+1')

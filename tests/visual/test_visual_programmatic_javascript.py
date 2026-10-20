"""Real Chromium renders actual async-gateway observations and compact metrics.

Leaf data I/O alone is replaced by the gateway fixture. The retained renderer
and shipped CSS run on an isolated local HTTP origin; no storage is substituted.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT
from tests.tasks_pkg.test_javascript_dependency_chains import local_gateway  # noqa: F401

pytestmark = pytest.mark.visual
pytest_plugins = ("tests.support.frontend_visual",)


@pytest.mark.parametrize('handoff_reason', ['semantic', 'missing_program_result', 'tool_name', 'aggregate'])
@pytest.mark.parametrize('width,language,dark', [
    (1280,'zh',False), (390,'zh',False), (1280,'en',True), (390,'en',True),
])
def test_actual_async_program_observation_renders_before_collapsed_code(
        local_gateway, frontend_visual_page, width, language, dark, tmp_path, handoff_reason):
    task, dispatched, invoke = local_gateway
    evidence = [{'id':9223372036854775807,'clause':'<img src=x onerror=alert(1)>', 'fee':3},
                {'id':'beta','clause':'May terminate without any penalty.', 'fee':120}]
    source = '''
      const clauses = await tools.invoke('read_clauses', {});
      const candidates = clauses.map(({id,clause,fee}) => ({id,clause,fee}));
      tools.observe('Select the clause that meets the semantic requirement', candidates);
      await tools.invoke('must_not_write', {});
    '''
    if handoff_reason == 'missing_program_result':
        source = "const clauses=await tools.invoke('read_clauses',{}); async function main(){return clauses;}"
    elif handoff_reason == 'tool_name':
        source = "return await tools.invoke('unavailable_bare_name',{});"
    elif handoff_reason == 'aggregate':
        source = "const rows=await tools.invoke('read_clauses',{}); return {count:rows.length,total:rows.reduce((s,r)=>s+r.fee,0)};"
    program = invoke(source, [{'type':'function','function':{'name':name,
        'parameters':{'type':'object','properties':{}}}} for name in
        ['read_clauses','must_not_write']], {
        'read_clauses':lambda args:('done',evidence),
        'must_not_write':lambda args:pytest.fail('effect after handoff'),
    }, expected_status='error' if handoff_reason == 'missing_program_result' else 'ok')
    assert dispatched == ([] if handoff_reason == 'tool_name' else [('read_clauses',{})])
    assert program['stats']['engine'] == 'quickjs-wasm'
    row = next(x for x in task['toolRounds'] if x.get('_programSynthetic'))
    page = frontend_visual_page
    page.set_viewport_size({'width':width,'height':880})
    page.evaluate('''dark=>{
      document.documentElement.dataset.theme=dark?'dark':'light';
      document.documentElement.classList.toggle('dark',dark);
      document.body.innerHTML='<main id="program-fixture"></main>';
    }''',dark)
    page.add_style_tag(content='body{display:block;overflow:auto;height:auto}#program-fixture{max-width:960px;margin:24px auto;padding:0 16px}')
    messages=json.loads((ROOT/'frontend/src/i18n/locales'/f'{language}.json').read_text())
    page.evaluate('messages=>window.messages=messages',messages)
    page.add_script_tag(content=r'''
      window._featureFlags={debug_mode:false};
      window.Icon=()=>'<svg aria-hidden="true" viewBox="0 0 16 16"></svg>';
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window.t=(key,params)=>String(messages[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
    ''')
    for name, source in [('groups','ui/tool-groups/index.ts'), ('card','ui/program-execution-card.ts')]:
        page.add_script_tag(path=native_module_path(f'.native/program-actual-{name}.js',
            ROOT/'frontend/src/conversation'/source))
    # This is the production display conversion, not an independently authored formatter.
    page.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    page.evaluate('row=>{window.programRow=row;document.getElementById("program-fixture").innerHTML=_renderToolSlot(row,[row])}',row)
    card=page.locator('.ptc-card')
    assert card.is_visible()

    if handoff_reason == 'aggregate':
        # Success is quiet by default; errors and judgment handoffs are open.
        assert card.evaluate('node=>node.open') is False
        assert card.locator('.ptc-result').evaluate('node=>node.open') is False
        card.locator(':scope > summary').click()
        card.locator('.ptc-result > summary').click()
    else:
        assert card.evaluate('node=>node.open') is True
    expected_reason = (messages['ptc.reason.noResult'] if handoff_reason == 'missing_program_result'
                       else messages['ptc.reason.toolName'] if handoff_reason == 'tool_name'
                       else '' if handoff_reason == 'aggregate'
                       else program['result']['reason'])
    if handoff_reason in {'aggregate', 'missing_program_result'}:
        assert card.locator('.ptc-observation').count() == 0
        if handoff_reason == 'aggregate':
            assert json.loads(card.locator('.ptc-result pre').inner_text()) == {'count':2,'total':123}
        else:
            assert card.locator('.ptc-status').inner_text() == messages['ptc.status.failed']
    else:
        assert card.locator('.ptc-observation').inner_text() == expected_reason
    assert card.locator('.ptc-result').evaluate('node=>node.open') is True
    assert card.locator('.ptc-code').evaluate('node=>node.open') is False
    assert card.locator('.ptc-result img').count() == 0
    if handoff_reason == 'semantic':
        assert '<img src=x onerror=alert(1)>' in card.locator('.ptc-result').inner_text()
        assert '9223372036854775807' in card.locator('.ptc-result').inner_text()
    elif handoff_reason == 'missing_program_result':
        assert program['stats']['outcome'] == 'error'
        assert program['error']['code'] == 'missing_program_result'
        displayed = json.loads(card.locator('.ptc-result pre').inner_text())
        assert displayed['code'] == 'missing_program_result'
        assert displayed['executionReceipt']['successfulToolCalls'] == 1
    elif handoff_reason == 'tool_name':
        assert program['stats']['successful_tool_calls'] == 0
    card.locator('.ptc-technical > summary').click()
    assert messages['ptc.calls.successful'].replace(
        '{done}', str(program['stats']['successful_tool_calls'])).replace(
        '{attempts}', str(program['stats']['tool_calls'])) in card.locator('.ptc-measurements').inner_text()
    assert card.locator('.ptc-measurements').is_visible()
    assert page.evaluate('''()=>{
      const card=document.querySelector('.ptc-card');
      return card.getBoundingClientRect().right<=innerWidth+1
        && card.scrollWidth<=card.clientWidth+1
        && card.querySelector('.ptc-result').compareDocumentPosition(card.querySelector('.ptc-code'))&Node.DOCUMENT_POSITION_FOLLOWING;
    }''')
    assert 'local_javascript' not in card.inner_text()
    card.locator('.ptc-code summary').click()
    assert 'await tools.invoke' in card.locator('.ptc-code code').inner_text()
    card.screenshot(path=str(tmp_path/f'program-{width}-{language}.png'))
    page.evaluate('()=>document.getElementById("program-fixture").innerHTML=_renderToolSlot(programRow,[programRow])')
    if handoff_reason in {'aggregate', 'missing_program_result'}:
        assert card.locator('.ptc-observation').count() == 0
    else:
        assert card.locator('.ptc-observation').inner_text() == expected_reason
    assert card.locator('.ptc-result').evaluate('node=>node.open') is (handoff_reason != 'aggregate')
